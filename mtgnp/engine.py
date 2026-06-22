"""
Shared engine plumbing used by every rules module.

`GameEngine` owns the pieces that the lifecycle, turn, priority and combat code
all need: the server's sequence-number counter, the helpers that send and
broadcast PDUs, the blocking "wait for this player to act" helper that validates
seq_num tokens, state-based actions, and putting triggered abilities on the stack.

Threading model
---------------
One reader thread per client socket pushes `(connection, pdu)` pairs onto
`inbox`.  A single game thread runs the rules and is the only thread that touches
GameState, so the rules code needs no locking at all.  PING is answered directly
by the reader thread (it is stateless) and never reaches the inbox.

Because only one thread runs the rules, the turn structure can be written as
ordinary straight-line code that blocks while waiting for a player -- far easier
to read than a callback-driven state machine.

The inbox carries the *connection* rather than a player id because during the
LOBBY state the server does not yet know what the clients will call themselves:
`player_id` is chosen by the client in its PLAYER_READY PDU (RFC Section 6.2).
"""

import queue
import time

from . import cards, effects, protocol
from .state import GameOver, StackItem

# Pushed onto the inbox by a reader thread when its client's socket closes.
DISCONNECTED = "__disconnected__"

# Default response deadline advertised in PRIORITY_GRANT.  The RFC's examples use
# 60000; we default higher so a human demonstrating the protocol by hand is not
# timed out mid-turn.  Override with the server's --time-limit-ms flag.
DEFAULT_TIME_LIMIT_MS = 300000


