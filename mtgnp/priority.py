"""
Priority windows and the stack.

This module implements the priority state machine of RFC Section 8:

    STEP_BEGIN
        |
        +-- PRIORITY_GRANT --> [PRIORITY: AP]
                                    |
                            PRIORITY_PASS (AP passes)
                                    |
                              [PRIORITY: NAP]
                             /              \\
                    NAP casts/acts       PRIORITY_PASS (NAP passes)
                          |                       |
                  caster keeps priority     stack empty?
                                            /          \\
                                         YES            NO
                                          |              |
                                   [STEP_ADVANCE]   resolve top item,
                                                    AP gets priority again

The same rules in our own words (RFC Section 8.1):
  1. The Active Player gets priority first in every priority window.
  2. A player who holds priority can cast a spell, activate an ability that is
     not a mana ability, or pass.
  3. Casting or activating puts an item on the stack, and that player keeps
     priority.
  4. Passing gives priority to the opponent.
  5. Two passes in a row while the stack is not empty resolve the top item, and
     then the Active Player gets priority again.
  6. Two passes in a row while the stack is empty end the step.
"""

from . import cards, effects, mana, protocol
from .state import Permanent, StackItem

# What a player can send while they hold priority.
PRIORITY_ACTIONS = frozenset({
    protocol.PRIORITY_PASS,
    protocol.CAST_SPELL,
    protocol.ACTIVATE_ABILITY,
    protocol.PLAY_LAND,
})


def run_priority_window(engine) -> None:
    """Run one priority window. This returns when the step should move on.

    It raises GameOver if a player wins or loses at any point inside the window.
    """
    state = engine.state
    active_player = state.active_player
    holder = active_player
    consecutive_passes = 0

    while True:
        # We always check the state-based actions before we grant priority
        # (RFC Section 8.4).
        engine.check_state_based_actions()

        seq = engine.grant_priority(holder)
        pdu = engine.await_action(
            holder, PRIORITY_ACTIONS, seq,
            regrant=lambda who=holder: engine.grant_priority(who),
        )

        if pdu["type"] == protocol.PRIORITY_PASS:
            consecutive_passes += 1
            if consecutive_passes < 2:
                holder = state.opponent_of(holder)   # Rule 4.
                continue

            # Both players have now passed one after the other.
            if state.stack:
                resolve_top_of_stack(engine)         # Rule 5.
                consecutive_passes = 0
                holder = active_player
                continue
            state.priority_holder = None             # Rule 6, so the step ends.
            return

        # Every other action is a game action. When it works, the player who
        # acted keeps priority, which is rule 3, and we start counting the passes
        # in a row again from zero.
        if _handle_priority_action(engine, holder, pdu):
            # We send the new state before we give priority back. Without this a
            # client cannot tell that a card left the hand, because STACK_PUSH on
            # its own says nothing about zones.
            engine.broadcast_state_update()
            consecutive_passes = 0
        # When it fails we already sent an ERROR, and the loop grants again.


def _handle_priority_action(engine, player_id: str, pdu: dict) -> bool:
    """Send one game action to the right handler. True means it was legal and we applied it."""
    if pdu["type"] == protocol.CAST_SPELL:
        return cast_spell(engine, player_id, pdu)
    if pdu["type"] == protocol.ACTIVATE_ABILITY:
        return activate_ability(engine, player_id, pdu)
    if pdu["type"] == protocol.PLAY_LAND:
        return play_land(engine, player_id, pdu)
    return False


# --- Casting spells (RFC Section 10.2.7) ----------------------------------

