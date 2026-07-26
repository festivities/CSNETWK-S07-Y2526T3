"""
The authoritative game state, and how we filter it for each player.

The server holds the only authoritative copy of the Game State (RFC Section
4.2). The clients never work out game outcomes on their own. They only display
what `GameState.visible_state()` gives them.

Hidden information
------------------
`visible_state(viewer)` returns the Visible State for one player. That player
sees their own hand card by card, while the opponent's hand becomes a count
only (RFC Section 3, "Visible State"). Libraries are always counts only. This
filtering is the one place where we keep hidden information hidden.

A note on field names
---------------------
The prose examples in the RFC and the PDU schemas in Section 10.2 do not agree
on three field names. We follow Section 10.2, because Section 5.3 says it is the
authority for field names. So `hand` is an object keyed by player and not a bare
array, the land flag is `land_played_this_turn` and not `land_played`, and the
creature flag is `summoning_sick` and not `summoning_sickness`. Our client
accepts both spellings, so it still works with either reading.
"""

from dataclasses import dataclass, field

from . import cards, protocol

STARTING_LIFE = 20
STARTING_HAND_SIZE = 7
MAX_HAND_SIZE = 7  # Checked at the Cleanup Step (RFC Section 7.8).


@dataclass
class Permanent:
    """A card on the battlefield.

    We also use `card_id` as the ID of the permanent. The RFC says that each
    permanent ID matches the card instance ID it came from (RFC Section 10.2.2),
    and a card instance can only be in one zone at a time.
    """

    card_id: str
    controller: str
    tapped: bool = False
    damage: int = 0
    # A creature that entered play this turn has summoning sickness until the
    # next Untap Step of its controller (RFC Section 3).
    summoning_sick: bool = True
    # Power and toughness bonuses that last until end of turn. We clear these
    # at the Cleanup Step.
    power_bonus: int = 0
    toughness_bonus: int = 0

    @property
    def card(self) -> cards.Card:
        return cards.lookup(self.card_id)

    @property
    def power(self) -> int:
        return (self.card.power or 0) + self.power_bonus

    @property
    def toughness(self) -> int:
        return (self.card.toughness or 0) + self.toughness_bonus

    @property
    def is_creature(self) -> bool:
        return self.card.is_creature

    def has(self, keyword: str) -> bool:
        return self.card.has(keyword)

    @property
    def has_lethal_damage(self) -> bool:
        """A creature dies when its damage reaches or passes its toughness."""
        return self.is_creature and self.damage >= self.toughness

    def to_wire(self) -> dict:
        """One battlefield entry for GAME_STATE_UPDATE (RFC Section 10.2.2).

        Cards that are not creatures only carry `id` and `tapped`. Creatures also
        carry their damage, their current power and toughness, and their
        summoning sickness flag.
        """
        entry = {"id": self.card_id, "tapped": self.tapped}
        if self.is_creature:
            entry["damage"] = self.damage
            entry["power"] = self.power
            entry["toughness"] = self.toughness
            entry["summoning_sick"] = self.summoning_sick
        return entry


@dataclass
class StackItem:
    """One entry on the stack (RFC Section 8.3)."""

    stack_item_id: str
    item_type: str      # SPELL, ABILITY or TRIGGER_ABILITY.
    source: str         # The card or permanent that put this item on the stack.
    controller: str
    targets: list = field(default_factory=list)
    # Which activated ability of the source permanent this is (ACTIVATE_ABILITY).
    ability_index: int | None = None
    # For a triggered ability, which trigger in effects.TRIGGERS we should run.
    trigger_key: str | None = None
    # Extra data that only one effect needs, such as the amount of life that
    # Gray Merchant drains.
    payload: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        """The body of STACK_PUSH, and one element of the `stack` array (RFC 10.2.9)."""
        return {
            "stack_item_id": self.stack_item_id,
            "item_type": self.item_type,
            "source": self.source,
            "targets": list(self.targets),
            "controller": self.controller,
        }


@dataclass
class Player:
    """The zones and the counters of one player."""

    player_id: str
    deck_list: list = field(default_factory=list)
    library: list = field(default_factory=list)     # Index 0 is the top card.
    hand: list = field(default_factory=list)
    graveyard: list = field(default_factory=list)   # Index 0 went in first.
    exile: list = field(default_factory=list)
    battlefield: list = field(default_factory=list)  # list[Permanent]
    life: int = STARTING_LIFE
    mulligans: int = 0
    has_kept: bool = False
    land_played_this_turn: bool = False

    # --- Zone helpers ----------------------------------------------------

    def draw(self) -> str | None:
        """Move the top card of the library to the hand.

        This returns None when the library is empty. A player who has to draw
        from an empty library loses the game (RFC Section 6.5), but the caller is
        the one that checks for this.
        """
        if not self.library:
            return None
        card_id = self.library.pop(0)
        self.hand.append(card_id)
        return card_id

    def creatures(self) -> list:
        return [p for p in self.battlefield if p.is_creature]

    def find_permanent(self, permanent_id: str) -> Permanent | None:
        return next((p for p in self.battlefield if p.card_id == permanent_id), None)


