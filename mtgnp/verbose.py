"""
Verbose mode: labelled console logging of every PDU sent and received.

The rubric requires that both the client and the server be able to print every
PDU crossing the socket, and that this be toggleable at runtime.  Both programs
own one VerboseLogger; `--verbose` switches it on at startup and typing `v`
(server) or `verbose` (client) toggles it while running.

Output format, one PDU per entry:

    [SERVER] --> SENT     to player_1   PRIORITY_GRANT   (seq_num=8)
        {"type": "PRIORITY_GRANT", "player_id": "player_1", ...}
"""

import json
import threading


class VerboseLogger:
    """Prints PDUs in a readable, clearly labelled format when enabled."""

    def __init__(self, label: str, enabled: bool = False, pretty: bool = False,
                 quiet: bool = False):
        self.label = label          # "SERVER" or "CLIENT"
        self.enabled = enabled
        self.pretty = pretty        # Indent the JSON body across several lines.
        # Silence even the operational notes. Used by the test suite, which runs
        # many servers at once and does not want their console output.
        self.quiet = quiet
        # Printing happens from several threads (reader threads, the game
        # thread, the heartbeat thread), so serialise it to keep lines intact.
        self._lock = threading.Lock()

    # --- Toggling ---------------------------------------------------------

    def toggle(self) -> bool:
        """Flip verbose mode and return the new state."""
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
            # flush so the log stays in step with the game even when stdout is
            # redirected to a file or a pipe, where Python would otherwise buffer.
            print(f"[{self.label}] {direction} {peer:<22} {pdu_type:<24} (seq_num={seq_num})",
                  flush=True)
            print(f"    {body}", flush=True)

    # --- Plain messages ---------------------------------------------------

    def note(self, message: str) -> None:
        """Print an operational message (always shown, verbose or not).

        Used for socket lifecycle events -- binding, accepting, disconnects --
        which the instructor needs to see even with verbose mode off.
        """
        if self.quiet:
            return
        with self._lock:
            print(f"[{self.label}] {message}", flush=True)
