"""
Mana payment: checking a declared payment and tapping the sources that pay for it.

MTGNP 1.0 handles mana production in the background (RFC Section 7.5). A mana
ability does not use the stack and does not have a PDU of its own. The client
declares the whole `mana_payment` inside CAST_SPELL or ACTIVATE_ABILITY instead,
and the server taps the matching mana sources in one step. If the server cannot
pay the declared payment, it answers with the ERROR code INSUFFICIENT_MANA.

Payment format (RFC Section 10.2.7)
-----------------------------------
Colored mana uses the color as the key, and generic mana uses the key "X":

    { "R": 1 }            Lightning Bolt, which needs one red
    { "R": 1, "X": 1 }    Searing Spear, which needs one red and one generic
    { "X": 1 }            Sol Ring, which needs one generic

We also accept "generic" as another name for "X", and "C" as a colorless
requirement, so that a client written a little differently still works with us.
"""

from collections import Counter

from . import cards

# Keys that name one kind of mana, which the payment has to match exactly.
SPECIFIC_MANA_KEYS = ("W", "U", "B", "R", "G", "C")
# Keys that mean "this much mana of any kind".
GENERIC_KEYS = ("X", "generic")


class InsufficientMana(Exception):
    """The declared payment does not match the cost, or the player cannot pay it."""


def cost_as_payment_from_cost(cost: dict) -> dict:
    """Turn a cost dictionary into the `mana_payment` that pays it exactly.

    We report generic mana under the key "X", the way the RFC asks for it.
    """
    payment = {symbol: amount for symbol, amount in (cost or {}).items()
               if symbol != cards.GENERIC}
    generic = (cost or {}).get(cards.GENERIC, 0)
    if generic:
        payment["X"] = generic
    return payment


def cost_as_payment(card: cards.Card) -> dict:
    """The standard `mana_payment` for the printed cost of a card.

    The client uses this to fill in CAST_SPELL on its own, so the player never
    has to work out a payment by hand.
    """
    return cost_as_payment_from_cost(card.cost)


def format_cost(cost: dict) -> str:
    """Show a cost the way a card prints it, for example {1}{R} or {U}{U}."""
    specific = Counter({s: n for s, n in (cost or {}).items() if s != cards.GENERIC})
    return _describe(specific, (cost or {}).get(cards.GENERIC, 0))


def normalise(payment: dict | None) -> tuple[Counter, int]:
    """Split a declared payment into the specific mana counts and the generic amount."""
    payment = payment or {}
    specific = Counter()
    generic = 0
    for key, amount in payment.items():
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            raise InsufficientMana(f"Mana payment amount for '{key}' is not an integer")
        if amount < 0:
            raise InsufficientMana(f"Mana payment amount for '{key}' is negative")
        if key in SPECIFIC_MANA_KEYS:
            specific[key] += amount
        elif key in GENERIC_KEYS:
            generic += amount
        else:
            raise InsufficientMana(f"Unknown mana symbol '{key}' in mana_payment")
    return specific, generic


def check_matches_cost(payment: dict | None, cost: dict) -> None:
    """Check that the declared payment is exactly the cost the player has to pay.

    If it is not, this raises InsufficientMana with a message that explains why.
    """
    declared_specific, declared_generic = normalise(payment)
    required_specific = Counter({c: n for c, n in cost.items() if c != cards.GENERIC})
    required_generic = cost.get(cards.GENERIC, 0)

    if declared_specific != required_specific or declared_generic != required_generic:
        raise InsufficientMana(
            f"Declared payment {_describe(declared_specific, declared_generic)} "
            f"does not match the cost {_describe(required_specific, required_generic)}"
        )


def pay(player, payment: dict | None) -> list:
    """Tap enough mana sources of `player` to pay for `payment`.

    This returns the permanents that we tapped. We do not change anything until
    we know that the player can pay the whole payment, so the payment happens in
    one step the way the RFC asks for. If the player cannot pay, this raises
    InsufficientMana and nothing is tapped.
    """
    specific, generic = normalise(payment)
    sources = _available_sources(player)

    pool = Counter()       # Mana we produced so far but have not spent yet.
    to_tap = []            # The sources we decided to tap.

    def tap_a_source_producing(symbol: str | None) -> bool:
        """Tap an untapped source that makes `symbol`, or any source if None."""
        for source in sources:
            if source in to_tap:
                continue
            output = cards.MANA_SOURCES[cards.base_of(source.card_id)]
            if symbol is None or symbol in output:
                to_tap.append(source)
                pool.update(output)
                return True
        return False

    # We pay the specific colored and colorless requirements first, because they
    # are the harder ones. If we paid the generic part first, we could waste the
    # only source that produces a color we still need.
    for symbol in SPECIFIC_MANA_KEYS:
        for _ in range(specific.get(symbol, 0)):
            if pool[symbol] == 0 and not tap_a_source_producing(symbol):
                raise InsufficientMana(f"No untapped source can produce {{{symbol}}}")
            pool[symbol] -= 1

    # The player can pay generic mana with anything, including mana that is
    # already in the pool, such as the second {C} from a Sol Ring.
    for _ in range(generic):
        spare = next((symbol for symbol, count in pool.items() if count > 0), None)
        if spare is None:
            if not tap_a_source_producing(None):
                raise InsufficientMana("Not enough untapped mana sources for the generic cost")
            spare = next(symbol for symbol, count in pool.items() if count > 0)
        pool[spare] -= 1

    # We can now pay every requirement, so we tap the sources that we chose.
    for source in to_tap:
        source.tapped = True
    return to_tap


def available_mana(player) -> Counter:
    """All the mana that `player` could make right now, which the client displays."""
    total = Counter()
    for source in _available_sources(player):
        total.update(cards.MANA_SOURCES[cards.base_of(source.card_id)])
    return total


def _available_sources(player) -> list:
    """The untapped permanents that can make mana.

    A creature with summoning sickness cannot activate an ability that has the
    tap symbol in its cost (RFC Section 3). This means that Llanowar Elves and
    Elvish Mystic cannot make mana on the turn they enter play. Lands and
    artifacts do not have this problem.
    """
    usable = []
    for permanent in player.battlefield:
        if permanent.tapped:
            continue
        if cards.base_of(permanent.card_id) not in cards.MANA_SOURCES:
            continue
        if permanent.is_creature and permanent.summoning_sick:
            continue
        usable.append(permanent)
    return usable


def _describe(specific: Counter, generic: int) -> str:
    parts = [f"{{{symbol}}}" * count for symbol, count in sorted(specific.items()) if count]
    if generic:
        parts.insert(0, f"{{{generic}}}")
    return "".join(parts) or "{0}"
