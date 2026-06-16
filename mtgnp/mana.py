"""
Mana payment: validating a declared payment and tapping the sources that fund it.

MTGNP 1.0 handles mana production implicitly (RFC Section 7.5).  Activating a
mana ability does not use the stack and has no PDU of its own.  Instead the
client declares the full `mana_payment` inside CAST_SPELL or ACTIVATE_ABILITY,
and the server deducts the corresponding mana sources in a single atomic step.
If the declared payment cannot be satisfied, the server answers with ERROR code
INSUFFICIENT_MANA.

Payment format (RFC Section 10.2.7)
-----------------------------------
Colored mana uses the color as the key; generic mana uses the key "X":

    { "R": 1 }            Lightning Bolt   -- one red
    { "R": 1, "X": 1 }    Searing Spear    -- one red plus one generic
    { "X": 1 }            Sol Ring         -- one generic

We also accept "generic" as a synonym for "X", and "C" as an explicit
colorless requirement, so that a slightly different client still interoperates.
"""

from collections import Counter

from . import cards

# Keys that name a specific kind of mana that must be matched exactly.
SPECIFIC_MANA_KEYS = ("W", "U", "B", "R", "G", "C")
# Keys that mean "this much mana of any kind".
GENERIC_KEYS = ("X", "generic")


class InsufficientMana(Exception):
    """The declared payment does not match the cost, or cannot be produced."""


def cost_as_payment_from_cost(cost: dict) -> dict:
    """Turn a cost dictionary into the `mana_payment` that pays it exactly.

    Generic mana is reported under the key "X", as the RFC specifies.
    """
    payment = {symbol: amount for symbol, amount in (cost or {}).items()
               if symbol != cards.GENERIC}
    generic = (cost or {}).get(cards.GENERIC, 0)
    if generic:
        payment["X"] = generic
    return payment


def cost_as_payment(card: cards.Card) -> dict:
    """The canonical `mana_payment` for a card's printed cost.

    The client uses this to fill in CAST_SPELL automatically, so a human never
    has to hand-compute a payment.
    """
    return cost_as_payment_from_cost(card.cost)


def format_cost(cost: dict) -> str:
    """Render a cost the way it is printed on a card, e.g. {1}{R} or {U}{U}."""
    specific = Counter({s: n for s, n in (cost or {}).items() if s != cards.GENERIC})
    return _describe(specific, (cost or {}).get(cards.GENERIC, 0))


def normalise(payment: dict | None) -> tuple[Counter, int]:
    """Split a declared payment into (specific mana counts, generic amount)."""
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
    """Verify the declared payment is exactly the cost that must be paid.

    Raises InsufficientMana with an explanatory message if it is not.
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
    """Tap enough of `player`'s mana sources to fund `payment`.

    Returns the permanents that were tapped.  Nothing is mutated unless the whole
    payment can be funded, which keeps the deduction atomic as the RFC requires.
    Raises InsufficientMana otherwise.
    """
    specific, generic = normalise(payment)
    sources = _available_sources(player)

    pool = Counter()       # Mana produced so far but not yet spent.
    to_tap = []            # Sources we have decided to tap.

    def tap_a_source_producing(symbol: str | None) -> bool:
        """Tap an untapped source that makes `symbol` (or any, if None)."""
        for source in sources:
            if source in to_tap:
                continue
            output = cards.MANA_SOURCES[cards.base_of(source.card_id)]
            if symbol is None or symbol in output:
                to_tap.append(source)
                pool.update(output)
                return True
        return False

    # Pay the specific (colored or colorless) requirements first: they are the
    # constrained ones, so satisfying them before generic avoids wasting a
    # source that was the only producer of a needed color.
    for symbol in SPECIFIC_MANA_KEYS:
        for _ in range(specific.get(symbol, 0)):
            if pool[symbol] == 0 and not tap_a_source_producing(symbol):
                raise InsufficientMana(f"No untapped source can produce {{{symbol}}}")
            pool[symbol] -= 1

    # Generic mana can be paid with anything, including mana already in the pool
    # (for example the second {C} from a Sol Ring).
    for _ in range(generic):
        spare = next((symbol for symbol, count in pool.items() if count > 0), None)
        if spare is None:
            if not tap_a_source_producing(None):
                raise InsufficientMana("Not enough untapped mana sources for the generic cost")
            spare = next(symbol for symbol, count in pool.items() if count > 0)
        pool[spare] -= 1

    # Every requirement is funded, so commit: tap the sources we selected.
    for source in to_tap:
        source.tapped = True
    return to_tap


def available_mana(player) -> Counter:
    """All mana `player` could produce right now (for client-side display)."""
    total = Counter()
    for source in _available_sources(player):
        total.update(cards.MANA_SOURCES[cards.base_of(source.card_id)])
    return total


def _available_sources(player) -> list:
    """Untapped permanents that can produce mana.

    A creature with summoning sickness may not activate an ability with the tap
    symbol in its cost (RFC Section 3), which rules out Llanowar Elves and
    Elvish Mystic on the turn they arrive.  Lands and artifacts are unaffected.
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
