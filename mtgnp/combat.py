"""
The Combat Phase sub-state machine (RFC Section 9).

    BEGIN_COMBAT
        |
    DECLARE_ATTACKERS     <-- AP declares; priority window follows
        |
    DECLARE_BLOCKERS      <-- NAP assigns blockers; priority window follows
        |
    ASSIGN_DAMAGE_ORDER   <-- only if an attacker is blocked by two or more
        |
   [FIRST_STRIKE_DAMAGE]  <-- only if first strike or double strike is present
        |
    COMBAT_DAMAGE         <-- server resolves damage
        |
    END_OF_COMBAT         <-- priority window; combat state is cleared

The declaration steps have no request PDU of their own: the PHASE_TRANSITION that
announces the step implicitly asks the relevant player to declare, and the client
echoes that transition's seq_num in its reply (RFC Sections 5.4, 9.3, 9.4).

MTGNP 1.0 does not implement trample: a blocked attacker deals its damage to its
blockers only, never to the defending player (RFC Section 9.7).
"""

from . import cards, effects, protocol
from .priority import run_priority_window


def run_combat_phase(engine) -> None:
    """Run the whole combat phase for the current turn."""
    state = engine.state

    # --- Beginning of Combat Step (RFC Section 9.2) ---
    transition(engine, protocol.BEGIN_COMBAT)
    run_priority_window(engine)

    # --- Declare Attackers Step (RFC Section 9.3) ---
    request_seq = transition(engine, protocol.DECLARE_ATTACKERS_STEP)
    declare_attackers(engine, request_seq)

    if not state.attackers:
        # With no attackers the server skips straight to End of Combat.
        transition(engine, protocol.END_OF_COMBAT)
        run_priority_window(engine)
        state.clear_combat()
        return

    engine.broadcast_state_update()
    _fire_attack_triggers(engine)
    run_priority_window(engine)

    # --- Declare Blockers Step (RFC Section 9.4) ---
    request_seq = transition(engine, protocol.DECLARE_BLOCKERS_STEP)
    declare_blockers(engine, request_seq)
    engine.broadcast_state_update()
    run_priority_window(engine)

    # --- Assign Damage Order Step (RFC Section 9.5) ---
    # Only entered when at least one attacker is blocked by two or more creatures.
    multiply_blocked = [a for a, blockers in state.blocks.items() if len(blockers) >= 2]
    if multiply_blocked:
        request_seq = transition(engine, protocol.ASSIGN_DAMAGE_ORDER_STEP)
        assign_damage_orders(engine, multiply_blocked, request_seq)
        run_priority_window(engine)

    # --- First Strike Damage Step (RFC Section 9.6) ---
    # Only entered if some creature in combat has first or double strike.
    if _anyone_has_first_strike(engine):
        transition(engine, protocol.FIRST_STRIKE_DAMAGE)
        deal_combat_damage(engine, first_strike_step=True)
        run_priority_window(engine)

    # --- Combat Damage Step (RFC Section 9.7) ---
    transition(engine, protocol.COMBAT_DAMAGE)
    deal_combat_damage(engine, first_strike_step=False)

    # --- End of Combat Step (RFC Section 9.8) ---
    transition(engine, protocol.END_OF_COMBAT)
    run_priority_window(engine)
    state.clear_combat()


def transition(engine, to_phase: str) -> int:
    """Broadcast PHASE_TRANSITION and return its seq_num.

    For the declaration steps that seq_num is also the token the acting client
    must echo, which is why this returns it.
    """
    state = engine.state
    from_phase = state.phase
    state.phase = to_phase
    return engine.broadcast({
        "type": protocol.PHASE_TRANSITION,
        "from_phase": from_phase,
        "to_phase": to_phase,
        "active_player": state.active_player,
        "turn": state.turn,
    })


# --- Declaring attackers (RFC Sections 9.3, 10.2.15) --------------------

def declare_attackers(engine, request_seq: int) -> None:
    """Wait for the Active Player's DECLARE_ATTACKERS and apply it."""
    state = engine.state
    attacker_player = state.active_player
    defender = state.non_active_player

    while True:
        pdu = engine.await_action(attacker_player, {protocol.DECLARE_ATTACKERS}, request_seq)
        declarations = pdu.get("attackers")

        if not isinstance(declarations, list):
            engine.send_error(attacker_player, protocol.ILLEGAL_ACTION,
                              "attackers must be an array (send [] to declare no attack).", pdu)
            continue

        problem = _check_attackers(engine, attacker_player, defender, declarations)
        if problem is not None:
            engine.send_error(attacker_player, protocol.ILLEGAL_ACTION, problem, pdu)
            continue

        # The declaration is legal: record it and tap the attackers.
        for declaration in declarations:
            creature_id = declaration["creature_id"]
            state.attackers[creature_id] = declaration.get("target", defender)
            creature = state.find_permanent(creature_id)
            # Declaring an attacker taps it immediately, unless it has vigilance.
            if not creature.has(cards.VIGILANCE):
                creature.tapped = True
        return


