"""
Turn structure: every phase and step of a turn, in order (RFC Section 7).

    UNTAP STEP
    UPKEEP STEP           <-- a priority window
    DRAW STEP             <-- a priority window
    PRECOMBAT MAIN PHASE  <-- a priority window, sorcery speed for the AP
    COMBAT PHASE          <-- see combat.py
    POSTCOMBAT MAIN PHASE <-- a priority window, sorcery speed for the AP
    END STEP              <-- a priority window
    CLEANUP STEP

The engine runs the rules on one thread, so a turn reads as plain sequential
code. Each step announces itself with PHASE_TRANSITION, does its work, and then
opens a priority window where the RFC asks for one. That window waits until both
players have passed.
"""

from . import combat, protocol
from .combat import transition
from .priority import run_priority_window
from .state import GameOver, MAX_HAND_SIZE


def play_turn(engine) -> None:
    """Play one whole turn. At the end the other player becomes the active one."""
    untap_step(engine)
    upkeep_step(engine)
    draw_step(engine)

    main_phase(engine, protocol.PRECOMBAT_MAIN)
    combat.run_combat_phase(engine)
    main_phase(engine, protocol.POSTCOMBAT_MAIN)

    end_step(engine)
    cleanup_step(engine)


# --- Untap Step (RFC Section 7.2) ---------------------------------------

def untap_step(engine) -> None:
    """Untap the permanents of the Active Player. We grant no priority here."""
    state = engine.state
    transition(engine, protocol.UNTAP)

    active = state.player(state.active_player)
    for permanent in active.battlefield:
        permanent.tapped = False
        # A creature loses its summoning sickness at the Untap Step of its
        # controller (RFC Section 3). This is the first moment where the
        # sickness would make any difference.
        permanent.summoning_sick = False

    active.land_played_this_turn = False
    engine.broadcast_state_update()


# --- Upkeep Step (RFC Section 7.3) --------------------------------------

def upkeep_step(engine) -> None:
    transition(engine, protocol.UPKEEP)
    run_priority_window(engine)


# --- Draw Step (RFC Section 7.4) ----------------------------------------

def draw_step(engine) -> None:
    """Draw one card for the Active Player, and then open a priority window."""
    state = engine.state
    transition(engine, protocol.DRAW)

    if not _skips_first_draw(state):
        active = state.player(state.active_player)
        if active.draw() is None:
            # A player who has to draw from an empty library loses the game
            # (RFC Section 6.5).
            raise GameOver(
                winner_id=state.opponent_of(state.active_player),
                loser_id=state.active_player,
                reason=protocol.REASON_DECK_EMPTY,
            )
        engine.broadcast_state_update()

    run_priority_window(engine)


def _skips_first_draw(state) -> bool:
    """The player who goes first does not draw on the first turn of the game."""
    return state.turn == 1 and state.active_player == state.player_order[0]


# --- Main Phases (RFC Section 7.5) --------------------------------------

def main_phase(engine, phase: str) -> None:
    """A Main Phase is only a priority window where sorcery speed is legal.

    The code in priority.py checks the land plays and the sorcery speed casts,
    because it also knows which phase we are in.
    """
    transition(engine, phase)
    run_priority_window(engine)


# --- End Step (RFC Section 7.7) -----------------------------------------

def end_step(engine) -> None:
    transition(engine, protocol.END_STEP)
    run_priority_window(engine)


# --- Cleanup Step (RFC Section 7.8) ------------------------------------

def cleanup_step(engine) -> None:
    """Discard down to 7 cards, remove the damage, and clear the end of turn effects.

    We grant no priority here, because nothing in MTGNP 1.0 triggers during the
    cleanup. The step ends by counting the turn up and by switching the Active
    Player.
    """
    state = engine.state
    transition(engine, protocol.CLEANUP)

    _discard_down_to_hand_size(engine)

    # We remove all the damage from the creatures and clear the bonuses that
    # only last until the end of the turn.
    for permanent in state.all_permanents():
        permanent.damage = 0
        permanent.power_bonus = 0
        permanent.toughness_bonus = 0

    engine.broadcast_state_update()

    # We move on to the next turn.
    state.turn += 1
    state.active_player = state.non_active_player


def _discard_down_to_hand_size(engine) -> None:
    """Make the Active Player discard until their hand holds 7 cards or fewer.

    The seq_num token of each DISCARD is the last GAME_STATE_UPDATE that we sent
    to that player (RFC Section 5.4).
    """
    state = engine.state
    player_id = state.active_player
    player = state.player(player_id)

    if len(player.hand) <= MAX_HAND_SIZE:
        return

    token = engine.send_state_update(player_id)

    while len(player.hand) > MAX_HAND_SIZE:
        pdu = engine.await_action(player_id, {protocol.DISCARD}, token)
        card_ids = pdu.get("card_ids")

        problem = _check_discard(player, card_ids)
        if problem is not None:
            engine.send_error(player_id, protocol.ILLEGAL_ACTION, problem, pdu)
            continue

        for card_id in card_ids:
            player.hand.remove(card_id)
            player.graveyard.append(card_id)

        # We report the smaller hand. If the hand is still too big, the seq_num
        # of this update becomes the token for the next DISCARD.
        token = engine.broadcast_state_update()[player_id]


def _check_discard(player, card_ids) -> str | None:
    """Check the card_ids of a DISCARD PDU against the hand of the player."""
    if not isinstance(card_ids, list) or not card_ids:
        return "card_ids must be a non-empty array of cards to discard."

    remaining = list(player.hand)
    for card_id in card_ids:
        if card_id not in remaining:
            return f"{card_id} is not in your hand."
        remaining.remove(card_id)  # This also catches the same card listed twice.
    return None
