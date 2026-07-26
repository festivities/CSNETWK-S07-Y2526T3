"""
Protocol conformance tests for MTGNP 1.0.

These tests run the real server over real TCP sockets. They talk to it with a
simple test client that only sends the PDUs we write by hand, so the framing, the
sequence number tokens and the error codes get tested the same way a client from
another group would test them.

We only use the standard library, so there is no test runner to install:

    python -m unittest discover -s tests -v

The tests do not depend on the shuffle, because each one uses a deck that holds
only the cards that the test needs. In an 8 card deck, every possible opening
hand of 7 cards holds at least 3 copies of each card, so nothing is left to
chance.
"""

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mtgnp import cards, protocol                      # noqa: E402
from mtgnp.server import MTGNPServer                   # noqa: E402

# Every test binds its own port, so the tests do not affect each other.
_next_port = 4600


def _allocate_port() -> int:
    global _next_port
    _next_port += 1
    return _next_port


def instances(base: str, count: int, start: int = 1) -> list:
    """Build `count` instances of one card, starting at the copy number `start`.

    For example, this returns ["mountain_001", "mountain_002"] and so on. The
    `start` value lets the two players use different copies of the same card,
    because both decks come from one shared fixed set and must not overlap.
    """
    return [f"{base}_{i:03d}" for i in range(start, start + count)]


class RawClient:
    """A very small MTGNP client that only sends the PDUs a test gives it."""

    def __init__(self, port: int):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=5)
        # The newest GAME_STATE_UPDATE from a running game, so that a test can
        # read the hand and the battlefield that the server reported last.
        self.last_state: dict = {}

    def send(self, pdu: dict) -> None:
        protocol.send_pdu(self.socket, pdu)

    def send_raw(self, payload: bytes) -> None:
        """Send a frame that we built by hand, so we can test broken payloads."""
        self.socket.sendall(len(payload).to_bytes(4, "big") + payload)

    def recv(self) -> dict:
        pdu = protocol.recv_pdu(self.socket)
        if pdu.get("type") == protocol.GAME_STATE_UPDATE:
            state = pdu.get("state") or {}
            if state.get("phase") not in (protocol.LOBBY, protocol.GAME_SETUP):
                self.last_state = state
        return pdu

    def recv_until(self, pdu_type: str, where=None, limit: int = 400) -> dict:
        """Keep reading until a PDU of `pdu_type` arrives, and match `where` if we got one."""
        for _ in range(limit):
            pdu = self.recv()
            if pdu.get("type") == pdu_type and (where is None or where(pdu)):
                return pdu
        raise AssertionError(f"never received {pdu_type}")

    def hand(self, player_id: str) -> list:
        """Our hand. We accept both the object shape and the array shape of `hand`."""
        hand = self.last_state.get("hand")
        if isinstance(hand, dict):
            return hand.get(player_id) or []
        return hand or []

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass


