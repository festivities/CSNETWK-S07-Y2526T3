"""
Protocol conformance tests for MTGNP 1.0.

These run the real server over real TCP sockets and talk to it with a deliberately
"dumb" test client that sends hand-written PDUs, so framing, sequence-number
tokens and error codes are exercised exactly as a third-party client would
exercise them.

Only the standard library is used -- no third-party test runner needed:

    python -m unittest discover -s tests -v

Tests avoid depending on the shuffle by using decks made up only of the cards a
test needs: with an eight-card deck every possible seven-card opening hand
contains at least three of each card, so nothing is left to chance.
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

# Each test binds its own port so the tests are fully independent.
_next_port = 4600


def _allocate_port() -> int:
    global _next_port
    _next_port += 1
    return _next_port


def instances(base: str, count: int, start: int = 1) -> list:
    """["mountain_001", ...] -- `count` instances of one card, from `start`.

    `start` lets the two players draw different copies of the same card, since
    both decks come from one shared fixed set and may not overlap.
    """
    return [f"{base}_{i:03d}" for i in range(start, start + count)]


class RawClient:
    """A minimal MTGNP client that sends exactly the PDUs a test tells it to."""

    def __init__(self, port: int):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=5)
        # The most recent in-game GAME_STATE_UPDATE, so tests can read the hand
        # and battlefield the server last reported.
        self.last_state: dict = {}

    def send(self, pdu: dict) -> None:
        protocol.send_pdu(self.socket, pdu)

    def send_raw(self, payload: bytes) -> None:
        """Send a hand-built frame, for testing malformed payloads."""
        self.socket.sendall(len(payload).to_bytes(4, "big") + payload)

    def recv(self) -> dict:
        pdu = protocol.recv_pdu(self.socket)
        if pdu.get("type") == protocol.GAME_STATE_UPDATE:
            state = pdu.get("state") or {}
            if state.get("phase") not in (protocol.LOBBY, protocol.GAME_SETUP):
                self.last_state = state
        return pdu

    def recv_until(self, pdu_type: str, where=None, limit: int = 400) -> dict:
        """Read until a PDU of `pdu_type` (optionally matching `where`) arrives."""
        for _ in range(limit):
            pdu = self.recv()
            if pdu.get("type") == pdu_type and (where is None or where(pdu)):
                return pdu
        raise AssertionError(f"never received {pdu_type}")

    def hand(self, player_id: str) -> list:
        """Our hand, accepting either the object or array shape for `hand`."""
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
    """Base class that boots a fresh server for each test."""

    # Default decks: deliberately symmetric, so it does not matter which player
    # wins the coin flip -- whoever is active holds the same kinds of card.
    #
    # Each is eight cards, and the opening hand is seven, so exactly one card is
    # left in the library.  That makes hand contents predictable without relying
    # on the shuffle: with two Lightning Bolts in an eight-card deck, at least one
    # of them is always in the opening hand, and at least five Mountains are too.
    DECK_ONE = instances("mountain", 6) + instances("lightning_bolt", 2)
    DECK_TWO = instances("mountain", 6, start=7) + instances("lightning_bolt", 2, start=3)

    def setUp(self):
        self.port = _allocate_port()
        self.server = MTGNPServer(host="127.0.0.1", port=self.port,
                                  verbose=False, quiet=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.clients = []
        time.sleep(0.25)   # Let the listening socket come up.

    def tearDown(self):
        for client in self.clients:
            client.close()
        if self.server._listener is not None:
            self.server._listener.close()
        # Shut the server's sockets so its reader threads finish. They are daemon
        # threads, so a thread still inside a print() when the interpreter exits
        # can die holding the stdout lock, which Python reports as a fatal error
        # after the run. Closing here lets them exit on their own instead.
        for connection in self.server.live_connections():
            connection.mark_closed()

    def connect(self) -> RawClient:
        client = RawClient(self.port)
        self.clients.append(client)
        return client

    # --- Getting a game under way ----------------------------------------

    def send_ready(self, client: RawClient, player_id: str, deck: list) -> None:
        client.send({"type": "PLAYER_READY", "seq_num": 1,
                     "player_id": player_id, "deck_list": deck})

    def start_game(self, deck_one=None, deck_two=None):
        """Bring two clients to the first priority window of turn 1.

        Returns (clients_by_id, active_player_id, first_priority_grant).
        """
        one, two = self.connect(), self.connect()

        # Send the two PLAYER_READY PDUs one at a time so their arrival order is
        # deterministic; otherwise which deck is shuffled first is a race.
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

        # The Upkeep Step's grant is now outstanding: the server is blocked
        # waiting for this player to act on it.  Remember it so take_grant can
        # hand it to the first caller instead of waiting for a second grant that
        # would never come.
        grant = clients[active].recv_until(protocol.PRIORITY_GRANT)
        self.pending_grant = (active, grant)
        return clients, active, grant

    def other(self, player_id: str) -> str:
        return "player_2" if player_id == "player_1" else "player_1"

    def take_grant(self, clients, player_id: str) -> dict:
        """The PRIORITY_GRANT this player must answer next.

        Returns an already-received but unanswered grant if there is one, so a
        test never waits for a grant the server has in fact already sent.
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
        """Both players pass priority until `target_phase` begins.

        Returns the PHASE_TRANSITION that announced it.
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
        """Play the first land in hand during a Main Phase; returns the new grant."""
        grant = self.take_grant(clients, active)
        land = next(c for c in clients[active].hand(active) if cards.lookup(c).is_land)
        clients[active].send({"type": "PLAY_LAND", "seq_num": grant["seq_num"],
                              "card_id": land})
        return clients[active].recv_until(protocol.PRIORITY_GRANT), land


# --- Framing and PDU structure (RFC Sections 5.2, 5.4) ------------------

class FramingTests(ServerTestCase):

    def test_length_prefixed_frame_round_trip(self):
        """A PDU is framed with a 4-byte big-endian length and parses back."""
        client = self.connect()
        client.send({"type": "PING", "seq_num": 7, "timestamp": 1234})
        pong = client.recv_until(protocol.PONG)
        self.assertEqual(pong["seq_num"], 7)
        self.assertEqual(pong["timestamp"], 1234)

    def test_oversized_pdu_is_refused(self):
        """A PDU may not exceed 65,535 bytes (RFC Section 5.2)."""
        sock = socket.socket()
        with self.assertRaises(protocol.PDUTooLarge):
            protocol.send_pdu(sock, {"type": "PING", "seq_num": 1,
                                     "pad": "x" * (protocol.MAX_PAYLOAD_BYTES + 1)})
        sock.close()

    def test_partial_reads_are_reassembled(self):
        """recv_pdu must reassemble a frame delivered in several TCP segments."""
        client = self.connect()
        payload = b'{"type": "PING", "seq_num": 42, "timestamp": 9}'
        client.socket.sendall(len(payload).to_bytes(4, "big"))
        time.sleep(0.05)                       # Force a separate segment.
        client.socket.sendall(payload[:10])
        time.sleep(0.05)
        client.socket.sendall(payload[10:])
        self.assertEqual(client.recv_until(protocol.PONG)["seq_num"], 42)

    def test_invalid_json_is_reported_and_connection_kept(self):
        """A well-framed but unparseable payload yields ERROR/INVALID_JSON."""
        client = self.connect()
        client.send_raw(b"{this is not json")
        self.assertEqual(client.recv_until(protocol.ERROR)["code"],
                         protocol.INVALID_JSON)

        # The connection must survive an illegal PDU, so a later PDU still works.
        client.send({"type": "PING", "seq_num": 99, "timestamp": 1})
        self.assertEqual(client.recv_until(protocol.PONG)["seq_num"], 99)

    def test_unknown_pdu_type_is_rejected(self):
        client = self.connect()
        client.send({"type": "TELEPORT_CREATURE", "seq_num": 3})
        self.assertEqual(client.recv_until(protocol.ERROR)["code"],
                         protocol.UNKNOWN_TYPE)

    def test_server_pdus_carry_type_and_seq_num(self):
        """type and seq_num are REQUIRED in every PDU (RFC Section 5.4)."""
        clients, active, grant = self.start_game()
        self.assertIn("type", grant)
        self.assertIsInstance(grant["seq_num"], int)
        self.assertEqual(grant["player_id"], active)
        self.assertIn("time_limit_ms", grant)


# --- TCP server behaviour (RFC Section 5.1) -----------------------------

class ConnectionTests(ServerTestCase):

    def test_third_connection_is_refused(self):
        """Only two players may be seated; further attempts are refused."""
        self.connect()
        self.connect()
        third = self.connect()
        third.socket.settimeout(3)
        with self.assertRaises((protocol.ConnectionClosed, OSError)):
            third.recv()

    def test_slot_frees_up_after_disconnect(self):
        """A disconnect releases a player slot for a replacement connection."""
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
        """Only four copies of Lightning Bolt exist, so _005 is not a real card."""
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
        """Both decks come from one shared set, so an instance cannot be in both."""
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
        """A player sees their own hand, but only a count of the opponent's."""
        clients, active, _ = self.start_game()
        state = clients["player_1"].last_state

        self.assertEqual(len(clients["player_1"].hand("player_1")), 7)
        self.assertNotIn("player_2", state.get("hand", {}))
        self.assertEqual(state["hand_counts"]["player_2"], 7)
        # Libraries are always counts only, never card lists.
        self.assertIsInstance(state["library_counts"]["player_2"], int)

    def test_mulligan_requires_exactly_n_cards_bottomed(self):
        """Keeping after one mulligan must bottom exactly one card."""
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

        # Keeping with zero bottomed cards is now illegal.
        one.send({"type": "MULLIGAN_CHOICE", "seq_num": redraw["seq_num"],
                  "keep": True, "cards_to_bottom": []})
        self.assertEqual(one.recv_until(protocol.ERROR)["code"], protocol.ILLEGAL_ACTION)

        # Keeping with exactly one is accepted, leaving a six-card hand.
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

        # start_game already consumed the Untap and Upkeep transitions on its way
        # to the first priority window, so the observed sequence resumes at Draw.
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
        """The opening hand stays at seven through the first Draw Step."""
        clients, active, _ = self.start_game()
        self.pass_until(clients, active, protocol.PRECOMBAT_MAIN)
        self.assertEqual(len(clients[active].hand(active)), 7)
        self.assertEqual(clients[active].last_state["turn"], 1)

    def test_untap_step_grants_no_priority(self):
        """The first PRIORITY_GRANT of the game belongs to the Upkeep Step."""
        clients, active, grant = self.start_game()
        # start_game stops at the first grant; the phase then is UPKEEP, not UNTAP.
        upkeep = clients[active].last_state
        self.assertEqual(upkeep["phase"], protocol.UNTAP)   # last state update
        self.assertEqual(grant["player_id"], active)        # AP acts first


