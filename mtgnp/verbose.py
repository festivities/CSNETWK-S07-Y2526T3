"""
Verbose mode: labeled console logging of every PDU sent and received.

The rubric requires that the client and the server can both print every PDU that
crosses the socket, and that we can turn this on and off while the program is
running. Each program owns one VerboseLogger. The `--verbose` option turns it on
at startup, and typing `v` on the server or `verbose` on a client toggles it.

Output format, one PDU per entry:

    [SERVER] --> SENT     to player_1   PRIORITY_GRANT   (seq_num=8)
        {"type": "PRIORITY_GRANT", "player_id": "player_1", ...}
"""

import json
import threading


class VerboseLogger:
    """Prints PDUs in a readable and clearly labeled format when it is on."""

    def __init__(self, label: str, enabled: bool = False, pretty: bool = False,
                 quiet: bool = False):
        self.label = label          # "SERVER" or "CLIENT"
        self.enabled = enabled
        self.pretty = pretty        # Indent the JSON body across several lines.
        # Turn off even the operational notes. The test suite uses this, because
        # it runs many servers at once and does not want their console output.
        self.quiet = quiet
        # Several threads print here, such as the reader threads, the game
        # thread, and the heartbeat thread. The lock keeps the lines from mixing
        # into each other.
        self._lock = threading.Lock()

    # --- Toggling ---------------------------------------------------------

    def toggle(self) -> bool:
        """Switch verbose mode on or off and return the new state."""
        self.enabled = not self.enabled
        self.note(f"verbose mode {'ON' if self.enabled else 'OFF'}")
        return self.enabled

    # --- PDU logging ------------------------------------------------------

    def sent(self, peer: str, pdu: dict) -> None:
        self._log_pdu("--> SENT    ", f"to {peer}", pdu)

    def received(self, peer: str, pdu: dict) -> None:
        self._log_pdu("<-- RECEIVED", f"from {peer}", pdu)

    def _log_pdu(self, direction: str, peer: str, pdu: dict) -> None:
        if not self.enabled:
            return
        pdu_type = pdu.get("type", "<no type>")
        seq_num = pdu.get("seq_num", "-")
        body = json.dumps(pdu, indent=2) if self.pretty else json.dumps(pdu)
        with self._lock:
            # We flush so the log stays in step with the game even when stdout
            # goes to a file or a pipe, where Python would otherwise hold the
            # output in a buffer.
            print(f"[{self.label}] {direction} {peer:<22} {pdu_type:<24} (seq_num={seq_num})",
                  flush=True)
            print(f"    {body}", flush=True)

    # --- Plain messages ---------------------------------------------------

    def note(self, message: str) -> None:
        """Print an operational message. This is shown whether verbose is on or off.

        We use it for socket events such as binding, accepting a client, and
        disconnects. The instructor needs to see these even when verbose mode is
        turned off.
        """
        if self.quiet:
            return
        with self._lock:
            print(f"[{self.label}] {message}", flush=True)