def cast_spell(engine, player_id: str, pdu: dict) -> bool:
    """Check a spell and cast it, which puts it on the stack."""
    state = engine.state
    player = state.player(player_id)
    card_id = pdu.get("card_id")

    if card_id not in player.hand:
        engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                          f"{card_id} is not in your hand.", pdu)
        return False

    card = cards.lookup(card_id)
    if card is None:
        engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                          f"{card_id} is not a card in the fixed set.", pdu)
        return False

    if card.is_land:
        engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                          "Lands are played with PLAY_LAND, not cast.", pdu)
        return False

    # Timing. A player can cast an instant whenever they hold priority. Every
    # other card is sorcery speed, which means only the Active Player, only
    # during a Main Phase, and only while the stack is empty (RFC Section 7.5).
    if not card.is_instant:
        if player_id != state.active_player or state.phase not in protocol.MAIN_PHASES:
            engine.send_error(player_id, protocol.WRONG_PHASE,
                              f"{card.name} can only be cast during your own Main Phase.", pdu)
            return False
        if state.stack:
            engine.send_error(player_id, protocol.WRONG_PHASE,
                              f"{card.name} can only be cast while the stack is empty.", pdu)
            return False

    target_spec = effects.target_spec_for_spell(card)
    if target_spec is None:
        engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                          f"{card.name}'s effect is not implemented in this build.", pdu)
        return False

    targets = pdu.get("targets") or []
    if not _validate_targets(engine, player_id, target_spec, targets, pdu):
        return False

    # We handle the mana last, because paying it taps permanents.
    # check_matches_cost makes sure the declared payment is the right one, and
    # pay() makes sure the player can really produce it and taps the sources in
    # one step.
    try:
        mana.check_matches_cost(pdu.get("mana_payment"), card.cost)
        mana.pay(player, pdu.get("mana_payment"))
    except mana.InsufficientMana as exc:
        engine.send_error(player_id, protocol.INSUFFICIENT_MANA, str(exc), pdu)
        return False

    player.hand.remove(card_id)
    engine.push_stack_item(StackItem(
        stack_item_id=state.next_stack_item_id(),
        item_type=protocol.ITEM_SPELL,
        source=card_id,
        controller=player_id,
        targets=list(targets),
    ))
    return True


# --- Activating abilities (RFC Section 10.2.8) ---------------------------

def activate_ability(engine, player_id: str, pdu: dict) -> bool:
    """Check an ability that is not a mana ability and activate it, which puts it on the stack."""
    state = engine.state
    player = state.player(player_id)
    source_id = pdu.get("source_id")

    permanent = player.find_permanent(source_id)
    if permanent is None:
        engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                          f"You do not control a permanent named {source_id}.", pdu)
        return False

    abilities = effects.abilities_of(source_id)
    index = pdu.get("ability_index", 0)
    if not isinstance(index, int) or not 0 <= index < len(abilities):
        engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                          f"{cards.name_of(source_id)} has no activated ability at "
                          f"index {index} in this build.", pdu)
        return False
    ability = abilities[index]

    if ability.requires_tap:
        if permanent.tapped:
            engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                              f"{cards.name_of(source_id)} is already tapped.", pdu)
            return False
        # Summoning sickness stops an ability that has the tap symbol in its
        # cost (RFC Section 3).
        if permanent.is_creature and permanent.summoning_sick:
            engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                              f"{cards.name_of(source_id)} has summoning sickness.", pdu)
            return False

    targets = pdu.get("targets") or []
    if not _validate_targets(engine, player_id, ability.target_spec, targets, pdu):
        return False

    cost_payment = pdu.get("cost_payment") or {}
    try:
        mana.check_matches_cost(cost_payment.get("mana"), ability.mana_cost)
        mana.pay(player, cost_payment.get("mana"))
    except mana.InsufficientMana as exc:
        engine.send_error(player_id, protocol.INSUFFICIENT_MANA, str(exc), pdu)
        return False

    if ability.requires_tap:
        permanent.tapped = True

    engine.push_stack_item(StackItem(
        stack_item_id=state.next_stack_item_id(),
        item_type=protocol.ITEM_ABILITY,
        source=source_id,
        controller=player_id,
        targets=list(targets),
        ability_index=index,
    ))
    return True


# --- Playing a land (RFC Sections 7.5, 10.2.19) -------------------------

def play_land(engine, player_id: str, pdu: dict) -> bool:
    """Put a land onto the battlefield. This never uses the stack."""
    state = engine.state
    player = state.player(player_id)
    card_id = pdu.get("card_id")

    if player_id != state.active_player or state.phase not in protocol.MAIN_PHASES:
        engine.send_error(player_id, protocol.WRONG_PHASE,
                          "Lands may only be played during your own Main Phase.", pdu)
        return False

    if state.stack:
        engine.send_error(player_id, protocol.WRONG_PHASE,
                          "Lands may only be played while the stack is empty.", pdu)
        return False

    if player.land_played_this_turn:
        engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                          "You have already played a land this turn.", pdu)
        return False

    if card_id not in player.hand:
        engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                          f"{card_id} is not in your hand.", pdu)
        return False

    card = cards.lookup(card_id)
    if card is None or not card.is_land:
        engine.send_error(player_id, protocol.ILLEGAL_ACTION,
                          f"{card_id} is not a land.", pdu)
        return False

    player.hand.remove(card_id)
    player.battlefield.append(Permanent(card_id=card_id, controller=player_id,
                                        summoning_sick=False))
    player.land_played_this_turn = True
    # The caller sends the updated state and the loop sends PRIORITY_GRANT
    # again, so the Active Player keeps priority (RFC Section 7.5).
    return True


