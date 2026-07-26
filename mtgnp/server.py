"""
The MTGNP Game Server: TCP sockets, accepting clients, dispatch, and the heartbeat.

What the server does (RFC Section 4.2). It holds the only authoritative copy of
the game state, checks every PDU that a client sends, runs all the phase and step
transitions, manages the stack, works out the combat damage, sees when a player
wins or loses, and filters the hidden information out of the state update of each
player.

Socket layout
-------------
One listening socket on port 4444 accepts exactly two players, and the server
refuses every other connection (RFC Section 5.1). Each accepted socket gets a
reader thread that frames the incoming PDUs and pushes them onto the inbox of the
engine. One game thread, which is the main thread, runs the rules. The reader
thread answers PING with PONG on its own, because a heartbeat does not read the
game state.
"""

import argparse
import socket
import threading

from . import engine as engine_module
from . import lifecycle, protocol
from .verbose import VerboseLogger

# How we label the two player slots before the clients choose their own IDs.
SLOT_LABELS = protocol.PLAYER_SLOT_LABELS
MAX_PLAYERS = protocol.MAX_PLAYERS


class ClientConnection:
    """One connected player: the socket, its reader thread, and its name."""

    def __init__(self, sock: socket.socket, address, label: str, logger, inbox):
        self.socket = sock
        self.address = address
        self.label = label            # The slot name, which we use until PLAYER_READY arrives.
        self.player_id = None         # The client chooses this in PLAYER_READY.
        self.logger = logger
        self.inbox = inbox
        self.open = True
        # The game thread and the reader thread of this connection can both send,
        # because the reader thread answers PING. The lock keeps the writes from
        # running into each other.
        self._send_lock = threading.Lock()

    @property
    def name(self) -> str:
        """The best name we have for the log, which is the chosen ID or the slot."""
        return self.player_id or self.label

    # --- Sending ---------------------------------------------------------

    def send(self, pdu: dict) -> None:
        if not self.open:
            return
        try:
            with self._send_lock:
                protocol.send_pdu(self.socket, pdu)
            self.logger.sent(self.name, pdu)
        except (OSError, protocol.PDUTooLarge) as exc:
            self.logger.note(f"failed to send to {self.name}: {exc}")
            self.mark_closed()

    # --- Receiving -------------------------------------------------------

    def read_loop(self) -> None:
        """Read PDUs off the socket until it closes and put them on the inbox of the engine."""
        while self.open:
            try:
                pdu = protocol.recv_pdu(self.socket)
            except protocol.InvalidJSON as exc:
                # The frame itself was fine, but the payload was not JSON, so the
                # stream is still in sync. We report the problem and keep the
                # connection open.
                self.logger.note(f"invalid JSON from {self.name}: {exc}")
                self.send({
                    "type": protocol.ERROR,
                    "seq_num": 0,
                    "code": protocol.INVALID_JSON,
                    "message": f"Payload was not valid UTF-8 JSON: {exc}",
                    "rejected_action": {},
                })
                continue
            except (protocol.ConnectionClosed, OSError):
                break

            self.logger.received(self.name, pdu)

            # A heartbeat does not read the game state, so we answer it here
            # instead of giving it to the game thread (RFC Sections 4.3 and
            # 10.2.25).
            if pdu.get("type") == protocol.PING:
                self.send({
                    "type": protocol.PONG,
                    "seq_num": pdu.get("seq_num"),
                    "timestamp": pdu.get("timestamp"),
                })
                continue

            self.inbox.put((self, pdu))

        self.mark_closed()
        # We tell the game thread that this player is gone.
        self.inbox.put((self, engine_module.DISCONNECTED))

    # --- Teardown --------------------------------------------------------

    def mark_closed(self) -> None:
        if not self.open:
            return
        self.open = False
        try:
            self.socket.close()
        except OSError:
            pass


