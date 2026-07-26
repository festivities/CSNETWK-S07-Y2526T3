"""
The MTGNP Player Client.

What the client does (RFC Section 4.3). It shows the Visible State of this
player, echoes the right seq_num in every action PDU, treats every
GAME_STATE_UPDATE as the truth, and sends PING heartbeats.

The client works out no game outcomes at all. It never decides whether an action
is legal. It builds the PDU, sends it, and shows whatever the server answers.
Everything it displays came from the server.

Threading model
---------------
A reader thread reads the incoming PDUs onto a queue. A heartbeat thread sends
PING and gives up when no PONG comes back. The main thread takes the PDUs off the
queue, displays each one, and asks the player what to do when the server waits
for an action. Only the main thread asks questions, so two prompts never mix.
"""

import argparse
import queue
import socket
import threading
import time

from . import cards, effects, mana, protocol
from .verbose import VerboseLogger

PING_INTERVAL_SECONDS = 30    # The value that RFC Section 4.3 recommends.
PONG_TIMEOUT_SECONDS = 10     # Also recommended: give up 10 s after a PING with no answer.


class ClientQuit(Exception):
    """The player asked to leave, or the standard input ended.

    The prompt loops keep asking until they get a command they understand, so the
    end of the input has to be an exception and not a value. If we returned a
    string instead, those loops would spin forever once stdin runs out.
    """


