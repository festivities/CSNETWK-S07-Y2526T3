"""
The fixed, pre-defined card catalog.

MTGNP 1.0 does not transfer card data over the wire (RFC Section 1): card costs,
types, power/toughness and ability text are pre-loaded by the server and by every
client from a shared out-of-band catalog.  The card IDs exchanged in PDUs are
keys into this catalog.

Our catalog is the master card list shipped in `data/`.  A card ID such as
`lightning_bolt_001` is a *card instance*: the part before the trailing number
("lightning_bolt") is the catalog key, and the number distinguishes the copies
of that card in the fixed set.
"""

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

# The five mana colors, plus generic mana, as used in cost dictionaries.
COLORS = ("W", "U", "B", "R", "G")
GENERIC = "generic"

# Where the shipped master card list lives (repository root / data / ...).
CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "MTGNP_MASTER-CARD-LIST.tsv"

# A card instance ID is the catalog key followed by "_" and a copy number.
_CARD_ID_PATTERN = re.compile(r"^(?P<base>[a-z_]+)_(?P<copy>\d+)$")


@dataclass(frozen=True)
class Card:
    """One entry in the fixed card set."""

    base: str            # Catalog key, e.g. "lightning_bolt".
    name: str            # Printed name, e.g. "Lightning Bolt".
    card_type: str       # "Land", "Instant", "Sorcery", "Creature", ...
    subtype: str
    color: str           # "W"/"U"/"B"/"R"/"G"/"C"
    cmc: int
    cost: dict           # {"R": 1, "generic": 1} -- omitted keys mean zero.
    power: int | None    # None for non-creatures.
    toughness: int | None
    copies: int          # How many copies of this card exist in the fixed set.
    effect_text: str

    # --- Type helpers (used all over the rules code) --------------------

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
        """Permanents stay on the battlefield once they resolve."""
        return not (self.is_instant or self.is_sorcery)

    @property
    def keywords(self) -> frozenset:
        return KEYWORDS.get(self.base, frozenset())

    def has(self, keyword: str) -> bool:
        return keyword in self.keywords


# --- Keyword abilities implemented by this build ---------------------------
#
# Listed explicitly rather than parsed out of the English effect text, so that
# it is obvious which keywords this build actually enforces.  Keywords printed
# on the cards but NOT implemented here (prowess, protection, hexproof, trample,
# kicker, madness, suspend, regenerate, illusion) are documented as limitations
# in the README.

HASTE = "haste"                  # May attack the turn it enters play.
FIRST_STRIKE = "first_strike"    # Deals damage in the First Strike Damage Step.
DOUBLE_STRIKE = "double_strike"  # Deals damage in both damage steps.
DEFENDER = "defender"            # Cannot be declared as an attacker.
FLYING = "flying"                # Blockable only by creatures with flying.
VIGILANCE = "vigilance"          # Does not tap when declared as an attacker.

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
# Activating a mana ability does not use the stack and needs no PDU of its own
# (RFC Section 7.5): the client declares the whole payment when casting, and the
# server taps these sources to cover it.  Each entry lists the mana symbols one
# tap of that permanent produces.

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
    """Load (once) and return the catalog, keyed by catalog base name."""
    global _catalog
    if _catalog:
        return _catalog

    path = path or CATALOG_PATH
    with open(path, newline="", encoding="utf-8-sig") as handle:
        # Row 1 of the file is a human-readable title, row 2 is the header.
        handle.readline()
        for row in csv.DictReader(handle, delimiter="\t"):
            card = _card_from_row(row)
            if card is not None:
                _catalog[card.base] = card

    if not _catalog:
        raise RuntimeError(f"No cards loaded from {path}")
    return _catalog


def _card_from_row(row: dict) -> Card | None:
    """Build a Card from one TSV row, or None if the row is blank."""
    base = (row.get("Card ID Base") or "").strip()
    if not base:
        return None

    # Colored requirements come from the per-color columns; everything else that
    # must be paid with any mana comes from the "Generic" column.
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
    """Power/toughness are printed as "-" for cards that are not creatures."""
    text = (value or "").strip()
    if not text or text == "-":
        return None
    try:
        return int(text)
    except ValueError:
        return None


# --- Card ID helpers -------------------------------------------------------

def base_of(card_id: str) -> str | None:
    """Return the catalog key for a card instance ID, or None if malformed.

    >>> base_of("lightning_bolt_001")
    'lightning_bolt'
    """
    match = _CARD_ID_PATTERN.match(card_id or "")
    return match.group("base") if match else None


def copy_number_of(card_id: str) -> int | None:
    """Return the copy number of a card instance ID, or None if malformed."""
    match = _CARD_ID_PATTERN.match(card_id or "")
    return int(match.group("copy")) if match else None


def lookup(card_id: str) -> Card | None:
    """Return the Card for a card instance ID, or None if it is not legal."""
    base = base_of(card_id)
    return load_catalog().get(base) if base else None


def name_of(card_id: str) -> str:
    """Human-readable name for a card instance ID (falls back to the raw ID)."""
    card = lookup(card_id)
    return card.name if card else card_id


def is_legal_card_id(card_id: str) -> bool:
    """True if the ID names a real copy of a real card in the fixed set.

    Both halves matter: the catalog key must exist, and the copy number must be
    within the number of copies the fixed set actually contains.
    """
    card = lookup(card_id)
    if card is None:
        return False
    copy_number = copy_number_of(card_id)
    return copy_number is not None and 1 <= copy_number <= card.copies