class MTGNPServer:
    """Accepts two players and runs one game after another for them."""

    def __init__(self, host: str = "0.0.0.0", port: int = protocol.DEFAULT_PORT,
                 verbose: bool = False, pretty: bool = False,
                 time_limit_ms: int = engine_module.DEFAULT_TIME_LIMIT_MS,
                 quiet: bool = False):
        self.host = host
        self.port = port
        self.logger = VerboseLogger("SERVER", enabled=verbose, pretty=pretty, quiet=quiet)
        self.engine = engine_module.GameEngine(self.logger, time_limit_ms=time_limit_ms)
        self.connections: list = []
        self._connections_lock = threading.Lock()
        self._listener: socket.socket | None = None

    # --- Connection bookkeeping ------------------------------------------

    def live_connections(self) -> list:
        """The connections that are open right now, in the order we accepted them."""
        with self._connections_lock:
            return [c for c in self.connections if c.open]

    def _free_label(self) -> str:
        """The name of the first player slot that nobody uses."""
        taken = {c.label for c in self.live_connections()}
        return next(label for label in SLOT_LABELS if label not in taken)

    # --- Serving ---------------------------------------------------------

    def serve_forever(self) -> None:
        """Bind the socket, listen on it, and then run games until someone stops us."""
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # This lets us start the server again right after a shutdown without the
        # "address in use" error.
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, self.port))
        # The backlog is a little larger than two, so we still have room to
        # accept a third connection and refuse it properly.
        self._listener.listen(MAX_PLAYERS + 2)

        self.logger.note(f"listening on {self.host}:{self.port} "
                         f"(verbose {'ON' if self.logger.enabled else 'OFF'})")
        self.logger.note("press 'v' then Enter to toggle verbose mode")

        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._verbose_toggle_loop, daemon=True).start()

        # The loop runs once per game. After GAME_OVER the server goes back to
        # LOBBY and uses the same TCP connections again (RFC Section 6.6).
        while True:
            ready = lifecycle.run_lobby(self.engine, self)
            lifecycle.run_game(self.engine, ready)
            self._reset_for_next_game()

    def _reset_for_next_game(self) -> None:
        """Go back to the LOBBY state, and keep the TCP connections open."""
        self.engine.state = None
        self.engine.connections = {}
        # We clear the player IDs at the start of every LOBBY state, so a player
        # can use the same ID again in the next game (RFC Section 6.2).
        for connection in self.live_connections():
            connection.player_id = None
        # We throw away anything a client sent after GAME_OVER but before the new
        # lobby started.
        while not self.engine.inbox.empty():
            self.engine.inbox.get()
        self.logger.note("returning to LOBBY; both players must send PLAYER_READY again")

    def _accept_loop(self) -> None:
        """Accept up to two players and refuse everyone else (RFC Section 5.1)."""
        while True:
            try:
                sock, address = self._listener.accept()
            except OSError:
                return

            if len(self.live_connections()) >= MAX_PLAYERS:
                # The game already has two players, so we refuse this one.
                self.logger.note(f"refused connection from {address}: "
                                 f"{MAX_PLAYERS} players already connected")
                try:
                    sock.close()
                except OSError:
                    pass
                continue

            # We turn off the Nagle algorithm, because our PDUs are small and we
            # want them to go out right away.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            connection = ClientConnection(sock, address, self._free_label(),
                                          self.logger, self.engine.inbox)
            with self._connections_lock:
                # We drop the connections that already closed, so their slots
                # become free again.
                self.connections = [c for c in self.connections if c.open]
                self.connections.append(connection)

            self.logger.note(f"accepted {connection.label} from {address}")
            threading.Thread(target=connection.read_loop, daemon=True).start()

    def _verbose_toggle_loop(self) -> None:
        """Let the user turn verbose mode on and off while the server runs by typing 'v'."""
        while True:
            try:
                line = input()
            except (EOFError, OSError):
                return   # There is no console, for example when we pipe the output.
            if line.strip().lower() in {"v", "verbose"}:
                self.logger.toggle()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="MTGNP 1.0 Game Server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="the interface to bind (default: all of them)")
    parser.add_argument("--port", type=int, default=protocol.DEFAULT_PORT,
                        help=f"the TCP port to listen on (default: {protocol.DEFAULT_PORT})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every PDU we send and receive, starting at startup")
    parser.add_argument("--pretty", action="store_true",
                        help="indent the PDU JSON over several lines in the verbose output")
    parser.add_argument("--time-limit-ms", type=int,
                        default=engine_module.DEFAULT_TIME_LIMIT_MS,
                        help="the response deadline that we announce in PRIORITY_GRANT")
    args = parser.parse_args(argv)

    server = MTGNPServer(host=args.host, port=args.port, verbose=args.verbose,
                         pretty=args.pretty, time_limit_ms=args.time_limit_ms)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.logger.note("shutting down")
