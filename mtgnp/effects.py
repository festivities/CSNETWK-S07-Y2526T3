"""
Card effects: spells, activated abilities and triggered abilities.

The rubric asks for at least five card effects. We implemented 16 spells, 4
activated abilities and 3 triggered abilities. Implementing every printed
ability on every card in the master list is a bonus objective, so we left it out
on purpose. A card whose effect we did not implement is still a legal deck
entry, but the server refuses to cast it and answers with ERROR/ILLEGAL_ACTION.
The README lists these cards as a known limitation.

How we write an effect
----------------------
Every effect is a function `fn(state, item) -> list[state_change]`. It changes
the authoritative GameState and returns the `state_changes` array that we send
in the STACK_RESOLVE PDU (RFC Section 10.2.14). An effect never touches a socket
and never decides who has priority, which makes it easy to read and to test.

We check the targets twice, once when the player casts the spell and again when
it resolves. If every target became illegal in between, the item fizzles (RFC
Section 8.4) and we never call the effect function.
"""

from dataclasses import dataclass, field

from . import cards

# --- Target specifications -------------------------------------------------
#
# A spec says what one target is allowed to be. Every effect in our build takes
# either no target at all or exactly one target.

NO_TARGET = "none"
ANY_TARGET = "any"                  # A player or a creature, which is "any target".
PLAYER = "player"
CREATURE = "creature"
TAPPED_CREATURE = "tapped_creature"
NONBLACK_CREATURE = "nonblack_creature"
NONBLACK_NONARTIFACT_CREATURE = "nonblack_nonartifact_creature"
SPELL = "spell"                     # Another spell on the stack.
NONCREATURE_SPELL = "noncreature_spell"
OWN_GRAVEYARD_CREATURE = "own_graveyard_creature"


# --- Helper for one state_changes record -----------------------------------
# Section 10.2.14 names this field `change_type`, while the prose examples call
# it `type`. We follow the schema, and state.py explains why.

def change(change_type: str, **fields) -> dict:
    return {"change_type": change_type, **fields}


# --- Primitive effect helpers ----------------------------------------------

def deal_damage(state, target: str, amount: int, source_name: str = "") -> list:
    """Deal `amount` damage to a player or to a creature.

    Damage on a creature only gets marked on it. The creature dies later, when we
    check the state-based actions again (RFC Section 8.4). This is the reason
    that this helper never moves anything to a graveyard on its own.
    """
    if state.is_player_id(target):
        state.player(target).life -= amount
        return [change("DAMAGE", target=target, amount=amount)]

    permanent = state.find_permanent(target)
    if permanent is None:
        return []
    permanent.damage += amount
    return [change("DAMAGE", target=target, amount=amount)]


def gain_life(state, player_id: str, amount: int) -> list:
    state.player(player_id).life += amount
    return [change("LIFE_GAIN", target=player_id, amount=amount)]


def lose_life(state, player_id: str, amount: int) -> list:
    state.player(player_id).life -= amount
    return [change("LIFE_LOSS", target=player_id, amount=amount)]


def destroy(state, permanent_id: str) -> list:
    """Move a permanent from the battlefield to the graveyard of its owner."""
    permanent = state.find_permanent(permanent_id)
    if permanent is None:
        return []
    owner = state.player(permanent.controller)
    owner.battlefield.remove(permanent)
    owner.graveyard.append(permanent.card_id)
    return [change("DESTROY", target=permanent_id)]


def exile(state, permanent_id: str) -> list:
    permanent = state.find_permanent(permanent_id)
    if permanent is None:
        return []
    owner = state.player(permanent.controller)
    owner.battlefield.remove(permanent)
    owner.exile.append(permanent.card_id)
    return [change("EXILE", target=permanent_id)]


def return_to_hand(state, permanent_id: str) -> list:
    permanent = state.find_permanent(permanent_id)
    if permanent is None:
        return []
    owner = state.player(permanent.controller)
    owner.battlefield.remove(permanent)
    owner.hand.append(permanent.card_id)
    return [change("RETURN_TO_HAND", target=permanent_id)]


def counter_spell(state, stack_item_id: str) -> list:
    """Take a spell off the stack and put its card into the graveyard of its owner."""
    target_item = state.find_stack_item(stack_item_id)
    if target_item is None:
        return []
    state.stack.remove(target_item)
    state.player(target_item.controller).graveyard.append(target_item.source)
    return [change("COUNTER", target=stack_item_id)]


def pump(state, permanent_id: str, power: int, toughness: int) -> list:
    """Give a creature a bonus to its power and toughness until end of turn."""
    permanent = state.find_permanent(permanent_id)
    if permanent is None:
        return []
    permanent.power_bonus += power
    permanent.toughness_bonus += toughness
    return [change("PUMP", target=permanent_id, power=power, toughness=toughness)]


def devotion_to_black(state, player_id: str) -> int:
    """Count the {B} symbols in the mana costs of the permanents that `player_id` controls."""
    return sum(
        permanent.card.cost.get("B", 0)
        for permanent in state.player(player_id).battlefield
    )


