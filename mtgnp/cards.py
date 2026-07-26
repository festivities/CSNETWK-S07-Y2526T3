"""
The fixed card catalog.

MTGNP 1.0 does not send card data over the wire (RFC Section 1). The server and
every client load the card costs, types, power and toughness, and ability text
from a shared catalog before the game starts. The card IDs inside the PDUs are
only keys into that catalog.

Our catalog is the master card list in `data/`. A card ID such as
`lightning_bolt_001` names one card instance. The part before the last number,
"lightning_bolt", is the catalog key, and the number tells the copies of that
card in the fixed set apart.
"""

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

# The five mana colors and generic mana, as we use them in cost dictionaries.
COLORS = ("W", "U", "B", "R", "G")
GENERIC = "generic"

# Where we keep the master card list, in the data folder of the project.
CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "MTGNP_MASTER-CARD-LIST.tsv"

# A card instance ID is the catalog key, then "_", then a copy number.
_CARD_ID_PATTERN = re.compile(r"^(?P<base>[a-z_]+)_(?P<copy>\d+)$")


@dataclass(frozen=True)
class Card:
    """One entry in the fixed card set."""

    base: str            # Catalog key, for example "lightning_bolt".
    name: str            # Printed name, for example "Lightning Bolt".
    card_type: str       # "Land", "Instant", "Sorcery", "Creature", and so on.
    subtype: str
    color: str           # "W", "U", "B", "R", "G" or "C".
    cmc: int
    cost: dict           # {"R": 1, "generic": 1}. A missing key means zero.
    power: int | None    # None for cards that are not creatures.
    toughness: int | None
    copies: int          # How many copies of this card the fixed set contains.
    effect_text: str

    # --- Type helpers, which the rules code uses everywhere --------------

    @property
    def is_land(self) -> bool:
        return "Land" in self.card_type

    @property
    def is_creature(self) -> bool:
        return "Creature" in self.card_type

    @property
    def is_instant(self) -> bool:
        return self.card_type == "Instant"

    @property
    def is_sorcery(self) -> bool:
        return self.card_type == "Sorcery"

    @property
    def is_artifact(self) -> bool:
        return "Artifact" in self.card_type

    @property
    def is_enchantment(self) -> bool:
        return "Enchantment" in self.card_type

    @property
    def is_permanent(self) -> bool:
        """A permanent stays on the battlefield after it resolves."""
        return not (self.is_instant or self.is_sorcery)

    @property
    def keywords(self) -> frozenset:
        return KEYWORDS.get(self.base, frozenset())

    def has(self, keyword: str) -> bool:
        return keyword in self.keywords


# --- The keyword abilities we implemented ----------------------------------
#
# We list these here instead of reading them out of the English effect text, so
# that it is clear which keywords our build really enforces. Some keywords are
# printed on the cards but are not implemented here, such as prowess,
# protection, hexproof, trample, kicker, madness, suspend, regenerate, and
# illusion. The README lists them as limitations.

HASTE = "haste"                  # Can attack on the turn it enters play.
FIRST_STRIKE = "first_strike"    # Deals damage in the First Strike Damage Step.
DOUBLE_STRIKE = "double_strike"  # Deals damage in both damage steps.
DEFENDER = "defender"            # Cannot be declared as an attacker.
FLYING = "flying"                # Only creatures with flying can block it.
VIGILANCE = "vigilance"          # Does not tap when it is declared as an attacker.

KEYWORDS = {
    "goblin_guide": frozenset({HASTE}),
    "monastery_swiftspear": frozenset({HASTE}),
    "serra_angel": frozenset({FLYING, VIGILANCE}),
    "air_elemental": frozenset({FLYING}),
    "ornithopter": frozenset({FLYING}),
    "white_knight": frozenset({FIRST_STRIKE}),
    "black_knight": frozenset({FIRST_STRIKE}),
    "wall_of_stone": frozenset({DEFENDER}),
}


# --- Mana sources -----------------------------------------------------------
#
# A mana ability does not use the stack and does not need a PDU of its own (RFC
# Section 7.5). The client declares the whole payment when it casts a spell, and
# the server taps these sources to cover that payment. Each entry lists the mana
# symbols that one tap of the permanent produces.

