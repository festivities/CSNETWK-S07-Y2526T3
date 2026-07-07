"""
The MTGNP Game Server: TCP sockets, client acceptance, dispatch and heartbeat.

Responsibilities (RFC Section 4.2): the server holds the single authoritative
copy of the game state, validates every PDU a client sends, drives all phase and
step transitions, manages the stack, computes combat damage, detects win
conditions, and filters hidden information out of each player's state updates.

Socket layout
-------------
One listening socket on port 4444 accepts exactly two players.  Any further
connection attempt is refused (RFC Section 5.1).  Each accepted socket gets a
reader thread that frames incoming PDUs and pushes them onto the engine's inbox;
one game thread (the main thread) runs the rules.  PING is answered with PONG by
the reader thread itself, since a heartbeat touches no game state.
"""

import argparse
import socket
import threading

from . import engine as engine_module
from . import lifecycle, protocol
from .verbose import VerboseLogger

# How the two player slots are labelled before clients choose their own IDs.
SLOT_LABELS = protocol.PLAYER_SLOT_LABELS
MAX_PLAYERS = protocol.MAX_PLAYERS


class ClientConnection:
    """One connected player: the socket, its reader thread, and its identity."""

    def __init__(self, sock: socket.socket, address, label: str, logger, inbox):
        self.socket = sock
        self.address = address
        self.label = label            # Slot name, used before PLAYER_READY arrives.
        self.player_id = None         # Chosen by the client in PLAYER_READY.
        self.logger = logger
        self.inbox = inbox
        self.open = True
        # The game thread and this connection's reader thread (answering PING)
        # can both send, so serialise writes.
        self._send_lock = threading.Lock()

    @property
    def name(self) -> str:
        """Best available name for logging: the chosen id, else the slot."""
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
        """Frame PDUs off the socket until it closes, feeding the engine's inbox."""
        while self.open:
            try:
                pdu = protocol.recv_pdu(self.socket)
            except protocol.InvalidJSON as exc:
                # The frame was well formed but its payload was not JSON, so the
                # stream is still in sync: report it and keep the connection.
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

            # A heartbeat is stateless, so answer it here rather than making the
            # game thread deal with it (RFC Sections 4.3, 10.2.25).
            if pdu.get("type") == protocol.PING:
                self.send({
                    "type": protocol.PONG,
                    "seq_num": pdu.get("seq_num"),
                    "timestamp": pdu.get("timestamp"),
                })
                continue

            self.inbox.put((self, pdu))

        self.mark_closed()
        # Tell the game thread this player is gone.
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
    """Accepts two players and runs games for them back to back."""

    def __init__(self, host: str = "0.0.0.0", port: int = protocol.DEFAULT_PORT,
                 verbose: bool = False, pretty: bool = False,
                 time_limit_ms: int = engine_module.DEFAULT_TIME_LIMIT_MS):
        self.host = host
        self.port = port
        self.logger = VerboseLogger("SERVER", enabled=verbose, pretty=pretty)
        self.engine = engine_module.GameEngine(self.logger, time_limit_ms=time_limit_ms)
        self.connections: list = []
        self._connections_lock = threading.Lock()
        self._listener: socket.socket | None = None

    # --- Connection bookkeeping ------------------------------------------

    def live_connections(self) -> list:
        """Currently open connections, in the order they were accepted."""
        with self._connections_lock:
            return [c for c in self.connections if c.open]

    def _free_label(self) -> str:
        """The first unused player slot name."""
        taken = {c.label for c in self.live_connections()}
        return next(label for label in SLOT_LABELS if label not in taken)

    # --- Serving ---------------------------------------------------------

    def serve_forever(self) -> None:
        """Bind, listen, then run games until interrupted."""
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Allows an immediate restart after shutdown without "address in use".
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, self.port))
        self._listener.listen(MAX_PLAYERS + 2)   # Backlog leaves room to refuse.

        self.logger.note(f"listening on {self.host}:{self.port} "
                         f"(verbose {'ON' if self.logger.enabled else 'OFF'})")
        self.logger.note("press 'v' then Enter to toggle verbose mode")

        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._verbose_toggle_loop, daemon=True).start()

        # One iteration per game.  After GAME_OVER the server returns to LOBBY
        # and reuses the same TCP connections (RFC Section 6.6).
        while True:
            ready = lifecycle.run_lobby(self.engine, self)
            lifecycle.run_game(self.engine, ready)
            self._reset_for_next_game()

    def _reset_for_next_game(self) -> None:
        """Return to the LOBBY state, keeping the TCP connections open."""
        self.engine.state = None
        self.engine.connections = {}
        # Player IDs are reset at the start of each LOBBY state, so the same ID
        # may be reused in the next game (RFC Section 6.2).
        for connection in self.live_connections():
            connection.player_id = None
        # Drop anything a client sent after GAME_OVER but before the new lobby.
        while not self.engine.inbox.empty():
            self.engine.inbox.get()
        self.logger.note("returning to LOBBY; both players must send PLAYER_READY again")

    def _accept_loop(self) -> None:
        """Accept up to two players; refuse anyone else (RFC Section 5.1)."""
        while True:
            try:
                sock, address = self._listener.accept()
            except OSError:
                return

            if len(self.live_connections()) >= MAX_PLAYERS:
                # Two players are already seated, so this attempt is refused.
                self.logger.note(f"refused connection from {address}: "
                                 f"{MAX_PLAYERS} players already seated")
                try:
                    sock.close()
                except OSError:
                    pass
                continue

            # Disable Nagle's algorithm: our PDUs are small and latency-sensitive.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            connection = ClientConnection(sock, address, self._free_label(),
                                          self.logger, self.engine.inbox)
            with self._connections_lock:
                # Forget connections that have already closed, so slots free up.
                self.connections = [c for c in self.connections if c.open]
                self.connections.append(connection)

            self.logger.note(f"accepted {connection.label} from {address}")
            threading.Thread(target=connection.read_loop, daemon=True).start()

    def _verbose_toggle_loop(self) -> None:
        """Let the operator toggle verbose mode at runtime by typing 'v'."""
        while True:
            try:
                line = input()
            except (EOFError, OSError):
                return   # No console attached (for example, output is piped).
            if line.strip().lower() in {"v", "verbose"}:
                self.logger.toggle()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="MTGNP 1.0 Game Server")
    parser.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all)")
    parser.add_argument("--port", type=int, default=protocol.DEFAULT_PORT,
                        help=f"TCP port to listen on (default: {protocol.DEFAULT_PORT})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every PDU sent and received at startup")
    parser.add_argument("--pretty", action="store_true",
                        help="indent PDU JSON across multiple lines in verbose output")
    parser.add_argument("--time-limit-ms", type=int,
                        default=engine_module.DEFAULT_TIME_LIMIT_MS,
                        help="response deadline advertised in PRIORITY_GRANT")
    args = parser.parse_args(argv)

    server = MTGNPServer(host=args.host, port=args.port, verbose=args.verbose,
                         pretty=args.pretty, time_limit_ms=args.time_limit_ms)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.logger.note("shutting down")