# --- Spell effects ---------------------------------------------------------
# Each entry is a pair of a target specification and an effect function. The
# effect gets the StackItem that is resolving, and we already checked the
# `targets` list of that item a second time.

def _damage_effect(amount: int):
    """Build an effect that deals the same amount of damage to one target."""
    def effect(state, item):
        return deal_damage(state, item.targets[0], amount)
    return effect


def _counter_effect(state, item):
    return counter_spell(state, item.targets[0])


def _giant_growth(state, item):
    return pump(state, item.targets[0], 3, 3)


def _unsummon(state, item):
    return return_to_hand(state, item.targets[0])


def _healing_salve(state, item):
    # The card says "choose one", and our build always uses the first mode,
    # which gains 3 life.
    return gain_life(state, item.targets[0], 3)


def _raise_dead(state, item):
    """Return a creature card from the graveyard of the controller to their hand."""
    owner = state.player(item.controller)
    card_id = item.targets[0]
    if card_id not in owner.graveyard:
        return []
    owner.graveyard.remove(card_id)
    owner.hand.append(card_id)
    return [change("RETURN_TO_HAND", target=card_id)]


def _swords_to_plowshares(state, item):
    """Exile the creature. Its controller then gains life equal to its power."""
    permanent = state.find_permanent(item.targets[0])
    if permanent is None:
        return []
    controller, power = permanent.controller, permanent.power
    return exile(state, permanent.card_id) + gain_life(state, controller, power)


SPELL_EFFECTS = {
    # Red burn
    "lightning_bolt": (ANY_TARGET, _damage_effect(3)),
    "shock": (ANY_TARGET, _damage_effect(2)),
    "searing_spear": (ANY_TARGET, _damage_effect(3)),
    "incinerate": (ANY_TARGET, _damage_effect(3)),
    "lava_spike": (PLAYER, _damage_effect(3)),
    "flame_slash": (CREATURE, _damage_effect(4)),
    # Blue permission and tempo
    "counterspell": (SPELL, _counter_effect),
    "cancel": (SPELL, _counter_effect),
    "negate": (NONCREATURE_SPELL, _counter_effect),
    "unsummon": (CREATURE, _unsummon),
    # Green
    "giant_growth": (CREATURE, _giant_growth),
    # White
    "healing_salve": (PLAYER, _healing_salve),
    "swords_to_plowshares": (CREATURE, _swords_to_plowshares),
    # Black
    "doom_blade": (NONBLACK_CREATURE, lambda state, item: destroy(state, item.targets[0])),
    "terror": (NONBLACK_NONARTIFACT_CREATURE, lambda state, item: destroy(state, item.targets[0])),
    "raise_dead": (OWN_GRAVEYARD_CREATURE, _raise_dead),
}


# --- Activated abilities ---------------------------------------------------

@dataclass(frozen=True)
class Ability:
    """One activated ability of a permanent (RFC Section 10.2.8).

    We do not list the mana abilities here. They never use the stack, and mana.py
    handles them when a client declares a payment.
    """

    description: str
    requires_tap: bool           # True when the cost has the tap symbol in it.
    mana_cost: dict = field(default_factory=dict)
    target_spec: str = NO_TARGET
    effect: object = None


def _millstone(state, item):
    """The target player mills 2, so the top two library cards go to their graveyard."""
    target = state.player(item.targets[0])
    milled = []
    for _ in range(2):
        if target.library:
            card_id = target.library.pop(0)
            target.graveyard.append(card_id)
            milled.append(card_id)
    return [change("MILL", target=item.targets[0], cards=milled)]


ABILITIES = {
    "prodigal_sorcerer": [Ability(
        description="Tap: Prodigal Sorcerer deals 1 damage to any target.",
        requires_tap=True, target_spec=ANY_TARGET,
        effect=lambda state, item: deal_damage(state, item.targets[0], 1),
    )],
    "royal_assassin": [Ability(
        description="Tap: Destroy target tapped creature.",
        requires_tap=True, target_spec=TAPPED_CREATURE,
        effect=lambda state, item: destroy(state, item.targets[0]),
    )],
    "rod_of_ruin": [Ability(
        description="{3}, Tap: Rod of Ruin deals 1 damage to any target.",
        requires_tap=True, mana_cost={cards.GENERIC: 3}, target_spec=ANY_TARGET,
        effect=lambda state, item: deal_damage(state, item.targets[0], 1),
    )],
    "millstone": [Ability(
        description="{2}, Tap: Target player mills 2 cards.",
        requires_tap=True, mana_cost={cards.GENERIC: 2}, target_spec=PLAYER,
        effect=_millstone,
    )],
}


# --- Triggered abilities ---------------------------------------------------

@dataclass(frozen=True)
class Trigger:
    """A triggered ability (RFC Section 8.6).

    When `target_spec` is not NO_TARGET, the server has to send TRIGGER_CHOICE to
    the controller so that they can pick a target, and this happens before the
    trigger goes on the stack. If there is no legal target, we throw the trigger
    away (RFC Section 8.6.4).
    """

    key: str
    description: str
    target_spec: str = NO_TARGET
    effect: object = None