class GameOver(Exception):
    """We raise this as soon as a player wins or loses.

    We wrote the turn loop as plain sequential code, so an exception is the
    clearest way to stop a game in progress the moment a player loses. It also
    makes sure that we do not open any more priority windows (RFC Section 8.4).
    """

    def __init__(self, winner_id: str | None, loser_id: str | None, reason: str):
        super().__init__(f"{reason}: winner={winner_id} loser={loser_id}")
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.reason = reason


class GameState:
    """The only authoritative copy of all the game information."""

    def __init__(self, player_ids: list):
        # `player_order[0]` goes first. The coin flip in GAME_SETUP sets this.
        self.player_order = list(player_ids)
        self.players = {pid: Player(pid) for pid in player_ids}

        self.turn = 0
        self.phase = protocol.LOBBY
        self.active_player = self.player_order[0]
        self.priority_holder: str | None = None
        self.stack: list = []

        # The combat records for the current turn. See combat.py.
        self.attackers: dict = {}       # attacker permanent id -> defending player id
        self.blocks: dict = {}          # attacker permanent id -> [blocker ids]
        self.damage_order: dict = {}    # attacker permanent id -> [blocker ids]

        # Counters that only go up, for the IDs that the server assigns.
        self._stack_counter = 0
        self._trigger_counter = 0

    # --- Players ---------------------------------------------------------

    def opponent_of(self, player_id: str) -> str:
        return next(pid for pid in self.player_order if pid != player_id)

    @property
    def non_active_player(self) -> str:
        return self.opponent_of(self.active_player)

    def player(self, player_id: str) -> Player:
        return self.players[player_id]

    def is_player_id(self, candidate: str) -> bool:
        return candidate in self.players

    # --- Server-assigned IDs ---------------------------------------------

    def next_stack_item_id(self) -> str:
        self._stack_counter += 1
        return f"stk_{self._stack_counter:02d}"

    def next_trigger_id(self) -> str:
        self._trigger_counter += 1
        return f"trg_{self._trigger_counter:02d}"

    # --- Battlefield lookups ---------------------------------------------

    def all_permanents(self) -> list:
        return [p for pid in self.player_order for p in self.players[pid].battlefield]

    def find_permanent(self, permanent_id: str) -> Permanent | None:
        """Find a permanent anywhere on the battlefield, no matter who controls it."""
        for pid in self.player_order:
            found = self.players[pid].find_permanent(permanent_id)
            if found is not None:
                return found
        return None

    def find_stack_item(self, stack_item_id: str) -> StackItem | None:
        return next((i for i in self.stack if i.stack_item_id == stack_item_id), None)

    # --- Turn bookkeeping ------------------------------------------------

    def clear_combat(self) -> None:
        """Throw away the attacker and blocker assignments (RFC Section 9.8)."""
        self.attackers.clear()
        self.blocks.clear()
        self.damage_order.clear()

    # --- Visible State ---------------------------------------------------

    def visible_state(self, viewer: str) -> dict:
        """The Visible State for one player, which is the payload of GAME_STATE_UPDATE.

        The viewer sees their own hand in full. The hand of the opponent becomes
        a count, and both libraries become counts. Everything else is public,
        such as the battlefield, the graveyards, the stack, and the life totals.
        """
        opponent = self.opponent_of(viewer)
        return {
            "turn": self.turn,
            "active_player": self.active_player,
            "phase": self.phase,
            "priority_holder": self.priority_holder,
            "life_totals": {pid: self.players[pid].life for pid in self.player_order},
            "stack": [item.to_wire() for item in self.stack],
            "battlefield": {
                pid: [p.to_wire() for p in self.players[pid].battlefield]
                for pid in self.player_order
            },
            "graveyard": {pid: list(self.players[pid].graveyard) for pid in self.player_order},
            # The full contents of the own hand, and only a count for the opponent.
            "hand": {viewer: list(self.players[viewer].hand)},
            "hand_counts": {opponent: len(self.players[opponent].hand)},
            "library_counts": {pid: len(self.players[pid].library) for pid in self.player_order},
            "land_played_this_turn": self.players[self.active_player].land_played_this_turn,
            # This key holds the current combat assignments. Section 10.2 does
            # not name it, so we added it. All of it is public information, and
            # without it the attacking player has no way to learn how we blocked
            # their attackers, which they need before they can send
            # ASSIGN_DAMAGE_ORDER. We did not rename or remove any RFC field, so
            # a stricter client can just ignore this key.
            "combat": {
                "attackers": dict(self.attackers),
                "blocks": {a: list(b) for a, b in self.blocks.items()},
                "damage_order": {a: list(b) for a, b in self.damage_order.items()},
            },
        }

    def lobby_state(self, ready_count: int, waiting_for: list) -> dict:
        """The lobby version of the GAME_STATE_UPDATE payload (RFC 10.2.2)."""
        return {
            "phase": self.phase,
            "players_ready": ready_count,
            "waiting_for": list(waiting_for),
        }