class MTGNPClient:
    """A simple client that displays the server state and sends the actions of the player."""

    def __init__(self, player_id: str, deck_list: list, host: str, port: int,
                 verbose: bool = False, pretty: bool = False):
        self.player_id = player_id
        self.deck_list = deck_list
        self.host = host
        self.port = port
        self.logger = VerboseLogger("CLIENT", enabled=verbose, pretty=pretty)

        self.socket: socket.socket | None = None
        self.inbox: queue.Queue = queue.Queue()
        self.running = True

        # PING has its own counter, which has nothing to do with the priority
        # token (RFC Section 5.4).
        self._ping_seq = 0
        self._pong_received = threading.Event()

        # The newest state from the server, and what we still owe the server.
        self.state: dict = {}
        self.kept_hand = False
        # CONCEDE echoes the seq_num of the newest server PDU of any type, and
        # that PDU does not have to be a PRIORITY_GRANT (RFC Section 5.4).
        self._last_server_seq = 0

    # --- Connection ------------------------------------------------------

    def run(self) -> None:
        """Connect, say that we are ready, and then answer what the server asks."""
        self.socket = socket.create_connection((self.host, self.port))
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.logger.note(f"connected to {self.host}:{self.port} as {self.player_id}")
        self.logger.note("type 'help' at any prompt for the command list")

        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

        self.send_player_ready()

        while self.running:
            try:
                pdu = self.inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            if pdu is None:          # This is how the reader thread says the socket closed.
                break
            try:
                self.handle(pdu)
            except ClientQuit:
                self.quit()
                break

        self.logger.note("disconnected")

    def quit(self) -> None:
        """Leave the game. We concede first when a game is running, and then close the socket."""
        self.logger.note("input closed; conceding and exiting")
        if self.state:
            # We try our best here. A client can send CONCEDE at any time, and it
            # echoes the seq_num of the newest server PDU (RFC Section 5.4).
            self.send({"type": protocol.CONCEDE, "seq_num": self._last_server_seq,
                       "player_id": self.player_id})
        self.running = False
        try:
            self.socket.close()
        except OSError:
            pass

    def send(self, pdu: dict) -> None:
        try:
            protocol.send_pdu(self.socket, pdu)
            self.logger.sent("server", pdu)
        except (OSError, protocol.PDUTooLarge) as exc:
            self.logger.note(f"failed to send: {exc}")
            self.running = False

    def _read_loop(self) -> None:
        while self.running:
            try:
                pdu = protocol.recv_pdu(self.socket)
            except protocol.InvalidJSON as exc:
                self.logger.note(f"server sent invalid JSON: {exc}")
                continue
            except (protocol.ConnectionClosed, OSError):
                break
            self.logger.received("server", pdu)
            self.inbox.put(pdu)
        self.running = False
        self.inbox.put(None)

    def _heartbeat_loop(self) -> None:
        """Send PING every so often, and close the connection if the server stops answering."""
        while self.running:
            time.sleep(PING_INTERVAL_SECONDS)
            if not self.running:
                return

            self._ping_seq += 1
            self._pong_received.clear()
            self.send({
                "type": protocol.PING,
                "seq_num": self._ping_seq,
                "timestamp": int(time.time() * 1000),
            })

            if not self._pong_received.wait(PONG_TIMEOUT_SECONDS):
                self.logger.note(f"no PONG within {PONG_TIMEOUT_SECONDS}s; disconnecting")
                self.running = False
                try:
                    self.socket.close()
                except OSError:
                    pass
                return

    # --- Dispatch --------------------------------------------------------

    def handle(self, pdu: dict) -> None:
        """Do whatever one server PDU asks us to do."""
        pdu_type = pdu.get("type")
        seq = pdu.get("seq_num")
        if isinstance(seq, int):
            self._last_server_seq = seq

        if pdu_type == protocol.PONG:
            self._pong_received.set()

        elif pdu_type == protocol.GAME_STATE_UPDATE:
            self.handle_state_update(pdu, seq)

        elif pdu_type == protocol.PHASE_TRANSITION:
            self.handle_phase_transition(pdu, seq)

        elif pdu_type == protocol.PRIORITY_GRANT:
            print(f"\n>>> You have priority ({self.state.get('phase', '?')}), "
                  f"time limit {pdu.get('time_limit_ms')} ms")
            self.prompt_priority_action(seq)

        elif pdu_type == protocol.STACK_PUSH:
            print(f"  [stack] + {pdu.get('stack_item_id')} {pdu.get('item_type')} "
                  f"{cards.name_of(pdu.get('source', ''))}"
                  f"{_targets_suffix(pdu.get('targets'))} "
                  f"(controller {pdu.get('controller')})")

        elif pdu_type == protocol.STACK_RESOLVE:
            print(f"  [stack] - {pdu.get('stack_item_id')} {pdu.get('result')}"
                  f"{_changes_suffix(pdu.get('state_changes'))}")

        elif pdu_type == protocol.COMBAT_DAMAGE_RESULT:
            self.render_combat_result(pdu)

        elif pdu_type == protocol.TRIGGER_ORDER:
            self.prompt_trigger_order(pdu, seq)

        elif pdu_type == protocol.TRIGGER_CHOICE:
            self.prompt_trigger_choice(pdu, seq)

        elif pdu_type == protocol.ERROR:
            print(f"\n  !! ERROR {pdu.get('code')}: {pdu.get('message')}")

        elif pdu_type == protocol.GAME_OVER:
            self.handle_game_over(pdu)

        else:
            print(f"  (unhandled PDU type {pdu_type})")

    def handle_state_update(self, pdu: dict, seq: int) -> None:
        """Take the state of the server as the truth, and then act if it asks us to."""
        state = pdu.get("state") or {}

        if state.get("phase") == protocol.LOBBY or state.get("phase") == protocol.GAME_SETUP:
            print(f"  [lobby] {state.get('players_ready')} ready, "
                  f"waiting for {state.get('waiting_for')}")
            return

        # We throw away anything we worked out here that does not agree with the
        # server (RFC Section 4.3).
        self.state = state
        self.render_board()

        # A GAME_STATE_UPDATE is also the request PDU for two of the decisions.
        if state.get("phase") == protocol.MULLIGAN and not self.kept_hand:
            self.prompt_mulligan(seq)
        elif state.get("phase") == protocol.CLEANUP and self.is_active_player() \
                and len(self.my_hand()) > 7:
            self.prompt_discard(seq)

    def handle_phase_transition(self, pdu: dict, seq: int) -> None:
        """Show the new step, and answer the declaration steps that it asks for."""
        to_phase = pdu.get("to_phase")
        print(f"\n--- Turn {pdu.get('turn')}: {pdu.get('from_phase')} -> {to_phase} "
              f"(active: {pdu.get('active_player')})")

        # PHASE_TRANSITION is what really tells us about the new step, and the
        # server does not send a GAME_STATE_UPDATE after every transition. We copy
        # these three fields in so that our display does not stay one step
        # behind. Everything else still comes only from GAME_STATE_UPDATE.
        self.state["phase"] = to_phase
        self.state["turn"] = pdu.get("turn")
        self.state["active_player"] = pdu.get("active_player")

        # These three steps do not have a request PDU of their own. The
        # transition is the request, and we have to echo its seq_num (RFC
        # Sections 5.4, 9.3 and 9.4).
        active = pdu.get("active_player")
        if to_phase == protocol.DECLARE_ATTACKERS_STEP and active == self.player_id:
            self.prompt_declare_attackers(seq)
        elif to_phase == protocol.DECLARE_BLOCKERS_STEP and active != self.player_id:
            self.prompt_declare_blockers(seq)
        elif to_phase == protocol.ASSIGN_DAMAGE_ORDER_STEP and active == self.player_id:
            self.prompt_damage_orders(seq)

    def handle_game_over(self, pdu: dict) -> None:
        won = pdu.get("winner_id") == self.player_id
        print(f"\n=========== GAME OVER: {'YOU WIN' if won else 'YOU LOSE'} "
              f"({pdu.get('reason')}) ===========")
        print(f"  winner: {pdu.get('winner_id')}   loser: {pdu.get('loser_id')}")

        # The server is back in LOBBY on the same connection, and a new
        # PLAYER_READY starts another game (RFC Section 6.6).
        self.kept_hand = False
        self.state = {}
        if _ask("Play again? [y/N] ").strip().lower().startswith("y"):
            self.send_player_ready()
        else:
            self.running = False

    # --- Outgoing actions ------------------------------------------------

    def send_player_ready(self) -> None:
        # PLAYER_READY has its own counter, and the rule about echoing a priority
        # token does not apply to it (RFC Section 6.2). One per game is enough.
        self._ping_seq = max(self._ping_seq, 0)
        self.send({
            "type": protocol.PLAYER_READY,
            "seq_num": 1,
            "player_id": self.player_id,
            "deck_list": self.deck_list,
        })
        print(f"  sent PLAYER_READY with {len(self.deck_list)} cards; waiting for opponent")

    def prompt_priority_action(self, seq: int) -> None:
        """Ask the player what they want to do while they hold priority."""
        while True:
            parts = _ask("  action> ").split()
            if not parts:
                continue
            command, args = parts[0].lower(), parts[1:]

            if command in {"pass", "p"}:
                self.send({"type": protocol.PRIORITY_PASS, "seq_num": seq})
                return

            if command in {"cast", "c"} and args:
                if self.send_cast_spell(seq, args):
                    return
                continue

            if command in {"land", "l"} and args:
                self.send({"type": protocol.PLAY_LAND, "seq_num": seq, "card_id": args[0]})
                return

            if command in {"ability", "a"} and args:
                if self.send_activate_ability(seq, args):
                    return
                continue

            if command == "concede":
                self.send({"type": protocol.CONCEDE, "seq_num": seq,
                           "player_id": self.player_id})
                return

            self.handle_local_command(command)

    def send_cast_spell(self, seq: int, args: list) -> bool:
        """Build CAST_SPELL. We take the mana payment from the printed cost of the card."""
        card_id = args[0]
        card = cards.lookup(card_id)
        if card is None:
            print(f"  {card_id} is not a card in the fixed set.")
            return False
        self.send({
            "type": protocol.CAST_SPELL,
            "seq_num": seq,
            "card_id": card_id,
            "targets": args[1:],
            "mana_payment": mana.cost_as_payment(card),
        })
        return True

    def send_activate_ability(self, seq: int, args: list) -> bool:
        """Build ACTIVATE_ABILITY from `ability <permanent_id> [index] [target]`."""
        source_id = args[0]
        abilities = effects.abilities_of(source_id)
        if not abilities:
            print(f"  {cards.name_of(source_id)} has no activated ability in our build.")
            return False

        rest = args[1:]
        index = 0
        if rest and rest[0].isdigit():
            index, rest = int(rest[0]), rest[1:]
        if not 0 <= index < len(abilities):
            print(f"  ability index {index} is out of range.")
            return False

        ability = abilities[index]
        self.send({
            "type": protocol.ACTIVATE_ABILITY,
            "seq_num": seq,
            "source_id": source_id,
            "ability_index": index,
            "targets": rest,
            "cost_payment": {
                "tap": ability.requires_tap,
                "mana": mana.cost_as_payment_from_cost(ability.mana_cost),
            },
        })
        return True

    def prompt_mulligan(self, seq: int) -> None:
        """Keep the hand or take a mulligan. Keeping after N mulligans puts exactly N cards on the bottom."""
        hand = self.my_hand()
        print(f"\n>>> Mulligan decision. Your hand ({len(hand)}):")
        for card_id in hand:
            print(f"      {_describe_card(card_id)}")
        print("    'keep [card_id ...]' to keep (list one card per mulligan taken), "
              "or 'mull' to draw a new hand")

        while True:
            parts = _ask("  mulligan> ").split()
            if not parts:
                continue
            command, args = parts[0].lower(), parts[1:]

            if command in {"mull", "mulligan", "no"}:
                self.send({"type": protocol.MULLIGAN_CHOICE, "seq_num": seq,
                           "keep": False, "cards_to_bottom": []})
                return
            if command in {"keep", "k", "yes"}:
                self.kept_hand = True
                self.send({"type": protocol.MULLIGAN_CHOICE, "seq_num": seq,
                           "keep": True, "cards_to_bottom": args})
                return
            self.handle_local_command(command)

    def prompt_discard(self, seq: int) -> None:
        """The Cleanup Step, where the player discards down to 7 cards."""
        hand = self.my_hand()
        print(f"\n>>> Hand size is {len(hand)}; discard down to 7.")
        for card_id in hand:
            print(f"      {_describe_card(card_id)}")

        while True:
            parts = _ask("  discard> ").split()
            if not parts:
                continue
            if parts[0].lower() in {"discard", "d"} and parts[1:]:
                self.send({"type": protocol.DISCARD, "seq_num": seq,
                           "card_ids": parts[1:]})
                return
            if parts[0].lower() not in {"discard", "d"}:
                self.handle_local_command(parts[0].lower())
            else:
                print("  usage: discard <card_id> [card_id ...]")

    def prompt_declare_attackers(self, seq: int) -> None:
        """Declare the attackers. An empty declaration means that we do not attack."""
        print("\n>>> Declare attackers. 'attack <creature_id> ...', or 'attack' for none.")
        for permanent in self.my_battlefield():
            if permanent.get("power") is not None:
                print(f"      {_describe_permanent(permanent)}")

        while True:
            parts = _ask("  attackers> ").split()
            if not parts:
                continue
            if parts[0].lower() in {"attack", "a", "none"}:
                defender = self.opponent_id()
                self.send({
                    "type": protocol.DECLARE_ATTACKERS,
                    "seq_num": seq,
                    "attackers": [{"creature_id": cid, "target": defender}
                                  for cid in parts[1:]],
                })
                return
            if parts[0].lower() == "concede":
                self.send({"type": protocol.CONCEDE, "seq_num": seq,
                           "player_id": self.player_id})
                return
            self.handle_local_command(parts[0].lower())

    def prompt_declare_blockers(self, seq: int) -> None:
        """Declare the blockers as blocker:attacker pairs. An empty list means no blocks."""
        attackers = (self.state.get("combat") or {}).get("attackers") or {}
        print("\n>>> Declare blockers. 'block <blocker_id>:<attacker_id> ...', "
              "or 'block' for none.")
        print(f"    Attacking you: {', '.join(cards.name_of(a) + ' (' + a + ')' for a in attackers) or 'nobody'}")
        for permanent in self.my_battlefield():
            if permanent.get("power") is not None:
                print(f"      {_describe_permanent(permanent)}")

        while True:
            parts = _ask("  blockers> ").split()
            if not parts:
                continue
            if parts[0].lower() in {"block", "b", "none"}:
                pairs, malformed = [], False
                for token in parts[1:]:
                    if ":" not in token:
                        print(f"  '{token}' should look like blocker_id:attacker_id")
                        malformed = True
                        break
                    blocker_id, attacker_id = token.split(":", 1)
                    pairs.append({"creature_id": blocker_id, "blocking_id": attacker_id})
                if malformed:
                    continue
                self.send({"type": protocol.DECLARE_BLOCKERS, "seq_num": seq,
                           "blockers": pairs})
                return
            if parts[0].lower() == "concede":
                self.send({"type": protocol.CONCEDE, "seq_num": seq,
                           "player_id": self.player_id})
                return
            self.handle_local_command(parts[0].lower())

    def prompt_damage_orders(self, seq: int) -> None:
        """Order the blockers of every attacker that two or more creatures block."""
        blocks = (self.state.get("combat") or {}).get("blocks") or {}
        multiply_blocked = {a: b for a, b in blocks.items() if len(b) >= 2}

        print("\n>>> Assign damage order for each multiply-blocked attacker.")
        for attacker_id, blockers in multiply_blocked.items():
            print(f"      {cards.name_of(attacker_id)} ({attacker_id}) "
                  f"is blocked by: {', '.join(blockers)}")

        outstanding = dict(multiply_blocked)
        while outstanding:
            parts = _ask("  order> ").split()
            if not parts:
                continue
            if parts[0].lower() in {"order", "o"} and len(parts) >= 3:
                attacker_id, order = parts[1], parts[2:]
                self.send({"type": protocol.ASSIGN_DAMAGE_ORDER, "seq_num": seq,
                           "attacker_id": attacker_id, "blocker_order": order})
                outstanding.pop(attacker_id, None)
                continue
            if parts[0].lower() in {"order", "o"}:
                print("  usage: order <attacker_id> <blocker_id> <blocker_id> ...")
                continue
            self.handle_local_command(parts[0].lower())

    def prompt_trigger_order(self, pdu: dict, seq: int) -> None:
        """Choose the stack order of our own triggers that fired at the same time."""
        trigger_ids = pdu.get("trigger_ids") or []
        print(f"\n>>> Order your simultaneous triggers: {trigger_ids}")
        print("    The first one listed is placed on the stack first, so it resolves last.")
        while True:
            parts = _ask("  trigger order> ").split()
            if parts and parts[0].lower() in {"order", "o"}:
                parts = parts[1:]
            if sorted(parts) == sorted(trigger_ids):
                self.send({"type": protocol.TRIGGER_ORDER_RESPONSE, "seq_num": seq,
                           "ordered_trigger_ids": parts})
                return
            print(f"  list exactly these ids, in your preferred order: {trigger_ids}")

    def prompt_trigger_choice(self, pdu: dict, seq: int) -> None:
        """Say yes or no to a trigger, and choose a target when the trigger needs one."""
        legal = pdu.get("legal_targets") or []
        print(f"\n>>> Trigger from {cards.name_of(pdu.get('source_id', ''))}: "
              f"{pdu.get('effect_summary')}")
        if pdu.get("requires_target"):
            print(f"    Legal targets: {', '.join(legal) or 'none'}")
        print("    'yes [target]' to use it, 'no' to decline.")

        while True:
            parts = _ask("  trigger> ").split()
            if not parts:
                continue
            command, args = parts[0].lower(), parts[1:]

            if command in {"no", "n", "decline"}:
                self.send({"type": protocol.TRIGGER_CHOICE_RESPONSE, "seq_num": seq,
                           "trigger_id": pdu.get("trigger_id"), "accept": False})
                return
            if command in {"yes", "y", "accept"}:
                self.send({
                    "type": protocol.TRIGGER_CHOICE_RESPONSE,
                    "seq_num": seq,
                    "trigger_id": pdu.get("trigger_id"),
                    "accept": True,
                    "chosen_target": args[0] if args else None,
                })
                return
            self.handle_local_command(command)

    # --- Local commands, which are not part of the protocol ---------------

    def handle_local_command(self, command: str) -> None:
        """The commands that only change this client and send no PDU at all."""
        if command in {"state", "s", "board"}:
            self.render_board()
        elif command in {"hand", "h"}:
            for card_id in self.my_hand():
                print(f"      {_describe_card(card_id)}")
        elif command in {"verbose", "v"}:
            self.logger.toggle()
        elif command in {"help", "?"}:
            _print_help()
        else:
            print(f"  unknown command '{command}'. Type 'help' for the list.")

    # --- Rendering -------------------------------------------------------

    def render_board(self) -> None:
        """Print the Visible State that the server sent us last."""
        state = self.state
        if not state:
            return

        me, opponent = self.player_id, self.opponent_id()
        life = state.get("life_totals") or {}
        libraries = state.get("library_counts") or {}
        hand_counts = state.get("hand_counts") or {}

        print("\n" + "=" * 68)
        print(f" Turn {state.get('turn')} | {state.get('phase')} | "
              f"active: {state.get('active_player')} | "
              f"priority: {state.get('priority_holder')}")
        print(f" Life   you({me}): {life.get(me)}    opponent({opponent}): {life.get(opponent)}")
        print(f" Library  you: {libraries.get(me)}   opponent: {libraries.get(opponent)}"
              f"     Opponent hand: {hand_counts.get(opponent)}")

        stack = state.get("stack") or []
        if stack:
            print(" Stack (top last):")
            for item in stack:
                print(f"    {item.get('stack_item_id')} {item.get('item_type')} "
                      f"{cards.name_of(item.get('source', ''))}"
                      f"{_targets_suffix(item.get('targets'))}")
        else:
            print(" Stack  (empty)")

        battlefield = state.get("battlefield") or {}
        for owner, label in ((me, "Your battlefield"), (opponent, "Opponent battlefield")):
            permanents = battlefield.get(owner) or []
            print(f" {label}:" + ("" if permanents else " (empty)"))
            for permanent in permanents:
                print(f"    {_describe_permanent(permanent)}")

        graveyards = state.get("graveyard") or {}
        for owner, label in ((me, "Your graveyard"), (opponent, "Opponent graveyard")):
            pile = graveyards.get(owner) or []
            if pile:
                print(f" {label}: {', '.join(pile)}")

        combat = state.get("combat") or {}
        if combat.get("attackers"):
            print(f" Combat  attackers: {combat['attackers']}")
            if combat.get("blocks"):
                print(f"         blocks:    {combat['blocks']}")

        hand = self.my_hand()
        print(f" Your hand ({len(hand)}):" + ("" if hand else " (empty)"))
        for card_id in hand:
            print(f"    {_describe_card(card_id)}")
        print("=" * 68)

    def render_combat_result(self, pdu: dict) -> None:
        print("\n  [combat damage]")
        for event in pdu.get("damage_events") or []:
            print(f"    {cards.name_of(event['source'])} deals {event['amount']} "
                  f"to {cards.name_of(event['target'])}")
        print(f"    life totals: {pdu.get('life_totals')}")
        if pdu.get("creatures_died"):
            print(f"    died: {', '.join(pdu['creatures_died'])}")

    # --- Small helpers that read the state from the server ----------------

    def my_hand(self) -> list:
        """Our hand, taken from the last update.

        Section 10.2.2 makes `hand` an object keyed by player, while the prose
        examples in the RFC show a bare array. We accept both shapes.
        """
        hand = self.state.get("hand")
        if isinstance(hand, dict):
            return hand.get(self.player_id) or []
        return hand or []

    def my_battlefield(self) -> list:
        return (self.state.get("battlefield") or {}).get(self.player_id) or []

    def is_active_player(self) -> bool:
        return self.state.get("active_player") == self.player_id

    def opponent_id(self) -> str:
        """Work out the ID of the opponent from the keys that the server sent."""
        for field in ("life_totals", "library_counts", "battlefield", "graveyard"):
            for key in (self.state.get(field) or {}):
                if key != self.player_id:
                    return key
        return "opponent"


