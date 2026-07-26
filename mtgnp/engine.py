"""
The shared engine parts that every rules module uses.

`GameEngine` owns the pieces that the lifecycle, turn, priority and combat code
all need. These are the sequence number counter of the server, the helpers that
send and broadcast PDUs, the helper that waits for a player to act and checks
their seq_num token, the state-based actions, and the code that puts triggered
abilities on the stack.

Threading model
---------------
Each client socket has one reader thread, and that thread pushes
`(connection, pdu)` pairs onto `inbox`. One game thread runs the rules, and it is
the only thread that touches GameState, so the rules code does not need any
locks. The reader thread answers PING on its own, because PING does not read the
game state, and that PDU never reaches the inbox.

Since only one thread runs the rules, we can write the turn structure as plain
sequential code that simply waits for a player. This is much easier to read than
a state machine built out of callbacks.

The inbox carries the connection instead of a player ID because the server does
not know yet what the clients will call themselves during the LOBBY state. The
client picks its own `player_id` in the PLAYER_READY PDU (RFC Section 6.2).
"""

import queue
import time

from . import cards, effects, protocol
from .state import GameOver, StackItem

# A reader thread pushes this onto the inbox when the socket of its client closes.
DISCONNECTED = "__disconnected__"

# The default response deadline that we announce in PRIORITY_GRANT. The examples
# in the RFC use 60000, but we chose a larger value so that a person who
# demonstrates the protocol by hand does not get timed out in the middle of a
# turn. The --time-limit-ms flag of the server changes this.
DEFAULT_TIME_LIMIT_MS = 300000