def _gray_merchant(state, item):
    """Each opponent loses X life, where X is the devotion to black, and the
    controller gains that much life."""
    amount = item.payload.get("amount", 0)
    if amount <= 0:
        return []
    opponent = state.opponent_of(item.controller)
    return lose_life(state, opponent, amount) + gain_life(state, item.controller, amount)


def _gravedigger(state, item):
    return _raise_dead(state, item)


def _goblin_guide_attack(state, item):
    """The defending player reveals the top card, and takes it if it is a land."""
    defender = state.player(item.payload["defender"])
    if not defender.library:
        return [change("REVEAL", target=defender.player_id, cards=[])]
    revealed = defender.library[0]
    card = cards.lookup(revealed)
    changes = [change("REVEAL", target=defender.player_id, cards=[revealed])]
    if card is not None and card.is_land:
        defender.library.pop(0)
        defender.hand.append(revealed)
        changes.append(change("RETURN_TO_HAND", target=revealed))
    return changes


# The triggers that fire when a permanent enters the battlefield.
ENTER_BATTLEFIELD_TRIGGERS = {
    "gray_merchant": Trigger(
        key="gray_merchant",
        description="When Gray Merchant of Asphodel enters, each opponent loses X life "
                    "(X = your devotion to black). You gain that much life.",
        effect=_gray_merchant,
    ),
    "gravedigger": Trigger(
        key="gravedigger",
        description="When Gravedigger enters, return target creature card from your "
                    "graveyard to your hand.",
        target_spec=OWN_GRAVEYARD_CREATURE,
        effect=_gravedigger,
    ),
}

# The triggers that fire when a player declares a creature as an attacker.
ATTACK_TRIGGERS = {
    "goblin_guide": Trigger(
        key="goblin_guide",
        description="Whenever Goblin Guide attacks, defending player reveals the top card "
                    "of their library. If it is a land, they put it into their hand.",
        effect=_goblin_guide_attack,
    ),
}

ALL_TRIGGERS = {**ENTER_BATTLEFIELD_TRIGGERS, **ATTACK_TRIGGERS}


# --- Castability and target legality --------------------------------------

def target_spec_for_spell(card: cards.Card) -> str | None:
    """The target spec for casting `card`, or None if our build cannot cast it.

    A permanent such as a creature, an artifact or a land does not need an effect
    at all, because it only enters the battlefield when it resolves.
    """
    if card.base in SPELL_EFFECTS:
        return SPELL_EFFECTS[card.base][0]
    if card.is_permanent and not card.is_enchantment:
        return NO_TARGET
    return None


def spell_effect_for(card: cards.Card):
    """The effect function of a spell, or None if the spell only becomes a permanent."""
    entry = SPELL_EFFECTS.get(card.base)
    return entry[1] if entry else None


def abilities_of(card_id: str) -> list:
    return ABILITIES.get(cards.base_of(card_id) or "", [])


def is_legal_target(state, spec: str, target: str, controller: str) -> bool:
    """Is `target` a legal target right now for an effect with this spec?

    We call this when the player casts the spell and again when it resolves. A
    target that stopped being legal in between makes the item fizzle.
    """
    if spec == NO_TARGET:
        return False

    if spec == PLAYER:
        return state.is_player_id(target)

    if spec == ANY_TARGET:
        return state.is_player_id(target) or _is_creature(state, target)

    if spec == CREATURE:
        return _is_creature(state, target)

    if spec == TAPPED_CREATURE:
        permanent = state.find_permanent(target)
        return permanent is not None and permanent.is_creature and permanent.tapped

    if spec == NONBLACK_CREATURE:
        permanent = state.find_permanent(target)
        return permanent is not None and permanent.is_creature and permanent.card.color != "B"

    if spec == NONBLACK_NONARTIFACT_CREATURE:
        permanent = state.find_permanent(target)
        return (permanent is not None and permanent.is_creature
                and permanent.card.color != "B" and not permanent.card.is_artifact)

    if spec == SPELL:
        item = state.find_stack_item(target)
        return item is not None and item.item_type == "SPELL"

    if spec == NONCREATURE_SPELL:
        item = state.find_stack_item(target)
        if item is None or item.item_type != "SPELL":
            return False
        card = cards.lookup(item.source)
        return card is not None and not card.is_creature

    if spec == OWN_GRAVEYARD_CREATURE:
        if target not in state.player(controller).graveyard:
            return False
        card = cards.lookup(target)
        return card is not None and card.is_creature

    return False


def legal_targets_for(state, spec: str, controller: str) -> list:
    """Every legal target for a spec right now. We use this to fill in TRIGGER_CHOICE."""
    if spec == NO_TARGET:
        return []

    candidates = list(state.player_order)
    candidates += [p.card_id for p in state.all_permanents()]
    candidates += list(state.player(controller).graveyard)
    candidates += [item.stack_item_id for item in state.stack]

    seen, legal = set(), []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if is_legal_target(state, spec, candidate, controller):
            legal.append(candidate)
    return legal


def _is_creature(state, permanent_id: str) -> bool:
    permanent = state.find_permanent(permanent_id)
    return permanent is not None and permanent.is_creature
