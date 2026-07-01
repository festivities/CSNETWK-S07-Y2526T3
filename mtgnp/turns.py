"""
Turn structure: every phase and step of a turn, in order (RFC Section 7).

    UNTAP STEP
    UPKEEP STEP           <-- priority window
    DRAW STEP             <-- priority window
    PRECOMBAT MAIN PHASE  <-- priority window (sorcery speed for AP)
    COMBAT PHASE          <-- see combat.py
    POSTCOMBAT MAIN PHASE <-- priority window (sorcery speed for AP)
    END STEP              <-- priority window
    CLEANUP STEP

Because the engine runs the rules on a single thread, a turn reads as plain
sequential code: each step announces itself with PHASE_TRANSITION, does its work,
and (where the RFC says so) opens a priority window that blocks until both
players have passed.
"""

from . import combat, protocol
from .combat import transition
from .priority import run_priority_window
from .state import GameOver, MAX_HAND_SIZE


def play_turn(engine) -> None:
    """Play one complete turn, ending with the next player becoming active."""
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
    """Untap the Active Player's permanents. No priority is granted."""
    state = engine.state
    transition(engine, protocol.UNTAP)

    active = state.player(state.active_player)
    for permanent in active.battlefield:
        permanent.tapped = False
        # A creature stops being summoning sick at its controller's Untap Step
        # (RFC Section 3), which is the first moment the sickness could matter.
        permanent.summoning_sick = False

    active.land_played_this_turn = False
    engine.broadcast_state_update()


# --- Upkeep Step (RFC Section 7.3) --------------------------------------

def upkeep_step(engine) -> None:
    transition(engine, protocol.UPKEEP)
    run_priority_window(engine)


# --- Draw Step (RFC Section 7.4) ----------------------------------------

def draw_step(engine) -> None:
    """Draw one card for the Active Player, then open a priority window."""
    state = engine.state
    transition(engine, protocol.DRAW)

    if not _skips_first_draw(state):
        active = state.player(state.active_player)
        if active.draw() is None:
            # A player required to draw from an empty library loses the game
            # (RFC Section 6.5).
            raise GameOver(
                winner_id=state.opponent_of(state.active_player),
                loser_id=state.active_player,
                reason=protocol.REASON_DECK_EMPTY,
            )
        engine.broadcast_state_update()

    run_priority_window(engine)


def _skips_first_draw(state) -> bool:
    """On the very first turn of the game the first player does not draw."""
    return state.turn == 1 and state.active_player == state.player_order[0]


# --- Main Phases (RFC Section 7.5) --------------------------------------

def main_phase(engine, phase: str) -> None:
    """A Main Phase is simply a priority window at which sorcery speed is legal.

    Land plays and sorcery-speed casts are validated inside priority.py, which
    checks the current phase.
    """
    transition(engine, phase)
    run_priority_window(engine)


# --- End Step (RFC Section 7.7) -----------------------------------------

def end_step(engine) -> None:
    transition(engine, protocol.END_STEP)
    run_priority_window(engine)


# --- Cleanup Step (RFC Section 7.8) ------------------------------------

def cleanup_step(engine) -> None:
    """Discard down to seven, clear damage and until-end-of-turn effects.

    No priority is granted: in MTGNP 1.0 nothing triggers at cleanup.  The step
    finishes by advancing the turn counter and switching the Active Player.
    """
    state = engine.state
    transition(engine, protocol.CLEANUP)

    _discard_down_to_hand_size(engine)

    # Remove all damage from creatures and clear "until end of turn" effects.
    for permanent in state.all_permanents():
        permanent.damage = 0
        permanent.power_bonus = 0
        permanent.toughness_bonus = 0

    engine.broadcast_state_update()

    # Advance to the next turn.
    state.turn += 1
    state.active_player = state.non_active_player


def _discard_down_to_hand_size(engine) -> None:
    """Make the Active Player discard until their hand holds seven or fewer.

    The seq_num token for each DISCARD is the most recent GAME_STATE_UPDATE sent
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

        # Report the reduced hand; if it is still too large this update's
        # seq_num becomes the token for the next DISCARD.
        token = engine.broadcast_state_update()[player_id]


def _check_discard(player, card_ids) -> str | None:
    """Validate a DISCARD PDU's card_ids against the player's hand."""
    if not isinstance(card_ids, list) or not card_ids:
        return "card_ids must be a non-empty array of cards to discard."

    remaining = list(player.hand)
    for card_id in card_ids:
        if card_id not in remaining:
            return f"{card_id} is not in your hand."
        remaining.remove(card_id)  # Catches the same card listed twice.
    return None