class GameEngine:
    """Rules-facing view of the server: state plus the ability to talk to clients."""

    def __init__(self, logger, time_limit_ms: int = DEFAULT_TIME_LIMIT_MS):
        self.logger = logger
        self.time_limit_ms = time_limit_ms
        self.inbox: queue.Queue = queue.Queue()

        # Both are set once the LOBBY state has produced two ready players.
        self.state = None
        self.connections: dict = {}     # player_id -> ClientConnection

        self._seq = 0                   # The server's own PDU counter.

    # --- Sequence numbers and sending ------------------------------------

    def next_seq(self) -> int:
        """The server's monotonically increasing counter (RFC Section 5.4).

        The RFC explicitly allows "a simple counter that increments with each PDU
        sent", which is what we use.  It keeps counting across games in the same
        session, since the TCP connections are reused.
        """
        self._seq += 1
        return self._seq

    def send_to(self, connection, pdu: dict, seq: int | None = None) -> int:
        """Send one PDU over one connection and return the seq_num it carried."""
        seq = self.next_seq() if seq is None else seq
        # Build the PDU with `type` and `seq_num` first: both are REQUIRED in
        # every PDU (RFC Section 5.4), and it makes verbose output easy to scan.
        body = {key: value for key, value in pdu.items() if key != "type"}
        framed = {"type": pdu["type"], "seq_num": seq, **body}
        connection.send(framed)
        return seq

    def send(self, player_id: str, pdu: dict, seq: int | None = None) -> int:
        """Send one PDU to one player by id."""
        connection = self.connections.get(player_id)
        if connection is None:
            return seq if seq is not None else self.next_seq()
        return self.send_to(connection, pdu, seq=seq)

    def broadcast(self, pdu: dict) -> int:
        """Send one PDU to both players under a single seq_num (an S->ALL PDU)."""
        seq = self.next_seq()
        for player_id in self.state.player_order:
            self.send(player_id, pdu, seq=seq)
        return seq

    # --- GAME_STATE_UPDATE ------------------------------------------------

    def send_state_update(self, player_id: str) -> int:
        """Send one player their personalised view of the game state."""
        return self.send(player_id, {
            "type": protocol.GAME_STATE_UPDATE,
            "state": self.state.visible_state(player_id),
        })

    def broadcast_state_update(self) -> dict:
        """Send every player their own filtered state.

        Each player gets a separate PDU with its own seq_num, because the payloads
        differ: hidden information is filtered per recipient (RFC Section 4.2).
        Returns {player_id: seq_num}, so a caller can use a player's own update as
        that player's seq_num token (needed for MULLIGAN_CHOICE and DISCARD).
        """
        return {pid: self.send_state_update(pid) for pid in self.state.player_order}

    # --- Errors (RFC Section 11) -----------------------------------------

    def send_error_to(self, connection, code: str, message: str, rejected: dict | None = None) -> None:
        """Report an illegal or invalid PDU without changing the game state.

        The ERROR PDU echoes the seq_num of the rejected action when one is
        available (RFC Section 10.2.23) and carries a copy of that action, so the
        client can tell its user exactly what was refused.  An illegal action
        never disconnects a client.
        """
        rejected = rejected or {}
        seq = rejected.get("seq_num")
        self.send_to(connection, {
            "type": protocol.ERROR,
            "code": code,
            "message": message,
            "rejected_action": rejected,
        }, seq=seq if isinstance(seq, int) else None)

    def send_error(self, player_id: str, code: str, message: str, rejected: dict | None = None) -> None:
        connection = self.connections.get(player_id)
        if connection is not None:
            self.send_error_to(connection, code, message, rejected)

    # --- Priority ---------------------------------------------------------

    def grant_priority(self, player_id: str) -> int:
        """Give a player priority and return the seq_num that is now their token."""
        self.state.priority_holder = player_id
        return self.send(player_id, {
            "type": protocol.PRIORITY_GRANT,
            "player_id": player_id,
            "time_limit_ms": self.time_limit_ms,
        })

    # --- Waiting for client actions --------------------------------------

    def next_client_pdu(self, deadline: float) -> tuple:
        """Return (player_id, pdu) for the next in-game client PDU.

        Handles the three things that can happen regardless of whose turn it is:
        a socket closing, an unknown PDU type, and a concession.  Raises GameOver
        for a disconnect or a concession, and for the response deadline expiring.
        """
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("response deadline expired")

            try:
                connection, pdu = self.inbox.get(timeout=remaining)
            except queue.Empty:
                continue

            player_id = connection.player_id

            # A reader thread reports its socket closing with a sentinel.
            if pdu is DISCONNECTED:
                self.logger.note(f"{player_id} disconnected")
                raise GameOver(
                    winner_id=self.state.opponent_of(player_id),
                    loser_id=player_id,
                    reason=protocol.REASON_DISCONNECT,
                )

            pdu_type = pdu.get("type")

            if pdu_type not in protocol.CLIENT_PDU_TYPES:
                self.send_error_to(connection, protocol.UNKNOWN_TYPE,
                                   f"Unknown PDU type {pdu_type!r}.", pdu)
                continue

            # CONCEDE may be sent at any time by either player, whoever holds
            # priority (RFC Section 5.4).
            if pdu_type == protocol.CONCEDE:
                raise GameOver(
                    winner_id=self.state.opponent_of(player_id),
                    loser_id=player_id,
                    reason=protocol.REASON_CONCEDE,
                )

            return player_id, pdu

    def await_action(self, player_id: str, allowed_types, expected_seq: int, regrant=None) -> dict:
        """Block until `player_id` sends one of `allowed_types` with a valid token.

        Everything invalid is answered with an ERROR PDU and then ignored, and we
        keep waiting: an illegal action never changes the game state and never
        disconnects the client (RFC Section 11).

        `regrant`, if given, re-issues the request PDU (normally PRIORITY_GRANT)
        after an error and returns the new expected seq_num token.
        """
        allowed = set(allowed_types)
        deadline = time.monotonic() + (self.time_limit_ms / 1000.0)

        while True:
            try:
                sender, pdu = self.next_client_pdu(deadline)
            except TimeoutError:
                raise self._timed_out(player_id)

            if sender != player_id:
                self.send_error(sender, protocol.NOT_YOUR_PRIORITY,
                                f"It is {player_id}'s turn to act.", pdu)
                continue

            if pdu["type"] not in allowed:
                expected = ", ".join(sorted(allowed))
                self.send_error(sender, protocol.WRONG_PHASE,
                                f"{pdu['type']} is not legal here; expected one of: {expected}.", pdu)
                if regrant is not None:
                    expected_seq = regrant()
                continue

            if pdu.get("seq_num") != expected_seq:
                self.send_error(
                    sender, protocol.STALE_ACTION,
                    f"Priority token mismatch. Expected seq_num {expected_seq}, "
                    f"got {pdu.get('seq_num')}.", pdu)
                if regrant is not None:
                    expected_seq = regrant()
                continue

            return pdu

    def await_from_any(self, allowed_types, tokens: dict) -> tuple:
        """Wait for an action from *either* player; returns (player_id, pdu).

        Used during the MULLIGAN state, where both players decide independently
        and may answer in either order (RFC Section 6.4).  `tokens` maps each
        player still expected to act to the seq_num they must echo.
        """
        allowed = set(allowed_types)
        deadline = time.monotonic() + (self.time_limit_ms / 1000.0)

        while True:
            try:
                sender, pdu = self.next_client_pdu(deadline)
            except TimeoutError:
                # Blame whoever still owes us a decision.
                raise self._timed_out(next(iter(tokens)))

            if sender not in tokens:
                self.send_error(sender, protocol.WRONG_PHASE,
                                "You have no pending decision.", pdu)
                continue

            if pdu["type"] not in allowed:
                expected = ", ".join(sorted(allowed))
                self.send_error(sender, protocol.WRONG_PHASE,
                                f"{pdu['type']} is not legal here; expected one of: {expected}.", pdu)
                continue

            if pdu.get("seq_num") != tokens[sender]:
                self.send_error(sender, protocol.STALE_ACTION,
                                f"Expected seq_num {tokens[sender]}, got {pdu.get('seq_num')}.", pdu)
                continue

            return sender, pdu

    def _timed_out(self, player_id: str) -> GameOver:
        """A player who misses the advertised deadline is treated as disconnected."""
        self.logger.note(f"{player_id} exceeded the {self.time_limit_ms} ms response deadline")
        return GameOver(
            winner_id=self.state.opponent_of(player_id),
            loser_id=player_id,
            reason=protocol.REASON_DISCONNECT,
        )

    # --- The stack --------------------------------------------------------

    def push_stack_item(self, item: StackItem) -> None:
        """Put an item on top of the stack and tell both players (RFC 8.3)."""
        self.state.stack.append(item)
        self.broadcast({"type": protocol.STACK_PUSH, **item.to_wire()})

    # --- State-based actions (RFC Section 8.4) ---------------------------

    def check_state_based_actions(self) -> list:
        """Apply state-based actions repeatedly until none remain.

        Checked after every game event and always before priority is granted.
        Returns the creatures that died, so combat can report them.

        A player at zero or less life loses immediately.  If both players would
        lose at once -- from mutual combat damage, say -- the Active Player loses
        and the Non-Active Player wins.
        """
        died = []

        # Creature destruction can cascade, so repeat until the board is stable.
        while True:
            casualties = [
                permanent
                for player_id in self.state.player_order
                for permanent in self.state.players[player_id].battlefield
                if permanent.has_lethal_damage or (permanent.is_creature and permanent.toughness <= 0)
            ]
            if not casualties:
                break
            for permanent in casualties:
                owner = self.state.players[permanent.controller]
                owner.battlefield.remove(permanent)
                owner.graveyard.append(permanent.card_id)
                died.append(permanent.card_id)

        # Losing the game is itself a state-based action.
        dead_players = [
            player_id for player_id in self.state.player_order
            if self.state.players[player_id].life <= 0
        ]
        if dead_players:
            loser = self.state.active_player if len(dead_players) == 2 else dead_players[0]
            raise GameOver(
                winner_id=self.state.opponent_of(loser),
                loser_id=loser,
                reason=protocol.REASON_LIFE_ZERO,
            )

        return died

    # --- Triggered abilities (RFC Section 8.6) ---------------------------

    def fire_enter_battlefield_triggers(self, permanent) -> None:
        """Queue any "when this enters the battlefield" trigger for a permanent."""
        trigger = effects.ENTER_BATTLEFIELD_TRIGGERS.get(cards.base_of(permanent.card_id))
        if trigger is None:
            return

        payload = {}
        if trigger.key == "gray_merchant":
            # X is locked in when the trigger is put on the stack.
            payload["amount"] = effects.devotion_to_black(self.state, permanent.controller)

        self.put_triggers_on_stack([(trigger, permanent.controller, permanent.card_id, payload)])

    def put_triggers_on_stack(self, pending: list) -> None:
        """Place fired triggers on the stack in the order the RFC requires.

        `pending` holds (Trigger, controller, source_id, payload) tuples.

        Ordering (RFC Section 8.6.2): the Active Player's triggers go on the stack
        first and therefore resolve last; the Non-Active Player's go on top and
        resolve first.  When one player controls two or more simultaneous
        triggers, that player chooses their relative order via TRIGGER_ORDER.

        All ordering decisions and target choices are resolved here, before any
        PRIORITY_GRANT is issued (RFC Section 8.6.1).
        """
        if not pending:
            return

        for controller in (self.state.active_player, self.state.non_active_player):
            mine = [entry for entry in pending if entry[1] == controller]
            if not mine:
                continue
            if len(mine) > 1:
                mine = self._ask_trigger_order(controller, mine)
            for trigger, _, source_id, payload in mine:
                self._place_one_trigger(trigger, controller, source_id, payload)

    def _ask_trigger_order(self, controller: str, entries: list) -> list:
        """Ask a player to order their simultaneous triggers (RFC Section 8.6.2).

        TRIGGER_ORDER does not consume priority: it is a mandatory decision made
        before the stack is updated.
        """
        by_id = {self.state.next_trigger_id(): entry for entry in entries}
        trigger_ids = list(by_id)

        def ask() -> int:
            return self.send(controller, {
                "type": protocol.TRIGGER_ORDER,
                "player_id": controller,
                "trigger_ids": trigger_ids,
            })

        seq = ask()
        while True:
            response = self.await_action(controller, {protocol.TRIGGER_ORDER_RESPONSE}, seq)
            ordered = response.get("ordered_trigger_ids")
            # The response must contain exactly the IDs we sent, reordered.
            if not isinstance(ordered, list) or sorted(ordered) != sorted(trigger_ids):
                self.send_error(controller, protocol.TRIGGER_ORDER_INVALID,
                                f"ordered_trigger_ids must be exactly {trigger_ids}.", response)
                seq = ask()
                continue
            return [by_id[trigger_id] for trigger_id in ordered]

    def _place_one_trigger(self, trigger, controller: str, source_id: str, payload: dict) -> None:
        """Choose a target if the trigger needs one, then push it on the stack."""
        targets = []

        if trigger.target_spec != effects.NO_TARGET:
            legal = effects.legal_targets_for(self.state, trigger.target_spec, controller)
            if not legal:
                # A trigger that requires a target but has none is discarded
                # immediately with no effect (RFC Section 8.6.4).
                self.logger.note(f"{trigger.key} trigger discarded: no legal targets")
                return
            chosen = self._ask_trigger_choice(trigger, controller, source_id, legal)
            if chosen is None:
                return  # The controller declined the trigger.
            targets = [chosen]

        self.push_stack_item(StackItem(
            stack_item_id=self.state.next_stack_item_id(),
            item_type=protocol.ITEM_TRIGGER_ABILITY,
            source=source_id,
            controller=controller,
            targets=targets,
            trigger_key=trigger.key,
            payload=payload,
        ))

    def _ask_trigger_choice(self, trigger, controller: str, source_id: str, legal: list):
        """Send TRIGGER_CHOICE and return the chosen target, or None if declined."""
        trigger_id = self.state.next_trigger_id()

        def ask() -> int:
            return self.send(controller, {
                "type": protocol.TRIGGER_CHOICE,
                "trigger_id": trigger_id,
                "source_id": source_id,
                "effect_summary": trigger.description,
                "requires_target": True,
                "legal_targets": legal,
            })

        seq = ask()
        while True:
            response = self.await_action(controller, {protocol.TRIGGER_CHOICE_RESPONSE}, seq)

            if response.get("trigger_id") != trigger_id:
                self.send_error(controller, protocol.TRIGGER_CHOICE_INVALID,
                                f"Unknown trigger_id; expected {trigger_id}.", response)
                seq = ask()
                continue

            if not response.get("accept"):
                return None

            chosen = response.get("chosen_target")
            if chosen is None:
                self.send_error(controller, protocol.TRIGGER_CHOICE_INVALID,
                                "chosen_target is required when accept is true.", response)
                seq = ask()
                continue

            if chosen not in legal:
                self.send_error(controller, protocol.ILLEGAL_TARGET,
                                f"{chosen} is not a legal target for this trigger.", response)
                seq = ask()
                continue

            return chosen