def _check_attackers(engine, attacker_player: str, defender: str, declarations: list) -> str | None:
    """Return a human-readable problem with the declaration, or None if legal."""
    state = engine.state
    seen = set()

    for declaration in declarations:
        if not isinstance(declaration, dict) or "creature_id" not in declaration:
            return "Each attacker entry needs a creature_id."

        creature_id = declaration["creature_id"]
        if creature_id in seen:
            return f"{creature_id} was declared as an attacker twice."
        seen.add(creature_id)

        creature = state.player(attacker_player).find_permanent(creature_id)
        if creature is None or not creature.is_creature:
            return f"You do not control a creature named {creature_id}."
        if creature.tapped:
            return f"{cards.name_of(creature_id)} is tapped and cannot attack."
        # Summoning sickness stops an attack unless the creature has haste.
        if creature.summoning_sick and not creature.has(cards.HASTE):
            return f"{cards.name_of(creature_id)} has summoning sickness and cannot attack."
        if creature.has(cards.DEFENDER):
            return f"{cards.name_of(creature_id)} has defender and cannot attack."

        target = declaration.get("target", defender)
        if target != defender:
            return f"{target} is not the defending player."

    return None


def _fire_attack_triggers(engine) -> None:
    """Queue "whenever this creature attacks" triggers (RFC Section 8.6)."""
    state = engine.state
    pending = []
    for attacker_id, defending_player in state.attackers.items():
        trigger = effects.ATTACK_TRIGGERS.get(cards.base_of(attacker_id))
        if trigger is not None:
            pending.append((trigger, state.active_player, attacker_id,
                            {"defender": defending_player}))
    engine.put_triggers_on_stack(pending)


# --- Declaring blockers (RFC Sections 9.4, 10.2.16) --------------------

def declare_blockers(engine, request_seq: int) -> None:
    """Wait for the Non-Active Player's DECLARE_BLOCKERS and apply it."""
    state = engine.state
    blocking_player = state.non_active_player

    while True:
        pdu = engine.await_action(blocking_player, {protocol.DECLARE_BLOCKERS}, request_seq)
        declarations = pdu.get("blockers")

        if not isinstance(declarations, list):
            engine.send_error(blocking_player, protocol.ILLEGAL_ACTION,
                              "blockers must be an array (send [] to not block).", pdu)
            continue

        problem = _check_blockers(engine, blocking_player, declarations)
        if problem is not None:
            engine.send_error(blocking_player, protocol.ILLEGAL_ACTION, problem, pdu)
            continue

        # Blocking does not tap the blocking creatures (RFC Section 9.4).
        state.blocks.clear()
        for declaration in declarations:
            attacker_id = declaration["blocking_id"]
            state.blocks.setdefault(attacker_id, []).append(declaration["creature_id"])
        return


def _check_blockers(engine, blocking_player: str, declarations: list) -> str | None:
    """Return a problem with the block assignment, or None if it is legal."""
    state = engine.state
    already_blocking = set()

    for declaration in declarations:
        if not isinstance(declaration, dict) or "creature_id" not in declaration \
                or "blocking_id" not in declaration:
            return "Each blocker entry needs a creature_id and a blocking_id."

        blocker_id = declaration["creature_id"]
        attacker_id = declaration["blocking_id"]

        # A single creature may block only one attacker (RFC Section 9.4).
        if blocker_id in already_blocking:
            return f"{cards.name_of(blocker_id)} cannot block more than one attacker."
        already_blocking.add(blocker_id)

        blocker = state.player(blocking_player).find_permanent(blocker_id)
        if blocker is None or not blocker.is_creature:
            return f"You do not control a creature named {blocker_id}."
        if blocker.tapped:
            return f"{cards.name_of(blocker_id)} is tapped and cannot block."

        if attacker_id not in state.attackers:
            return f"{attacker_id} is not attacking."

        attacker = state.find_permanent(attacker_id)
        # A creature with flying can only be blocked by another flyer.
        if attacker.has(cards.FLYING) and not blocker.has(cards.FLYING):
            return (f"{cards.name_of(attacker_id)} has flying and cannot be blocked "
                    f"by {cards.name_of(blocker_id)}.")

    return None


# --- Assigning damage order (RFC Sections 9.5, 10.2.17) ---------------