class GameEngine:
    """The view of the server that the rules use: the state, and a way to reach the clients."""

    def __init__(self, logger, time_limit_ms: int = DEFAULT_TIME_LIMIT_MS):
        self.logger = logger
        self.time_limit_ms = time_limit_ms
        self.inbox: queue.Queue = queue.Queue()

        # We set both of these once the LOBBY state has two ready players.
        self.state = None
        self.connections: dict = {}     # player_id -> ClientConnection

        self._seq = 0                   # The PDU counter of the server.

    # --- Sequence numbers and sending ------------------------------------

    def next_seq(self) -> int:
        """The counter of the server, which only goes up (RFC Section 5.4).

        The RFC allows "a simple counter that increments with each PDU sent", and
        that is what we use. It keeps counting across games in the same session,
        because we reuse the same TCP connections.
        """
        self._seq += 1
        return self._seq

    def send_to(self, connection, pdu: dict, seq: int | None = None) -> int:
        """Send one PDU over one connection and return the seq_num that it carried."""
        seq = self.next_seq() if seq is None else seq
        # We build the PDU with `type` and `seq_num` in front. Every PDU needs
        # both fields (RFC Section 5.4), and this also makes the verbose output
        # easier to read.
        body = {key: value for key, value in pdu.items() if key != "type"}
        framed = {"type": pdu["type"], "seq_num": seq, **body}
        connection.send(framed)
        return seq

    def send(self, player_id: str, pdu: dict, seq: int | None = None) -> int:
        """Send one PDU to one player, found by their ID."""
        connection = self.connections.get(player_id)
        if connection is None:
            return seq if seq is not None else self.next_seq()
        return self.send_to(connection, pdu, seq=seq)

    def broadcast(self, pdu: dict) -> int:
        """Send one PDU to both players under one seq_num. This is an S->ALL PDU."""
        seq = self.next_seq()
        for player_id in self.state.player_order:
            self.send(player_id, pdu, seq=seq)
        return seq

    # --- GAME_STATE_UPDATE ------------------------------------------------

    def send_state_update(self, player_id: str) -> int:
        """Send one player their own filtered view of the game state."""
        return self.send(player_id, {
            "type": protocol.GAME_STATE_UPDATE,
            "state": self.state.visible_state(player_id),
        })

    def broadcast_state_update(self) -> dict:
        """Send every player their own filtered state.

        Each player gets a separate PDU with its own seq_num, because the two
        payloads are not the same. We filter the hidden information for each
        receiver (RFC Section 4.2). This returns {player_id: seq_num}, so the
        caller can use the update of a player as the seq_num token of that
        player. MULLIGAN_CHOICE and DISCARD need this.
        """
        return {pid: self.send_state_update(pid) for pid in self.state.player_order}

    # --- Errors (RFC Section 11) -----------------------------------------

    def send_error_to(self, connection, code: str, message: str, rejected: dict | None = None) -> None:
        """Report an illegal or invalid PDU without changing the game state.

        The ERROR PDU echoes the seq_num of the rejected action when we have one
        (RFC Section 10.2.23), and it also carries a copy of that action, so the
        client can show the player exactly what we refused. An illegal action
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
        """Give a player priority and return the seq_num that becomes their token."""
        self.state.priority_holder = player_id
        return self.send(player_id, {
            "type": protocol.PRIORITY_GRANT,
            "player_id": player_id,
            "time_limit_ms": self.time_limit_ms,
        })

    # --- Waiting for client actions --------------------------------------

    def next_client_pdu(self, deadline: float) -> tuple:
        """Return (player_id, pdu) for the next client PDU during a game.

        This handles the three things that can happen no matter whose turn it is:
        a socket that closes, an unknown PDU type, and a player who concedes. It
        raises GameOver for a disconnect, for a concession, and when the response
        deadline runs out.
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

            # A reader thread uses this marker to report that its socket closed.
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

            # Either player can send CONCEDE at any time, even when the other
            # player holds priority (RFC Section 5.4).
            if pdu_type == protocol.CONCEDE:
                raise GameOver(
                    winner_id=self.state.opponent_of(player_id),
                    loser_id=player_id,
                    reason=protocol.REASON_CONCEDE,
                )

            return player_id, pdu

    def await_action(self, player_id: str, allowed_types, expected_seq: int, regrant=None) -> dict:
        """Wait until `player_id` sends one of `allowed_types` with a valid token.

        We answer anything invalid with an ERROR PDU, ignore it, and keep
        waiting. An illegal action never changes the game state and never
        disconnects the client (RFC Section 11).

        If the caller passes `regrant`, we send the request PDU again after an
        error, which is usually PRIORITY_GRANT, and we take the new seq_num token
        from it.
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
        """Wait for an action from either player and return (player_id, pdu).

        We use this during the MULLIGAN state, where both players decide on their
        own and can answer in any order (RFC Section 6.4). `tokens` maps every
        player we are still waiting for to the seq_num they have to echo back.
        """
        allowed = set(allowed_types)
        deadline = time.monotonic() + (self.time_limit_ms / 1000.0)

        while True:
            try:
                sender, pdu = self.next_client_pdu(deadline)
            except TimeoutError:
                # We blame the player who still owes us a decision.
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
        """We treat a player who misses the announced deadline as disconnected."""
        self.logger.note(f"{player_id} exceeded the {self.time_limit_ms} ms response deadline")
        return GameOver(
            winner_id=self.state.opponent_of(player_id),
            loser_id=player_id,
            reason=protocol.REASON_DISCONNECT,
        )

    # --- The stack --------------------------------------------------------

    def push_stack_item(self, item: StackItem) -> None:
        """Put an item on top of the stack and tell both players about it (RFC 8.3)."""
        self.state.stack.append(item)
        self.broadcast({"type": protocol.STACK_PUSH, **item.to_wire()})

    # --- State-based actions (RFC Section 8.4) ---------------------------

    def check_state_based_actions(self) -> list:
        """Apply the state-based actions again and again until none are left.

        We check these after every game event, and always before we grant
        priority. This returns the creatures that died, so that the combat code
        can report them.

        A player at zero life or less loses right away. If both players would
        lose at the same time, for example from combat damage that both sides
        dealt, the Active Player loses and the Non-Active Player wins.
        """
        died = []

        # Destroying a creature can destroy another one, so we repeat this until
        # the board stops changing.
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

        # Losing the game is also a state-based action.
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
        """Queue the "when this enters the battlefield" trigger of a permanent, if it has one."""
        trigger = effects.ENTER_BATTLEFIELD_TRIGGERS.get(cards.base_of(permanent.card_id))
        if trigger is None:
            return

        payload = {}
        if trigger.key == "gray_merchant":
            # We fix the value of X when the trigger goes on the stack.
            payload["amount"] = effects.devotion_to_black(self.state, permanent.controller)

        self.put_triggers_on_stack([(trigger, permanent.controller, permanent.card_id, payload)])

    def put_triggers_on_stack(self, pending: list) -> None:
        """Put the triggers that fired on the stack, in the order the RFC asks for.

        `pending` holds (Trigger, controller, source_id, payload) tuples.

        The order comes from RFC Section 8.6.2. The triggers of the Active Player
        go on the stack first, so they resolve last. The triggers of the
        Non-Active Player go on top, so they resolve first. When one player
        controls two or more triggers that fired together, that player chooses
        the order among them through TRIGGER_ORDER.

        We settle every ordering decision and every target choice here, before we
        send any PRIORITY_GRANT (RFC Section 8.6.1).
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
        """Ask a player to order the triggers that fired together (RFC Section 8.6.2).

        TRIGGER_ORDER does not use up priority. It is a decision that the player
        has to make before we update the stack.
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
            # The response has to contain the same IDs we sent, only in a new order.
            if not isinstance(ordered, list) or sorted(ordered) != sorted(trigger_ids):
                self.send_error(controller, protocol.TRIGGER_ORDER_INVALID,
                                f"ordered_trigger_ids must be exactly {trigger_ids}.", response)
                seq = ask()
                continue
            return [by_id[trigger_id] for trigger_id in ordered]

    def _place_one_trigger(self, trigger, controller: str, source_id: str, payload: dict) -> None:
        """Choose a target if the trigger needs one, then put it on the stack."""
        targets = []

        if trigger.target_spec != effects.NO_TARGET:
            legal = effects.legal_targets_for(self.state, trigger.target_spec, controller)
            if not legal:
                # A trigger that needs a target but has none is thrown away right
                # away and does nothing (RFC Section 8.6.4).
                self.logger.note(f"{trigger.key} trigger discarded: no legal targets")
                return
            chosen = self._ask_trigger_choice(trigger, controller, source_id, legal)
            if chosen is None:
                return  # The controller said no to the trigger.
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
        """Send TRIGGER_CHOICE and return the chosen target, or None if the player says no."""
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