# --- Stack resolution (RFC Section 8.4) ---------------------------------

def resolve_top_of_stack(engine) -> None:
    """Take the top item off the stack, check its targets, and resolve or fizzle it."""
    state = engine.state
    item = state.stack.pop()

    target_spec = _target_spec_of(item)

    # If every target became illegal, the item fizzles and does nothing.
    if target_spec != effects.NO_TARGET and item.targets:
        still_legal = [
            target for target in item.targets
            if effects.is_legal_target(state, target_spec, target, item.controller)
        ]
        if not still_legal:
            _send_to_graveyard_if_spell(state, item)
            engine.broadcast({
                "type": protocol.STACK_RESOLVE,
                "stack_item_id": item.stack_item_id,
                "result": "FIZZLE",
                "state_changes": [],
            })
            engine.broadcast_state_update()
            return

    state_changes, entered = _apply(engine, item)

    engine.broadcast({
        "type": protocol.STACK_RESOLVE,
        "stack_item_id": item.stack_item_id,
        "result": "RESOLVED",
        "state_changes": state_changes,
    })

    # We check the state-based actions before anything else. An effect that kills
    # a player ends the game right here, and we open no more priority windows
    # (RFC Section 8.4).
    engine.check_state_based_actions()
    engine.broadcast_state_update()

    # A permanent that just entered play can have an enter-the-battlefield
    # trigger. That trigger goes on the stack before we grant priority again.
    if entered is not None:
        engine.fire_enter_battlefield_triggers(entered)


def _apply(engine, item: StackItem) -> tuple:
    """Apply an item that is resolving. Returns (state_changes, permanent_that_entered)."""
    state = engine.state

    if item.item_type == protocol.ITEM_SPELL:
        card = cards.lookup(item.source)
        if card.is_permanent:
            # Creatures, artifacts and enchantments go to the battlefield.
            permanent = Permanent(card_id=item.source, controller=item.controller)
            state.player(item.controller).battlefield.append(permanent)
            return [effects.change("PERMANENT_ENTERS", card_id=item.source,
                                   controller=item.controller, tapped=False)], permanent

        # An instant or a sorcery does its effect and then goes to the graveyard
        # of its owner.
        effect = effects.spell_effect_for(card)
        changes = effect(state, item) if effect else []
        state.player(item.controller).graveyard.append(item.source)
        return changes, None

    if item.item_type == protocol.ITEM_ABILITY:
        ability = effects.abilities_of(item.source)[item.ability_index]
        return ability.effect(state, item), None

    # Everything left is a triggered ability.
    trigger = effects.ALL_TRIGGERS[item.trigger_key]
    return trigger.effect(state, item), None


def _target_spec_of(item: StackItem) -> str:
    """The target rule that decides which targets a stack item can have."""
    if item.item_type == protocol.ITEM_SPELL:
        return effects.target_spec_for_spell(cards.lookup(item.source)) or effects.NO_TARGET
    if item.item_type == protocol.ITEM_ABILITY:
        return effects.abilities_of(item.source)[item.ability_index].target_spec
    return effects.ALL_TRIGGERS[item.trigger_key].target_spec


def _send_to_graveyard_if_spell(state, item: StackItem) -> None:
    """An instant or a sorcery that fizzled still goes to the graveyard of its owner."""
    if item.item_type != protocol.ITEM_SPELL:
        return
    card = cards.lookup(item.source)
    if card is not None and not card.is_permanent:
        state.player(item.controller).graveyard.append(item.source)


# --- Target validation ---------------------------------------------------

def _validate_targets(engine, player_id: str, target_spec: str, targets: list, pdu: dict) -> bool:
    """Check how many targets there are, and check that each one is legal right now."""
    state = engine.state

    if target_spec == effects.NO_TARGET:
        if targets:
            engine.send_error(player_id, protocol.ILLEGAL_TARGET,
                              "This spell or ability takes no targets.", pdu)
            return False
        return True

    # Every effect with a target in our build takes exactly one target.
    if len(targets) != 1:
        engine.send_error(player_id, protocol.ILLEGAL_TARGET,
                          f"Exactly one target is required; got {len(targets)}.", pdu)
        return False

    if not effects.is_legal_target(state, target_spec, targets[0], player_id):
        engine.send_error(player_id, protocol.ILLEGAL_TARGET,
                          f"{targets[0]} is not a legal target "
                          f"(required: {target_spec}).", pdu)
        return False
    return True