def assign_damage_orders(engine, multiply_blocked: list, request_seq: int) -> None:
    """Collect one ASSIGN_DAMAGE_ORDER per attacker blocked by two or more creatures."""
    state = engine.state
    outstanding = set(multiply_blocked)

    while outstanding:
        pdu = engine.await_action(state.active_player, {protocol.ASSIGN_DAMAGE_ORDER}, request_seq)
        attacker_id = pdu.get("attacker_id")
        order = pdu.get("blocker_order")

        if attacker_id not in outstanding:
            engine.send_error(state.active_player, protocol.ILLEGAL_ACTION,
                              f"{attacker_id} is not awaiting a damage order. "
                              f"Still needed: {sorted(outstanding)}.", pdu)
            continue

        if not isinstance(order, list) or sorted(order) != sorted(state.blocks[attacker_id]):
            engine.send_error(state.active_player, protocol.ILLEGAL_ACTION,
                              f"blocker_order must list exactly "
                              f"{state.blocks[attacker_id]}.", pdu)
            continue

        state.damage_order[attacker_id] = list(order)
        outstanding.discard(attacker_id)


# --- Dealing combat damage (RFC Sections 9.6, 9.7, 10.2.18) ----------

def deal_combat_damage(engine, first_strike_step: bool) -> None:
    """Assign and apply all combat damage for one damage step, simultaneously.

    `first_strike_step` selects which creatures deal damage now: creatures with
    first strike or double strike in the First Strike Damage Step, and everyone
    else -- plus double strikers again -- in the regular Combat Damage Step.
    """
    state = engine.state
    events = []

    for attacker_id, defending_player in state.attackers.items():
        attacker = state.find_permanent(attacker_id)
        if attacker is None:
            continue  # It was destroyed or removed before damage.

        blockers = [
            blocker_id for blocker_id in state.blocks.get(attacker_id, [])
            if state.find_permanent(blocker_id) is not None
        ]

        # Attacker's damage.
        if _deals_damage_now(attacker, first_strike_step):
            if not blockers:
                # Unblocked: damage goes straight to the defending player.
                events.append({"source": attacker_id, "target": defending_player,
                               "amount": attacker.power})
            else:
                events.extend(_assign_to_blockers(state, attacker, attacker_id, blockers))

        # Each blocker deals its power back to the attacker it is blocking.
        for blocker_id in blockers:
            blocker = state.find_permanent(blocker_id)
            if _deals_damage_now(blocker, first_strike_step) and blocker.power > 0:
                events.append({"source": blocker_id, "target": attacker_id,
                               "amount": blocker.power})

    # All combat damage is dealt simultaneously, so compute every event before
    # applying any of it.
    for event in events:
        effects.deal_damage(state, event["target"], event["amount"])

    # Creatures with lethal damage die; a player at zero life loses.
    died = engine.check_state_based_actions()

    engine.broadcast({
        "type": protocol.COMBAT_DAMAGE_RESULT,
        "damage_events": events,
        "life_totals": {pid: state.players[pid].life for pid in state.player_order},
        "creatures_died": died,
    })
    engine.broadcast_state_update()


def _assign_to_blockers(state, attacker, attacker_id: str, blockers: list) -> list:
    """Split an attacker's power among its blockers, in the chosen order.

    Each blocker in order is assigned enough damage to be lethal before the next
    one receives any; whatever is left over is assigned to the final blocker.
    With no trample in MTGNP 1.0, excess damage is simply lost.
    """
    order = [b for b in state.damage_order.get(attacker_id, blockers) if b in blockers]
    events = []
    remaining = attacker.power

    for index, blocker_id in enumerate(order):
        if remaining <= 0:
            break
        blocker = state.find_permanent(blocker_id)
        lethal_needed = max(1, blocker.toughness - blocker.damage)
        is_last = index == len(order) - 1
        amount = remaining if is_last else min(remaining, lethal_needed)
        events.append({"source": attacker_id, "target": blocker_id, "amount": amount})
        remaining -= amount

    return events


def _deals_damage_now(creature, first_strike_step: bool) -> bool:
    """Does this creature deal its combat damage in the current damage step?"""
    has_first = creature.has(cards.FIRST_STRIKE)
    has_double = creature.has(cards.DOUBLE_STRIKE)
    if first_strike_step:
        return has_first or has_double
    return (not has_first) or has_double


def _anyone_has_first_strike(engine) -> bool:
    """Is a First Strike Damage Step needed at all (RFC Section 9.6)?"""
    state = engine.state
    in_combat = list(state.attackers) + [b for blockers in state.blocks.values() for b in blockers]
    for permanent_id in in_combat:
        permanent = state.find_permanent(permanent_id)
        if permanent is not None and (permanent.has(cards.FIRST_STRIKE)
                                      or permanent.has(cards.DOUBLE_STRIKE)):
            return True
    return False