# --- Priority, tokens and the stack (RFC Sections 8.1-8.5) -----------

class PriorityTests(ServerTestCase):

    def test_stale_seq_num_is_rejected_and_priority_reissued(self):
        clients, active, grant = self.start_game()
        clients[active].send({"type": "PRIORITY_PASS",
                              "seq_num": grant["seq_num"] - 5})
        error = clients[active].recv_until(protocol.ERROR)
        self.assertEqual(error["code"], protocol.STALE_ACTION)
        self.assertEqual(error["rejected_action"]["type"], "PRIORITY_PASS")
        # The server re-issues PRIORITY_GRANT so the player may try again.
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
        # Only after the AP passes does the NAP get priority.
        nap_grant = clients[self.other(active)].recv_until(protocol.PRIORITY_GRANT)
        self.assertEqual(nap_grant["player_id"], self.other(active))

    def test_land_only_in_main_phase(self):
        """Playing a land during Upkeep is a WRONG_PHASE error."""
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
        # The Active Player retains priority after playing a land.
        self.assertEqual(grant["player_id"], active)

    def test_mana_payment_must_match_the_printed_cost(self):
        """An empty payment does not satisfy Lightning Bolt's {R}."""
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
        """The right payment still fails with no land on the battlefield."""
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
        """A full cast -> STACK_PUSH -> both pass -> STACK_RESOLVE cycle."""
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

        # The caster retains priority; both then pass so the spell resolves.
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
        # The spell went to its owner's graveyard, and the stack is empty again.
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

    # Three one-mana creatures each, so at least two are always in the opening
    # hand whichever player wins the coin flip.
    SORCERY_ONE = instances("mountain", 5) + instances("monastery_swiftspear", 3)
    SORCERY_TWO = instances("plains", 5) + instances("savannah_lions", 3)

    def test_sorcery_speed_spell_needs_an_empty_stack(self):
        """A creature cannot be cast while a spell is already on the stack."""
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

        # A second creature at sorcery speed is illegal with a non-empty stack.
        # Timing is checked before mana, so this is WRONG_PHASE, not a mana error.
        clients[active].send({"type": "CAST_SPELL", "seq_num": grant["seq_num"],
                              "card_id": creatures[1], "targets": [],
                              "mana_payment": {colour: 1}})
        error = clients[active].recv_until(protocol.ERROR)
        self.assertEqual(error["code"], protocol.WRONG_PHASE)