class ServerTestCase(unittest.TestCase):
    """The base class that starts a new server for every test."""

    # The default decks. We made them symmetric on purpose, so it does not matter
    # which player wins the coin flip. Whoever is active holds the same kinds of
    # card either way.
    #
    # Each deck holds 8 cards, and an opening hand holds 7, so exactly one card
    # stays in the library. This makes the contents of a hand predictable without
    # any help from the shuffle. With two Lightning Bolts in an 8 card deck, the
    # opening hand always holds at least one of them, and at least 5 Mountains.
    DECK_ONE = instances("mountain", 6) + instances("lightning_bolt", 2)
    DECK_TWO = instances("mountain", 6, start=7) + instances("lightning_bolt", 2, start=3)

    def setUp(self):
        self.port = _allocate_port()
        self.server = MTGNPServer(host="127.0.0.1", port=self.port,
                                  verbose=False, quiet=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.clients = []
        time.sleep(0.25)   # We give the listening socket time to come up.

    def tearDown(self):
        for client in self.clients:
            client.close()
        if self.server._listener is not None:
            self.server._listener.close()
        # We close the sockets of the server so that its reader threads finish.
        # They are daemon threads, so a thread that is still inside a print()
        # when the interpreter exits can die while it holds the stdout lock, and
        # Python then reports a fatal error after the run. Closing the sockets
        # here lets those threads end on their own instead.
        for connection in self.server.live_connections():
            connection.mark_closed()

    def connect(self) -> RawClient:
        client = RawClient(self.port)
        self.clients.append(client)
        return client

    # --- Helpers that get a game started ---------------------------------

    def send_ready(self, client: RawClient, player_id: str, deck: list) -> None:
        client.send({"type": "PLAYER_READY", "seq_num": 1,
                     "player_id": player_id, "deck_list": deck})

    def start_game(self, deck_one=None, deck_two=None):
        """Bring two clients up to the first priority window of turn 1.

        This returns (clients_by_id, active_player_id, first_priority_grant).
        """
        one, two = self.connect(), self.connect()

        # We send the two PLAYER_READY PDUs one after the other, so they always
        # arrive in the same order. If we sent them together, the order in which
        # the decks get shuffled would be a race.
        self.send_ready(one, "player_1", deck_one or self.DECK_ONE)
        one.recv_until(protocol.GAME_STATE_UPDATE)
        self.send_ready(two, "player_2", deck_two or self.DECK_TWO)

        in_mulligan = lambda p: (p.get("state") or {}).get("phase") == protocol.MULLIGAN
        mull_one = one.recv_until(protocol.GAME_STATE_UPDATE, in_mulligan)
        mull_two = two.recv_until(protocol.GAME_STATE_UPDATE, in_mulligan)

        one.send({"type": "MULLIGAN_CHOICE", "seq_num": mull_one["seq_num"],
                  "keep": True, "cards_to_bottom": []})
        two.send({"type": "MULLIGAN_CHOICE", "seq_num": mull_two["seq_num"],
                  "keep": True, "cards_to_bottom": []})

        clients = {"player_1": one, "player_2": two}
        untap = one.recv_until(protocol.PHASE_TRANSITION,
                               lambda p: p.get("to_phase") == protocol.UNTAP)
        active = untap["active_player"]

        # The grant of the Upkeep Step is now open, and the server is waiting for
        # this player to answer it. We remember it here so that take_grant can
        # give it to the first caller. Without this, a test would wait for a
        # second grant that never arrives.
        grant = clients[active].recv_until(protocol.PRIORITY_GRANT)
        self.pending_grant = (active, grant)
        return clients, active, grant

    def other(self, player_id: str) -> str:
        return "player_2" if player_id == "player_1" else "player_1"

    def take_grant(self, clients, player_id: str) -> dict:
        """The PRIORITY_GRANT that this player has to answer next.

        When we already received a grant but have not answered it yet, this
        returns that one. This way a test never waits for a grant that the server
        has in fact already sent.
        """
        pending = getattr(self, "pending_grant", None)
        if pending is not None and pending[0] == player_id:
            self.pending_grant = None
            return pending[1]
        return clients[player_id].recv_until(protocol.PRIORITY_GRANT)

    def pass_priority(self, clients, player_id: str) -> None:
        grant = self.take_grant(clients, player_id)
        clients[player_id].send({"type": "PRIORITY_PASS", "seq_num": grant["seq_num"]})

    def pass_until(self, clients, active, target_phase):
        """Both players pass priority until `target_phase` starts.

        This returns the PHASE_TRANSITION that announced that phase.
        """
        inactive = self.other(active)
        for _ in range(60):
            self.pass_priority(clients, active)
            self.pass_priority(clients, inactive)
            transition = clients[active].recv_until(protocol.PHASE_TRANSITION)
            if transition.get("to_phase") == target_phase:
                return transition
        raise AssertionError(f"never reached {target_phase}")

    def play_a_land(self, clients, active):
        """Play the first land in the hand during a Main Phase, and return the new grant."""
        grant = self.take_grant(clients, active)
        land = next(c for c in clients[active].hand(active) if cards.lookup(c).is_land)
        clients[active].send({"type": "PLAY_LAND", "seq_num": grant["seq_num"],
                              "card_id": land})
        return clients[active].recv_until(protocol.PRIORITY_GRANT), land


# --- Framing and PDU structure (RFC Sections 5.2, 5.4) ------------------

class FramingTests(ServerTestCase):

    def test_length_prefixed_frame_round_trip(self):
        """We frame a PDU with a 4-byte big-endian length, and it parses back."""
        client = self.connect()
        client.send({"type": "PING", "seq_num": 7, "timestamp": 1234})
        pong = client.recv_until(protocol.PONG)
        self.assertEqual(pong["seq_num"], 7)
        self.assertEqual(pong["timestamp"], 1234)

    def test_oversized_pdu_is_refused(self):
        """A PDU must not be larger than 65,535 bytes (RFC Section 5.2)."""
        sock = socket.socket()
        with self.assertRaises(protocol.PDUTooLarge):
            protocol.send_pdu(sock, {"type": "PING", "seq_num": 1,
                                     "pad": "x" * (protocol.MAX_PAYLOAD_BYTES + 1)})
        sock.close()

    def test_partial_reads_are_reassembled(self):
        """recv_pdu has to put a frame back together when TCP splits it into several parts."""
        client = self.connect()
        payload = b'{"type": "PING", "seq_num": 42, "timestamp": 9}'
        client.socket.sendall(len(payload).to_bytes(4, "big"))
        time.sleep(0.05)                       # This forces a separate segment.
        client.socket.sendall(payload[:10])
        time.sleep(0.05)
        client.socket.sendall(payload[10:])
        self.assertEqual(client.recv_until(protocol.PONG)["seq_num"], 42)

    def test_invalid_json_is_reported_and_connection_kept(self):
        """A payload with a good frame but bad JSON gives us ERROR/INVALID_JSON."""
        client = self.connect()
        client.send_raw(b"{this is not json")
        self.assertEqual(client.recv_until(protocol.ERROR)["code"],
                         protocol.INVALID_JSON)

        # The connection has to survive an illegal PDU, so a later PDU still works.
        client.send({"type": "PING", "seq_num": 99, "timestamp": 1})
        self.assertEqual(client.recv_until(protocol.PONG)["seq_num"], 99)

    def test_unknown_pdu_type_is_rejected(self):
        client = self.connect()
        client.send({"type": "TELEPORT_CREATURE", "seq_num": 3})
        self.assertEqual(client.recv_until(protocol.ERROR)["code"],
                         protocol.UNKNOWN_TYPE)

    def test_server_pdus_carry_type_and_seq_num(self):
        """Every PDU needs a type and a seq_num (RFC Section 5.4)."""
        clients, active, grant = self.start_game()
        self.assertIn("type", grant)
        self.assertIsInstance(grant["seq_num"], int)
        self.assertEqual(grant["player_id"], active)
        self.assertIn("time_limit_ms", grant)


# --- How the TCP server behaves (RFC Section 5.1) -----------------------

class ConnectionTests(ServerTestCase):

    def test_third_connection_is_refused(self):
        """A game takes only two players, and the server refuses everyone else."""
        self.connect()
        self.connect()
        third = self.connect()
        third.socket.settimeout(3)
        with self.assertRaises((protocol.ConnectionClosed, OSError)):
            third.recv()

    def test_slot_frees_up_after_disconnect(self):
        """A disconnect frees a player slot, so another client can take it."""
        first, _second = self.connect(), self.connect()
        first.close()
        time.sleep(0.3)

        replacement = self.connect()
        replacement.send({"type": "PING", "seq_num": 1, "timestamp": 5})
        self.assertEqual(replacement.recv_until(protocol.PONG)["seq_num"], 1)


# --- LOBBY and deck validation (RFC Section 6.2) -----------------------

class LobbyTests(ServerTestCase):

    def expect_deck_error(self, deck_list) -> dict:
        client = self.connect()
        self.send_ready(client, "player_1", deck_list)
        error = client.recv_until(protocol.ERROR)
        self.assertEqual(error["code"], protocol.ILLEGAL_DECK, error.get("message"))
        return error

    def test_empty_deck_is_illegal(self):
        self.expect_deck_error([])

    def test_deck_over_fifty_cards_is_illegal(self):
        deck = instances("mountain", 20) + instances("forest", 20) + instances("island", 11)
        self.assertEqual(len(deck), 51)
        self.assertIn("51", self.expect_deck_error(deck)["message"])

    def test_card_outside_the_fixed_set_is_illegal(self):
        self.expect_deck_error(["black_lotus_001", "mountain_001"])

    def test_copy_number_beyond_the_fixed_set_is_illegal(self):
        """The fixed set has only 4 copies of Lightning Bolt, so _005 is not a real card."""
        self.expect_deck_error(["lightning_bolt_005"])

    def test_same_card_instance_twice_is_illegal(self):
        self.expect_deck_error(["mountain_001", "mountain_001"])

    def test_duplicate_player_id_is_rejected(self):
        one, two = self.connect(), self.connect()
        self.send_ready(one, "player_1", self.DECK_ONE)
        one.recv_until(protocol.GAME_STATE_UPDATE)

        self.send_ready(two, "player_1", self.DECK_TWO)
        self.assertEqual(two.recv_until(protocol.ERROR)["code"], protocol.DUPLICATE_ID)

    def test_overlapping_decks_are_rejected(self):
        """Both decks come from one shared set, so the same instance cannot be in both."""
        one, two = self.connect(), self.connect()
        self.send_ready(one, "player_1", self.DECK_ONE)
        one.recv_until(protocol.GAME_STATE_UPDATE)

        self.send_ready(two, "player_2", self.DECK_ONE)
        self.assertEqual(two.recv_until(protocol.ERROR)["code"], protocol.ILLEGAL_DECK)

    def test_only_player_ready_is_accepted_in_lobby(self):
        client = self.connect()
        client.send({"type": "PRIORITY_PASS", "seq_num": 1})
        self.assertEqual(client.recv_until(protocol.ERROR)["code"], protocol.WRONG_PHASE)

    def test_lobby_update_reports_players_ready(self):
        client = self.connect()
        self.send_ready(client, "player_1", self.DECK_ONE)
        update = client.recv_until(protocol.GAME_STATE_UPDATE)
        self.assertEqual(update["state"]["players_ready"], 1)
        self.assertEqual(update["state"]["phase"], protocol.LOBBY)


# --- GAME_SETUP, hidden information and mulligans ----------------------

class SetupTests(ServerTestCase):

    def test_setup_deals_seven_cards_and_twenty_life(self):
        one, two = self.connect(), self.connect()
        self.send_ready(one, "player_1", self.DECK_ONE)
        one.recv_until(protocol.GAME_STATE_UPDATE)
        self.send_ready(two, "player_2", self.DECK_TWO)

        update = one.recv_until(
            protocol.GAME_STATE_UPDATE,
            lambda p: (p.get("state") or {}).get("phase") == protocol.MULLIGAN)
        state = update["state"]

        self.assertEqual(state["life_totals"]["player_1"], 20)
        self.assertEqual(state["life_totals"]["player_2"], 20)
        self.assertEqual(len(one.hand("player_1")), 7)
        self.assertEqual(state["library_counts"]["player_1"], len(self.DECK_ONE) - 7)

    def test_opponent_hand_is_hidden(self):
        """A player sees their own hand, but only a count for the hand of the opponent."""
        clients, active, _ = self.start_game()
        state = clients["player_1"].last_state

        self.assertEqual(len(clients["player_1"].hand("player_1")), 7)
        self.assertNotIn("player_2", state.get("hand", {}))
        self.assertEqual(state["hand_counts"]["player_2"], 7)
        # A library is always only a count, and never a list of cards.
        self.assertIsInstance(state["library_counts"]["player_2"], int)

    def test_mulligan_requires_exactly_n_cards_bottomed(self):
        """A player who keeps after one mulligan has to bottom exactly one card."""
        one, two = self.connect(), self.connect()
        self.send_ready(one, "player_1", self.DECK_ONE)
        one.recv_until(protocol.GAME_STATE_UPDATE)
        self.send_ready(two, "player_2", self.DECK_TWO)

        in_mulligan = lambda p: (p.get("state") or {}).get("phase") == protocol.MULLIGAN
        first = one.recv_until(protocol.GAME_STATE_UPDATE, in_mulligan)

        one.send({"type": "MULLIGAN_CHOICE", "seq_num": first["seq_num"],
                  "keep": False, "cards_to_bottom": []})
        redraw = one.recv_until(protocol.GAME_STATE_UPDATE, in_mulligan)
        self.assertEqual(len(one.hand("player_1")), 7)

        # Keeping with no card on the bottom is illegal now.
        one.send({"type": "MULLIGAN_CHOICE", "seq_num": redraw["seq_num"],
                  "keep": True, "cards_to_bottom": []})
        self.assertEqual(one.recv_until(protocol.ERROR)["code"], protocol.ILLEGAL_ACTION)

        # Keeping with exactly one card works, and leaves a hand of 6 cards.
        one.send({"type": "MULLIGAN_CHOICE", "seq_num": redraw["seq_num"],
                  "keep": True, "cards_to_bottom": [one.hand("player_1")[0]]})

        two_mull = two.recv_until(protocol.GAME_STATE_UPDATE, in_mulligan)
        two.send({"type": "MULLIGAN_CHOICE", "seq_num": two_mull["seq_num"],
                  "keep": True, "cards_to_bottom": []})

        untap = one.recv_until(protocol.PHASE_TRANSITION,
                               lambda p: p.get("to_phase") == protocol.UNTAP)
        self.assertEqual(untap["turn"], 1)
        self.assertEqual(len(one.hand("player_1")), 6)

    def test_mulligan_rejects_cards_to_bottom_when_not_keeping(self):
        one, two = self.connect(), self.connect()
        self.send_ready(one, "player_1", self.DECK_ONE)
        one.recv_until(protocol.GAME_STATE_UPDATE)
        self.send_ready(two, "player_2", self.DECK_TWO)

        first = one.recv_until(
            protocol.GAME_STATE_UPDATE,
            lambda p: (p.get("state") or {}).get("phase") == protocol.MULLIGAN)
        one.send({"type": "MULLIGAN_CHOICE", "seq_num": first["seq_num"],
                  "keep": False, "cards_to_bottom": [one.hand("player_1")[0]]})
        self.assertEqual(one.recv_until(protocol.ERROR)["code"], protocol.ILLEGAL_ACTION)


# --- Phase sequence (RFC Section 7) ------------------------------------

class TurnStructureTests(ServerTestCase):

    def test_phases_occur_in_the_order_the_rfc_specifies(self):
        clients, active, _ = self.start_game()
        inactive = self.other(active)

        # start_game already read the Untap and Upkeep transitions on its way to
        # the first priority window, so what we see here starts again at Draw.
        seen = [protocol.UNTAP, protocol.UPKEEP]
        for _ in range(40):
            self.pass_priority(clients, active)
            self.pass_priority(clients, inactive)
            transition = clients[active].recv_until(protocol.PHASE_TRANSITION)
            seen.append(transition["to_phase"])
            if transition["to_phase"] == protocol.DECLARE_ATTACKERS_STEP:
                break

        self.assertEqual(seen, [
            protocol.UNTAP, protocol.UPKEEP, protocol.DRAW,
            protocol.PRECOMBAT_MAIN, protocol.BEGIN_COMBAT,
            protocol.DECLARE_ATTACKERS_STEP,
        ])

    def test_first_player_does_not_draw_on_turn_one(self):
        """The opening hand still holds 7 cards after the first Draw Step."""
        clients, active, _ = self.start_game()
        self.pass_until(clients, active, protocol.PRECOMBAT_MAIN)
        self.assertEqual(len(clients[active].hand(active)), 7)
        self.assertEqual(clients[active].last_state["turn"], 1)

    def test_untap_step_grants_no_priority(self):
        """The first PRIORITY_GRANT of the game comes from the Upkeep Step."""
        clients, active, grant = self.start_game()
        # start_game stops at the first grant, and the phase there is UPKEEP and
        # not UNTAP.
        upkeep = clients[active].last_state
        self.assertEqual(upkeep["phase"], protocol.UNTAP)   # The last state update.
        self.assertEqual(grant["player_id"], active)        # The AP acts first.


# --- Priority, the tokens, and the stack (RFC Sections 8.1 to 8.5) ---

class PriorityTests(ServerTestCase):

    def test_stale_seq_num_is_rejected_and_priority_reissued(self):
        clients, active, grant = self.start_game()
        clients[active].send({"type": "PRIORITY_PASS",
                              "seq_num": grant["seq_num"] - 5})
        error = clients[active].recv_until(protocol.ERROR)
        self.assertEqual(error["code"], protocol.STALE_ACTION)
        self.assertEqual(error["rejected_action"]["type"], "PRIORITY_PASS")
        # The server sends PRIORITY_GRANT again so the player can try once more.
        self.assertEqual(clients[active].recv_until(protocol.PRIORITY_GRANT)["player_id"],
                         active)

    def test_acting_without_priority_is_rejected(self):
        clients, active, grant = self.start_game()
        inactive = self.other(active)
        clients[inactive].send({"type": "PRIORITY_PASS", "seq_num": grant["seq_num"]})
        self.assertEqual(clients[inactive].recv_until(protocol.ERROR)["code"],
                         protocol.NOT_YOUR_PRIORITY)

    def test_active_player_receives_priority_first(self):
        clients, active, grant = self.start_game()
        clients[active].send({"type": "PRIORITY_PASS", "seq_num": grant["seq_num"]})
        # The NAP only gets priority after the AP passes.
        nap_grant = clients[self.other(active)].recv_until(protocol.PRIORITY_GRANT)
        self.assertEqual(nap_grant["player_id"], self.other(active))

    def test_land_only_in_main_phase(self):
        """Playing a land during the Upkeep Step gives a WRONG_PHASE error."""
        clients, active, grant = self.start_game()
        land = next(c for c in clients[active].hand(active) if cards.lookup(c).is_land)
        clients[active].send({"type": "PLAY_LAND", "seq_num": grant["seq_num"],
                              "card_id": land})
        self.assertEqual(clients[active].recv_until(protocol.ERROR)["code"],
                         protocol.WRONG_PHASE)

    def test_one_land_per_turn(self):
        clients, active, _ = self.start_game()
        self.pass_until(clients, active, protocol.PRECOMBAT_MAIN)
        grant, first_land = self.play_a_land(clients, active)

        second = next(c for c in clients[active].hand(active)
                      if cards.lookup(c).is_land and c != first_land)
        clients[active].send({"type": "PLAY_LAND", "seq_num": grant["seq_num"],
                              "card_id": second})
        error = clients[active].recv_until(protocol.ERROR)
        self.assertEqual(error["code"], protocol.ILLEGAL_ACTION)
        self.assertIn("already played a land", error["message"])

    def test_land_play_updates_battlefield_and_keeps_priority(self):
        clients, active, _ = self.start_game()
        self.pass_until(clients, active, protocol.PRECOMBAT_MAIN)
        grant, land = self.play_a_land(clients, active)

        battlefield = clients[active].last_state["battlefield"][active]
        self.assertEqual([p["id"] for p in battlefield], [land])
        self.assertTrue(clients[active].last_state["land_played_this_turn"])
        # The Active Player keeps priority after playing a land.
        self.assertEqual(grant["player_id"], active)

    def test_mana_payment_must_match_the_printed_cost(self):
        """An empty payment does not pay the {R} that Lightning Bolt costs."""
        clients, active, _ = self.start_game()
        self.pass_until(clients, active, protocol.PRECOMBAT_MAIN)
        grant, _ = self.play_a_land(clients, active)

        bolt = next(c for c in clients[active].hand(active)
                    if cards.base_of(c) == "lightning_bolt")
        clients[active].send({"type": "CAST_SPELL", "seq_num": grant["seq_num"],
                              "card_id": bolt, "targets": [self.other(active)],
                              "mana_payment": {}})
        self.assertEqual(clients[active].recv_until(protocol.ERROR)["code"],
                         protocol.INSUFFICIENT_MANA)

    def test_cannot_pay_without_an_untapped_source(self):
        """Even the right payment fails when no land is on the battlefield."""
        clients, active, _ = self.start_game()
        self.pass_until(clients, active, protocol.PRECOMBAT_MAIN)
        grant = clients[active].recv_until(protocol.PRIORITY_GRANT)

        bolt = next(c for c in clients[active].hand(active)
                    if cards.base_of(c) == "lightning_bolt")
        clients[active].send({"type": "CAST_SPELL", "seq_num": grant["seq_num"],
                              "card_id": bolt, "targets": [self.other(active)],
                              "mana_payment": {"R": 1}})
        self.assertEqual(clients[active].recv_until(protocol.ERROR)["code"],
                         protocol.INSUFFICIENT_MANA)

    def test_bolt_resolves_for_three_damage(self):
        """A whole cycle of cast, STACK_PUSH, both players pass, and STACK_RESOLVE."""
        clients, active, _ = self.start_game()
        inactive = self.other(active)
        self.pass_until(clients, active, protocol.PRECOMBAT_MAIN)
        grant, _ = self.play_a_land(clients, active)

        bolt = next(c for c in clients[active].hand(active)
                    if cards.base_of(c) == "lightning_bolt")
        clients[active].send({"type": "CAST_SPELL", "seq_num": grant["seq_num"],
                              "card_id": bolt, "targets": [inactive],
                              "mana_payment": {"R": 1}})

        push = clients[active].recv_until(protocol.STACK_PUSH)
        self.assertEqual(push["item_type"], protocol.ITEM_SPELL)
        self.assertEqual(push["source"], bolt)
        self.assertEqual(push["targets"], [inactive])
        self.assertEqual(push["controller"], active)

        # The player who cast it keeps priority, and then both pass so that the
        # spell resolves.
        grant = clients[active].recv_until(protocol.PRIORITY_GRANT)
        clients[active].send({"type": "PRIORITY_PASS", "seq_num": grant["seq_num"]})
        grant = clients[inactive].recv_until(protocol.PRIORITY_GRANT)
        clients[inactive].send({"type": "PRIORITY_PASS", "seq_num": grant["seq_num"]})

        resolve = clients[active].recv_until(protocol.STACK_RESOLVE)
        self.assertEqual(resolve["result"], "RESOLVED")
        change = resolve["state_changes"][0]
        self.assertEqual(change["change_type"], "DAMAGE")
        self.assertEqual(change["amount"], 3)

        clients[active].recv_until(protocol.GAME_STATE_UPDATE)
        self.assertEqual(clients[active].last_state["life_totals"][inactive], 17)
        # The spell went to the graveyard of its owner, and the stack is empty again.
        self.assertIn(bolt, clients[active].last_state["graveyard"][active])
        self.assertEqual(clients[active].last_state["stack"], [])

    def test_illegal_target_is_rejected(self):
        clients, active, _ = self.start_game()
        self.pass_until(clients, active, protocol.PRECOMBAT_MAIN)
        grant, _ = self.play_a_land(clients, active)

        bolt = next(c for c in clients[active].hand(active)
                    if cards.base_of(c) == "lightning_bolt")
        clients[active].send({"type": "CAST_SPELL", "seq_num": grant["seq_num"],
                              "card_id": bolt, "targets": ["no_such_thing_001"],
                              "mana_payment": {"R": 1}})
        self.assertEqual(clients[active].recv_until(protocol.ERROR)["code"],
                         protocol.ILLEGAL_TARGET)

    # Each deck holds three creatures that cost one mana, so the opening hand
    # always holds at least two of them, no matter who wins the coin flip.
    SORCERY_ONE = instances("mountain", 5) + instances("monastery_swiftspear", 3)
    SORCERY_TWO = instances("plains", 5) + instances("savannah_lions", 3)

    def test_sorcery_speed_spell_needs_an_empty_stack(self):
        """A player cannot cast a creature while a spell is already on the stack."""
        clients, active, _ = self.start_game(deck_one=self.SORCERY_ONE,
                                             deck_two=self.SORCERY_TWO)
        self.pass_until(clients, active, protocol.PRECOMBAT_MAIN)
        grant, land = self.play_a_land(clients, active)
        colour = cards.lookup(land).color

        creatures = [c for c in clients[active].hand(active)
                     if cards.lookup(c).is_creature]
        clients[active].send({"type": "CAST_SPELL", "seq_num": grant["seq_num"],
                              "card_id": creatures[0], "targets": [],
                              "mana_payment": {colour: 1}})
        clients[active].recv_until(protocol.STACK_PUSH)
        grant = clients[active].recv_until(protocol.PRIORITY_GRANT)

        # A second creature at sorcery speed is illegal while the stack is not
        # empty. We check the timing before the mana, so we get WRONG_PHASE here
        # and not a mana error.
        clients[active].send({"type": "CAST_SPELL", "seq_num": grant["seq_num"],
                              "card_id": creatures[1], "targets": [],
                              "mana_payment": {colour: 1}})
        error = clients[active].recv_until(protocol.ERROR)
        self.assertEqual(error["code"], protocol.WRONG_PHASE)


# --- Combat (RFC Section 9) ---------------------------------------------

class CombatTests(ServerTestCase):

    # Creatures that cost one mana and have no haste, in colors that do not
    # overlap, so the test works no matter who wins the coin flip.
    LIONS = instances("plains", 4) + instances("savannah_lions", 4)
    MYSTICS = instances("forest", 4) + instances("elvish_mystic", 4)

    def cast_a_creature(self, clients, active):
        """Play a land, cast a creature that costs one mana, and return its card ID."""
        self.pass_until(clients, active, protocol.PRECOMBAT_MAIN)
        grant, land = self.play_a_land(clients, active)

        creature = next(c for c in clients[active].hand(active)
                        if cards.lookup(c).is_creature)
        colour = cards.lookup(land).color
        clients[active].send({"type": "CAST_SPELL", "seq_num": grant["seq_num"],
                              "card_id": creature, "targets": [],
                              "mana_payment": {colour: 1}})
        clients[active].recv_until(protocol.STACK_PUSH)

        # Both players pass, so the creature resolves onto the battlefield.
        self.pass_priority(clients, active)
        self.pass_priority(clients, self.other(active))
        clients[active].recv_until(protocol.STACK_RESOLVE)
        return creature

    def test_summoning_sickness_prevents_attacking(self):
        """A creature that entered play this turn cannot attack unless it has haste."""
        clients, active, _ = self.start_game(deck_one=self.LIONS, deck_two=self.MYSTICS)
        creature = self.cast_a_creature(clients, active)

        clients[active].recv_until(protocol.GAME_STATE_UPDATE)
        entry = next(p for p in clients[active].last_state["battlefield"][active]
                     if p["id"] == creature)
        self.assertTrue(entry["summoning_sick"])

        transition = self.pass_until(clients, active, protocol.DECLARE_ATTACKERS_STEP)
        clients[active].send({
            "type": "DECLARE_ATTACKERS", "seq_num": transition["seq_num"],
            "attackers": [{"creature_id": creature, "target": self.other(active)}]})
        error = clients[active].recv_until(protocol.ERROR)
        self.assertEqual(error["code"], protocol.ILLEGAL_ACTION)
        self.assertIn("summoning sickness", error["message"])

    # Monastery Swiftspear, a 1/2 with haste, on both sides, with different copies.
    HASTE_ONE = instances("mountain", 6) + instances("monastery_swiftspear", 2)
    HASTE_TWO = (instances("mountain", 6, start=7)
                 + instances("monastery_swiftspear", 2, start=3))

    def test_haste_creature_attacks_the_turn_it_arrives(self):
        """Monastery Swiftspear has haste, so summoning sickness does not stop it from attacking."""
        clients, active, _ = self.start_game(deck_one=self.HASTE_ONE,
                                             deck_two=self.HASTE_TWO)
        inactive = self.other(active)
        creature = self.cast_a_creature(clients, active)

        transition = self.pass_until(clients, active, protocol.DECLARE_ATTACKERS_STEP)
        clients[active].send({
            "type": "DECLARE_ATTACKERS", "seq_num": transition["seq_num"],
            "attackers": [{"creature_id": creature, "target": inactive}]})

        # An attacker taps as soon as the player declares it (RFC Section 9.3).
        clients[active].recv_until(protocol.GAME_STATE_UPDATE)
        entry = next(p for p in clients[active].last_state["battlefield"][active]
                     if p["id"] == creature)
        self.assertTrue(entry["tapped"])

        # The defender does not block, and then we go on to the damage.
        blockers = self.pass_until(clients, active, protocol.DECLARE_BLOCKERS_STEP)
        clients[inactive].send({"type": "DECLARE_BLOCKERS",
                                "seq_num": blockers["seq_num"], "blockers": []})
        self.pass_priority(clients, active)
        self.pass_priority(clients, inactive)

        # Nobody blocked, so the power of 1 goes straight to the player.
        result = clients[active].recv_until(protocol.COMBAT_DAMAGE_RESULT)
        self.assertEqual(result["life_totals"][inactive], 19)
        self.assertEqual(result["damage_events"],
                         [{"source": creature, "target": inactive, "amount": 1}])

    def test_declare_attackers_token_is_the_phase_transition(self):
        """DECLARE_ATTACKERS echoes the seq_num of the PHASE_TRANSITION."""
        clients, active, _ = self.start_game()
        transition = self.pass_until(clients, active, protocol.DECLARE_ATTACKERS_STEP)

        clients[active].send({"type": "DECLARE_ATTACKERS",
                              "seq_num": transition["seq_num"] + 99, "attackers": []})
        self.assertEqual(clients[active].recv_until(protocol.ERROR)["code"],
                         protocol.STALE_ACTION)

        # An empty declaration is legal, and it goes straight to End of Combat.
        clients[active].send({"type": "DECLARE_ATTACKERS",
                              "seq_num": transition["seq_num"], "attackers": []})
        end = clients[active].recv_until(
            protocol.PHASE_TRANSITION,
            lambda p: p.get("to_phase") == protocol.END_OF_COMBAT)
        self.assertEqual(end["from_phase"], protocol.DECLARE_ATTACKERS_STEP)


# --- GAME_OVER and session restart (RFC Sections 6.5, 6.6) -----------

class GameOverTests(ServerTestCase):

    def test_concede_ends_the_game_and_returns_to_lobby(self):
        clients, active, grant = self.start_game()
        inactive = self.other(active)

        # Either player can send CONCEDE at any time, even without priority.
        clients[inactive].send({"type": "CONCEDE", "seq_num": grant["seq_num"],
                                "player_id": inactive})
        over = clients[active].recv_until(protocol.GAME_OVER)
        self.assertEqual(over["reason"], protocol.REASON_CONCEDE)
        self.assertEqual(over["winner_id"], active)
        self.assertEqual(over["loser_id"], inactive)

        # The same TCP connections can start a new game (RFC Section 6.6).
        time.sleep(0.3)
        self.send_ready(clients["player_1"], "player_1", self.DECK_ONE)
        clients["player_1"].recv_until(protocol.GAME_STATE_UPDATE)
        self.send_ready(clients["player_2"], "player_2", self.DECK_TWO)

        again = clients["player_1"].recv_until(
            protocol.GAME_STATE_UPDATE,
            lambda p: (p.get("state") or {}).get("phase") == protocol.MULLIGAN)
        self.assertEqual(again["state"]["turn"], 0)
        self.assertEqual(len(clients["player_1"].hand("player_1")), 7)

    def test_disconnect_ends_the_game(self):
        """When a connection drops, the game ends with the reason DISCONNECT."""
        clients, active, _ = self.start_game()
        inactive = self.other(active)

        clients[inactive].close()
        over = clients[active].recv_until(protocol.GAME_OVER)
        self.assertEqual(over["reason"], protocol.REASON_DISCONNECT)
        self.assertEqual(over["winner_id"], active)


if __name__ == "__main__":
    unittest.main(verbosity=2)