MANA_SOURCES = {
    "mountain": ("R",),
    "forest": ("G",),
    "plains": ("W",),
    "island": ("U",),
    "swamp": ("B",),
    "llanowar_elves": ("G",),
    "elvish_mystic": ("G",),
    "sol_ring": ("C", "C"),
}


# --- Catalog loading -------------------------------------------------------

_catalog: dict[str, Card] = {}


def load_catalog(path: Path | None = None) -> dict[str, Card]:
    """Load the catalog once and return it, keyed by the catalog base name."""
    global _catalog
    if _catalog:
        return _catalog

    path = path or CATALOG_PATH
    with open(path, newline="", encoding="utf-8-sig") as handle:
        # The first row of the file is a title for readers, and the second row
        # is the actual header, so we skip the first one.
        handle.readline()
        for row in csv.DictReader(handle, delimiter="\t"):
            card = _card_from_row(row)
            if card is not None:
                _catalog[card.base] = card

    if not _catalog:
        raise RuntimeError(f"No cards loaded from {path}")
    return _catalog


def _card_from_row(row: dict) -> Card | None:
    """Build a Card from one TSV row, or return None if the row is blank."""
    base = (row.get("Card ID Base") or "").strip()
    if not base:
        return None

    # The colored requirements come from the per-color columns. The rest of the
    # cost, which the player can pay with any mana, comes from the "Generic"
    # column.
    cost = {color: _as_int(row.get(color)) for color in COLORS}
    cost = {color: amount for color, amount in cost.items() if amount > 0}
    generic = _as_int(row.get("Generic"))
    if generic > 0:
        cost[GENERIC] = generic

    return Card(
        base=base,
        name=(row.get("Card Name") or "").strip(),
        card_type=(row.get("Card Type") or "").strip(),
        subtype=(row.get("Subtype") or "").strip(),
        color=(row.get("Color") or "").strip(),
        cmc=_as_int(row.get("CMC")),
        cost=cost,
        power=_as_optional_int(row.get("Power")),
        toughness=_as_optional_int(row.get("Toughness")),
        copies=_as_int(row.get("Copies in Set")),
        effect_text=(row.get("Simplified Effect") or "").strip(),
    )


def _as_int(value: str | None) -> int:
    try:
        return int((value or "0").strip())
    except ValueError:
        return 0


def _as_optional_int(value: str | None) -> int | None:
    """The list prints power and toughness as "-" for cards that are not creatures."""
    text = (value or "").strip()
    if not text or text == "-":
        return None
    try:
        return int(text)
    except ValueError:
        return None


# --- Card ID helpers -------------------------------------------------------

def base_of(card_id: str) -> str | None:
    """Return the catalog key of a card instance ID, or None if the ID is wrong.

    >>> base_of("lightning_bolt_001")
    'lightning_bolt'
    """
    match = _CARD_ID_PATTERN.match(card_id or "")
    return match.group("base") if match else None


def copy_number_of(card_id: str) -> int | None:
    """Return the copy number of a card instance ID, or None if the ID is wrong."""
    match = _CARD_ID_PATTERN.match(card_id or "")
    return int(match.group("copy")) if match else None


def lookup(card_id: str) -> Card | None:
    """Return the Card for a card instance ID, or None if the ID is not legal."""
    base = base_of(card_id)
    return load_catalog().get(base) if base else None


def name_of(card_id: str) -> str:
    """The printed name of a card instance ID. If we cannot find the card, we
    return the raw ID instead."""
    card = lookup(card_id)
    return card.name if card else card_id


def is_legal_card_id(card_id: str) -> bool:
    """True if the ID names a real copy of a real card in the fixed set.

    Both parts of the ID matter. The catalog key has to exist, and the copy
    number has to be inside the number of copies that the fixed set contains.
    """
    card = lookup(card_id)
    if card is None:
        return False
    copy_number = copy_number_of(card_id)
    return copy_number is not None and 1 <= copy_number <= card.copies