# --- Small helpers that format the output ----------------------------------

def _describe_card(card_id: str) -> str:
    card = cards.lookup(card_id)
    if card is None:
        return card_id
    cost = mana.format_cost(card.cost)
    body = f"{card_id:<26} {card.name:<26} {card.card_type:<18} {cost}"
    if card.is_creature:
        body += f"  {card.power}/{card.toughness}"
    return body


def _describe_permanent(permanent: dict) -> str:
    card_id = permanent.get("id", "")
    flags = ["tapped"] if permanent.get("tapped") else []
    # We accept both spellings of the summoning sickness flag.
    if permanent.get("summoning_sick") or permanent.get("summoning_sickness"):
        flags.append("summoning sick")
    body = f"{card_id:<26} {cards.name_of(card_id):<26}"
    if permanent.get("power") is not None:
        body += f" {permanent['power']}/{permanent['toughness']}"
        if permanent.get("damage"):
            body += f" (damage {permanent['damage']})"
    return body + (f"  [{', '.join(flags)}]" if flags else "")


def _targets_suffix(targets) -> str:
    return f" -> {', '.join(targets)}" if targets else ""


def _changes_suffix(changes) -> str:
    if not changes:
        return ""
    parts = []
    for entry in changes:
        # Section 10.2.14 names this `change_type`, and the examples use `type`.
        kind = entry.get("change_type") or entry.get("type")
        detail = entry.get("target", "")
        amount = entry.get("amount")
        parts.append(f"{kind} {detail}" + (f" {amount}" if amount is not None else ""))
    return "  [" + "; ".join(parts) + "]"


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise ClientQuit from None


