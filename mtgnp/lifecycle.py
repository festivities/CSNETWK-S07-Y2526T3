"""
The game lifecycle (RFC Section 6).

    LOBBY --> GAME_SETUP --> MULLIGAN --> IN_GAME --> GAME_OVER
      ^                                                  |
      +--------------------------------------------------+
                the server waits for new PLAYER_READY PDUs

After GAME_OVER the server goes back to LOBBY and waits for a new PLAYER_READY
from each player over the same TCP connections. This lets the two players play
again without connecting a second time (RFC Sections 6.2 and 6.6).
"""

import random

from . import cards, protocol, turns
from .engine import DISCONNECTED
from .state import GameOver, GameState, STARTING_HAND_SIZE, STARTING_LIFE

MIN_DECK_SIZE = 1
MAX_DECK_SIZE = 50


# --- LOBBY (RFC Section 6.2) ---------------------------------------------

def run_lobby(engine, server) -> dict:
    """Wait until both connected clients have sent a valid PLAYER_READY.

    This returns {player_id: (connection, deck_list)}. The clients choose their
    own player IDs, so this code runs before any GameState exists. That is why it
    reads the inbox directly instead of using the helpers that the game uses.
    """
    ready: dict = {}   # connection -> (player_id, deck_list)
    engine.logger.note("LOBBY: waiting for PLAYER_READY from two players")

    while True:
        # Are both players ready, and are both of them still connected?
        live = server.live_connections()
        if len(live) >= 2 and all(connection in ready for connection in live[:2]):
            chosen = live[:2]
            return {ready[c][0]: (c, ready[c][1]) for c in chosen}

        connection, pdu = engine.inbox.get()

        if pdu is DISCONNECTED:
            ready.pop(connection, None)
            engine.logger.note(f"LOBBY: {connection.label} disconnected while waiting")
            continue

        pdu_type = pdu.get("type")

        if pdu_type not in protocol.CLIENT_PDU_TYPES:
            engine.send_error_to(connection, protocol.UNKNOWN_TYPE,
                                 f"Unknown PDU type {pdu_type!r}.", pdu)
            continue

        if pdu_type != protocol.PLAYER_READY:
            engine.send_error_to(connection, protocol.WRONG_PHASE,
                                 "Only PLAYER_READY is accepted in the LOBBY state.", pdu)
            continue

        problem_code, problem = _check_player_ready(pdu, connection, ready)
        if problem is not None:
            engine.send_error_to(connection, problem_code, problem, pdu)
            continue

        # We accept this submission, or replace an earlier one. A player can send
        # PLAYER_READY again before both players are ready, and in that case the
        # newer deck list is the one we use.
        player_id = pdu["player_id"]
        connection.player_id = player_id
        ready[connection] = (player_id, list(pdu["deck_list"]))
        engine.logger.note(f"LOBBY: {player_id} is ready with {len(pdu['deck_list'])} cards")

        _send_lobby_status(engine, server, ready)


def _check_player_ready(pdu: dict, connection, ready: dict) -> tuple:
    """Check a PLAYER_READY PDU. Returns (error_code, message), or (None, None) if it is fine."""
    player_id = pdu.get("player_id")
    if not isinstance(player_id, str) or not player_id.strip():
        return protocol.ILLEGAL_ACTION, "player_id must be a non-empty string."

    # The other connected player must not already use this ID.
    for other_connection, (other_id, _) in ready.items():
        if other_connection is not connection and other_id == player_id:
            return protocol.DUPLICATE_ID, f"player_id {player_id!r} is already claimed."

    deck_list = pdu.get("deck_list")
    if not isinstance(deck_list, list):
        return protocol.ILLEGAL_DECK, "deck_list must be an array of card IDs."
    if len(deck_list) < MIN_DECK_SIZE:
        return protocol.ILLEGAL_DECK, "Deck is empty; at least 1 card is required."
    if len(deck_list) > MAX_DECK_SIZE:
        return (protocol.ILLEGAL_DECK,
                f"Deck contains {len(deck_list)} cards; maximum is {MAX_DECK_SIZE}.")

    if len(set(deck_list)) != len(deck_list):
        return protocol.ILLEGAL_DECK, "Deck lists the same card instance more than once."

    illegal = [card_id for card_id in deck_list if not cards.is_legal_card_id(card_id)]
    if illegal:
        return (protocol.ILLEGAL_DECK,
                f"Not cards in the fixed set: {', '.join(illegal[:5])}.")

    # Both decks come from one shared fixed set, and the RFC names each permanent
    # by its card instance ID (Section 10.2.2). Two players cannot own the same
    # instance, because then a permanent ID would point to two different cards.
    for other_connection, (_, other_deck) in ready.items():
        if other_connection is connection:
            continue
        clash = sorted(set(deck_list) & set(other_deck))
        if clash:
            return (protocol.ILLEGAL_DECK,
                    f"These card instances are already in your opponent's deck: "
                    f"{', '.join(clash[:5])}. Use different copy numbers.")

    return None, None


def _send_lobby_status(engine, server, ready: dict) -> None:
    """Tell every connected client how many players are ready (RFC 10.2.2).

    `waiting_for` lists the player slots that are not ready yet, by their slot
    label. A slot still counts when its client connected but has not sent
    PLAYER_READY, and also when no client connected to it at all. The server
    cannot know the player_id that a missing client will choose, so it reports
    the slot instead.
    """
    live = server.live_connections()
    ready_labels = {c.label for c in live if c in ready}
    waiting_for = [label for label in protocol.PLAYER_SLOT_LABELS
                   if label not in ready_labels]

    for connection in live:
        engine.send_to(connection, {
            "type": protocol.GAME_STATE_UPDATE,
            "state": {
                "phase": protocol.LOBBY,
                "players_ready": len(ready),
                "waiting_for": waiting_for,
            },
        })