# --- Combat (RFC Section 9) ---------------------------------------------

class CombatTests(ServerTestCase):

    # One-mana creatures with no haste, from disjoint colours, so whichever
    # player wins the coin flip can perform the test.
    LIONS = instances("plains", 4) + instances("savannah_lions", 4)
    MYSTICS = instances("forest", 4) + instances("elvish_mystic", 4)

    def cast_a_creature(self, clients, active):
        """Play a land and cast a one-mana creature; returns its card id."""
        self.pass_until(clients, active, protocol.PRECOMBAT_MAIN)
        grant, land = self.play_a_land(clients, active)

        creature = next(c for c in clients[active].hand(active)
                        if cards.lookup(c).is_creature)
        colour = cards.lookup(land).color
        clients[active].send({"type": "CAST_SPELL", "seq_num": grant["seq_num"],
                              "card_id": creature, "targets": [],
                              "mana_payment": {colour: 1}})
        clients[active].recv_until(protocol.STACK_PUSH)

        # Both pass so the creature resolves onto the battlefield.
        self.pass_priority(clients, active)
        self.pass_priority(clients, self.other(active))
        clients[active].recv_until(protocol.STACK_RESOLVE)
        return creature

    def test_summoning_sickness_prevents_attacking(self):
        """A creature that arrived this turn cannot attack without haste."""
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

    # Monastery Swiftspear (1/2, haste) on both sides, using different copies.
    HASTE_ONE = instances("mountain", 6) + instances("monastery_swiftspear", 2)
    HASTE_TWO = (instances("mountain", 6, start=7)
                 + instances("monastery_swiftspear", 2, start=3))

    def test_haste_creature_attacks_the_turn_it_arrives(self):
        """Monastery Swiftspear has haste, so summoning sickness does not stop it."""
        clients, active, _ = self.start_game(deck_one=self.HASTE_ONE,
                                             deck_two=self.HASTE_TWO)
        inactive = self.other(active)
        creature = self.cast_a_creature(clients, active)

        transition = self.pass_until(clients, active, protocol.DECLARE_ATTACKERS_STEP)
        clients[active].send({
            "type": "DECLARE_ATTACKERS", "seq_num": transition["seq_num"],
            "attackers": [{"creature_id": creature, "target": inactive}]})

        # Declaring an attacker taps it immediately (RFC Section 9.3).
        clients[active].recv_until(protocol.GAME_STATE_UPDATE)
        entry = next(p for p in clients[active].last_state["battlefield"][active]
                     if p["id"] == creature)
        self.assertTrue(entry["tapped"])

        # Let the defender decline to block, then push through to damage.
        blockers = self.pass_until(clients, active, protocol.DECLARE_BLOCKERS_STEP)
        clients[inactive].send({"type": "DECLARE_BLOCKERS",
                                "seq_num": blockers["seq_num"], "blockers": []})
        self.pass_priority(clients, active)
        self.pass_priority(clients, inactive)

        # Unblocked, so Swiftspear's power of 1 goes straight to the player.
        result = clients[active].recv_until(protocol.COMBAT_DAMAGE_RESULT)
        self.assertEqual(result["life_totals"][inactive], 19)
        self.assertEqual(result["damage_events"],
                         [{"source": creature, "target": inactive, "amount": 1}])

    def test_declare_attackers_token_is_the_phase_transition(self):
        """DECLARE_ATTACKERS echoes the PHASE_TRANSITION's seq_num."""
        clients, active, _ = self.start_game()
        transition = self.pass_until(clients, active, protocol.DECLARE_ATTACKERS_STEP)

        clients[active].send({"type": "DECLARE_ATTACKERS",
                              "seq_num": transition["seq_num"] + 99, "attackers": []})
        self.assertEqual(clients[active].recv_until(protocol.ERROR)["code"],
                         protocol.STALE_ACTION)

        # An empty declaration is legal and skips straight to End of Combat.
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

        # CONCEDE may be sent by either player at any time, even without priority.
        clients[inactive].send({"type": "CONCEDE", "seq_num": grant["seq_num"],
                                "player_id": inactive})
        over = clients[active].recv_until(protocol.GAME_OVER)
        self.assertEqual(over["reason"], protocol.REASON_CONCEDE)
        self.assertEqual(over["winner_id"], active)
        self.assertEqual(over["loser_id"], inactive)

        # The same TCP connections may start a new game (RFC Section 6.6).
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
        """Losing a connection ends the game with reason DISCONNECT."""
        clients, active, _ = self.start_game()
        inactive = self.other(active)

        clients[inactive].close()
        over = clients[active].recv_until(protocol.GAME_OVER)
        self.assertEqual(over["reason"], protocol.REASON_DISCONNECT)
        self.assertEqual(over["winner_id"], active)


if __name__ == "__main__":
    unittest.main(verbosity=2)