def _print_help() -> None:
    print("""
  Actions while you hold priority:
    pass                                 pass priority
    cast <card_id> [target]              cast a spell, the mana payment is automatic
    land <card_id>                       play a land, Main Phase only, once per turn
    ability <permanent_id> [index] [target]   activate an ability
    concede                              concede the game

  When the server asks for a declaration:
    keep [card_id ...] | mull            your mulligan decision
    discard <card_id> ...                discard down to 7 cards at Cleanup
    attack [creature_id ...]             declare attackers, no ids means no attack
    block [blocker_id:attacker_id ...]   declare blockers, no pairs means no blocks
    order <attacker_id> <blocker_id> ... assign damage order
    yes [target] | no                    use a trigger or decline it

  Anytime:
    state    reprint the board          hand    list your hand
    verbose  turn PDU logging on/off    help    this message
""")


def load_deck(path: str) -> list:
    """Read a deck file into a list of card instance IDs.

    We accept two kinds of line, and a `#` starts a comment:

        4 lightning_bolt     becomes lightning_bolt_001 up to _004
        mountain_007         is one specific card instance
    """
    deck, used = [], {}

    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue

            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit():
                count, base = int(parts[0]), parts[1]
                for _ in range(count):
                    used[base] = used.get(base, 0) + 1
                    deck.append(f"{base}_{used[base]:03d}")
            else:
                deck.append(parts[0])

    return deck


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="MTGNP 1.0 Player Client")
    parser.add_argument("--player-id", required=True,
                        help="the name to claim in PLAYER_READY, for example player_1")
    parser.add_argument("--deck", required=True,
                        help="the path to a deck file, see the decks folder")
    parser.add_argument("--host", default="127.0.0.1", help="the server address")
    parser.add_argument("--port", type=int, default=protocol.DEFAULT_PORT,
                        help=f"the server port (default: {protocol.DEFAULT_PORT})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every PDU we send and receive, starting at startup")
    parser.add_argument("--pretty", action="store_true",
                        help="indent the PDU JSON over several lines in the verbose output")
    args = parser.parse_args(argv)

    deck_list = load_deck(args.deck)
    client = MTGNPClient(player_id=args.player_id, deck_list=deck_list,
                         host=args.host, port=args.port,
                         verbose=args.verbose, pretty=args.pretty)
    try:
        client.run()
    except (KeyboardInterrupt, ConnectionRefusedError, OSError) as exc:
        client.logger.note(f"stopped: {exc}")