# --- GAME_SETUP (RFC Section 6.3) ---------------------------------------

def run_game_setup(engine, ready: dict) -> None:
    """Build the game state: the life totals, the shuffled decks, the opening hands, and the coin flip.

    This runs on its own and needs no input from the clients.
    """
    # We confirm that both players are ready before anything else happens. The
    # worked example does this in Step 4.
    for player_id, (connection, _) in ready.items():
        engine.send_to(connection, {
            "type": protocol.GAME_STATE_UPDATE,
            "state": {"phase": protocol.GAME_SETUP, "players_ready": 2, "waiting_for": []},
        })

    player_ids = list(ready)
    state = GameState(player_ids)
    engine.state = state
    engine.connections = {pid: connection for pid, (connection, _) in ready.items()}

    for player_id, (_, deck_list) in ready.items():
        player = state.player(player_id)
        player.deck_list = list(deck_list)
        player.life = STARTING_LIFE
        player.library = list(deck_list)
        random.shuffle(player.library)          # The server does the shuffle.
        _draw_opening_hand(player)

    # A random coin flip decides who goes first.
    first_player = random.choice(player_ids)
    state.player_order = [first_player, state.opponent_of(first_player)]
    state.active_player = first_player
    state.turn = 0
    state.phase = protocol.MULLIGAN

    engine.logger.note(f"GAME_SETUP: {first_player} wins the coin flip and goes first")


def _draw_opening_hand(player) -> None:
    """Draw 7 cards, or the whole library when the deck has fewer than 7 cards."""
    player.hand = []
    for _ in range(min(STARTING_HAND_SIZE, len(player.library))):
        player.draw()


# --- MULLIGAN (RFC Section 6.4) -----------------------------------------

def run_mulligan(engine) -> None:
    """Run the London Mulligan for both players.

    Each player decides on their own, and they can answer in any order. A player
    who takes a mulligan draws a new hand of 7 cards. When that player finally
    keeps a hand after N mulligans, they have to put exactly N cards on the
    bottom of their library.
    """
    state = engine.state
    state.phase = protocol.MULLIGAN

    # The MULLIGAN state update of a player carries the seq_num that the same
    # player has to echo back.
    tokens = engine.broadcast_state_update()
    pending = dict(tokens)

    while pending:
        player_id, pdu = engine.await_from_any({protocol.MULLIGAN_CHOICE}, pending)
        player = state.player(player_id)
        keep = pdu.get("keep")
        to_bottom = pdu.get("cards_to_bottom")

        if not isinstance(keep, bool) or not isinstance(to_bottom, list):
            engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                              "MULLIGAN_CHOICE needs a boolean 'keep' and an array "
                              "'cards_to_bottom'.", pdu)
            continue

        if not keep:
            # The player takes a mulligan, so cards_to_bottom has to be empty.
            if to_bottom:
                engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                                  "cards_to_bottom must be empty when keep is false.", pdu)
                continue
            player.mulligans += 1
            _redraw_hand(player)
            engine.logger.note(f"MULLIGAN: {player_id} takes mulligan #{player.mulligans}")
            # Only the player who took the mulligan gets a new hand, and the
            # update for that hand carries their new token.
            pending[player_id] = engine.send_state_update(player_id)
            continue

        # The player keeps the hand, so one card per mulligan goes to the bottom.
        problem = _check_cards_to_bottom(player, to_bottom)
        if problem is not None:
            engine.send_error(player_id, protocol.ILLEGAL_ACTION, problem, pdu)
            continue

        for card_id in to_bottom:
            player.hand.remove(card_id)
            player.library.append(card_id)      # The end of the list is the bottom.
        player.has_kept = True
        del pending[player_id]
        engine.logger.note(
            f"MULLIGAN: {player_id} keeps a hand of {len(player.hand)} "
            f"after {player.mulligans} mulligan(s)")

    engine.broadcast_state_update()


def _redraw_hand(player) -> None:
    """Shuffle the hand back into the library and draw a new hand of 7 cards."""
    player.library.extend(player.hand)
    player.hand = []
    random.shuffle(player.library)
    _draw_opening_hand(player)


def _check_cards_to_bottom(player, to_bottom: list) -> str | None:
    """The cards going to the bottom have to be in the hand, and there have to be
    as many of them as the player took mulligans."""
    if len(to_bottom) != player.mulligans:
        return (f"cards_to_bottom must contain exactly {player.mulligans} card(s); "
                f"got {len(to_bottom)}.")

    remaining = list(player.hand)
    for card_id in to_bottom:
        if card_id not in remaining:
            return f"{card_id} is not in your hand."
        remaining.remove(card_id)
    return None


# --- IN_GAME and GAME_OVER (RFC Sections 6.5, 6.6) --------------------

def run_game(engine, ready: dict) -> None:
    """Play one whole game, from the setup up to sending GAME_OVER."""
    try:
        run_game_setup(engine, ready)
        run_mulligan(engine)

        # The first turn of the first player is turn 1 (RFC Section 6.5).
        engine.state.turn = 1
        while True:
            turns.play_turn(engine)

    except GameOver as over:
        broadcast_game_over(engine, over)


def broadcast_game_over(engine, over: GameOver) -> None:
    """Announce the result. winner_id is always the player who survived or who did nothing wrong."""
    engine.logger.note(f"GAME_OVER: {over.winner_id} wins ({over.reason})")
    engine.broadcast({
        "type": protocol.GAME_OVER,
        "winner_id": over.winner_id,
        "loser_id": over.loser_id,
        "reason": over.reason,
    })
