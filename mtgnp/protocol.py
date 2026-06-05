"""
The MTGNP wire protocol: message framing, PDU type names, and error codes.

This module is deliberately the only place in the project that knows how a PDU
becomes bytes on a TCP socket.  Everything above it deals in plain Python
dictionaries.

Framing (RFC 0001, Section 5.2)
-------------------------------

     0                   1                   2                   3
    +---------------------------------------------------------------+
    |                    Message Length (32 bits)                   |
    +---------------------------------------------------------------+
    |                  JSON Payload (variable length)               |
    +---------------------------------------------------------------+

Each PDU is a UTF-8 JSON object preceded by its byte length as a 4-byte
big-endian unsigned integer.  A receiver MUST read exactly that many bytes
before attempting to parse the JSON.  A PDU MUST NOT exceed 65,535 bytes.
"""

import json
import socket

# --- Framing constants (RFC Section 5.1, 5.2) -------------------------------

DEFAULT_PORT = 4444        # The default MTGNP server port.
LENGTH_PREFIX_BYTES = 4    # Size of the big-endian length prefix.
MAX_PAYLOAD_BYTES = 65535  # A PDU MUST NOT exceed this many bytes.

# Exactly two players are seated per game (RFC Section 5.1).  These labels name
# the two seats before the clients choose their own IDs in PLAYER_READY, and are
# what the lobby reports in `waiting_for`.
MAX_PLAYERS = 2
PLAYER_SLOT_LABELS = ("player_1", "player_2")


# --- Framing errors --------------------------------------------------------

class ConnectionClosed(Exception):
    """The peer closed the TCP connection (or it broke)."""


class InvalidJSON(Exception):
    """A frame was received but its payload was not valid UTF-8 JSON.

    The frame length was valid, so the stream is still in sync and the
    connection can be kept open -- the server answers with ERROR/INVALID_JSON.
    """


class PDUTooLarge(Exception):
    """A PDU exceeded MAX_PAYLOAD_BYTES and cannot be framed."""


# --- Sending and receiving -------------------------------------------------

def send_pdu(sock: socket.socket, pdu: dict) -> None:
    """Serialise `pdu` to JSON and write one length-prefixed frame."""
    payload = json.dumps(pdu).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise PDUTooLarge(f"PDU is {len(payload)} bytes; maximum is {MAX_PAYLOAD_BYTES}")

    header = len(payload).to_bytes(LENGTH_PREFIX_BYTES, byteorder="big", signed=False)
    # sendall loops internally until every byte has been handed to the kernel.
    sock.sendall(header + payload)


def recv_pdu(sock: socket.socket) -> dict:
    """Read exactly one length-prefixed frame and return the decoded PDU.

    Raises ConnectionClosed if the peer hangs up, and InvalidJSON if the
    payload is not parseable (the caller may then keep the connection open).
    """
    header = _recv_exactly(sock, LENGTH_PREFIX_BYTES)
    length = int.from_bytes(header, byteorder="big", signed=False)

    if length > MAX_PAYLOAD_BYTES:
        # A frame this large is not valid MTGNP; we cannot trust the stream.
        raise ConnectionClosed(f"Declared PDU length {length} exceeds {MAX_PAYLOAD_BYTES}")

    payload = _recv_exactly(sock, length)
    try:
        pdu = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidJSON(str(exc)) from exc

    if not isinstance(pdu, dict):
        raise InvalidJSON("PDU payload must be a JSON object")
    return pdu


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    """Read exactly `count` bytes from the socket.

    A single recv() may return fewer bytes than requested, so we loop until we
    have the whole frame.  This is the heart of correct message framing over a
    TCP byte stream: TCP has no notion of message boundaries, only bytes.
    """
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:  # An empty read means the peer performed an orderly close.
            raise ConnectionClosed("Peer closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# --- PDU type names (RFC Section 10.1) -------------------------------------
# All 25 PDU types defined by MTGNP 1.0.

# Client -> Server (14)
PLAYER_READY = "PLAYER_READY"
MULLIGAN_CHOICE = "MULLIGAN_CHOICE"
PRIORITY_PASS = "PRIORITY_PASS"
CAST_SPELL = "CAST_SPELL"
ACTIVATE_ABILITY = "ACTIVATE_ABILITY"
TRIGGER_ORDER_RESPONSE = "TRIGGER_ORDER_RESPONSE"
TRIGGER_CHOICE_RESPONSE = "TRIGGER_CHOICE_RESPONSE"
DECLARE_ATTACKERS = "DECLARE_ATTACKERS"
DECLARE_BLOCKERS = "DECLARE_BLOCKERS"
ASSIGN_DAMAGE_ORDER = "ASSIGN_DAMAGE_ORDER"
PLAY_LAND = "PLAY_LAND"
DISCARD = "DISCARD"
CONCEDE = "CONCEDE"
PING = "PING"

# Server -> Client / broadcast (11)
GAME_STATE_UPDATE = "GAME_STATE_UPDATE"
PHASE_TRANSITION = "PHASE_TRANSITION"
PRIORITY_GRANT = "PRIORITY_GRANT"
STACK_PUSH = "STACK_PUSH"
TRIGGER_ORDER = "TRIGGER_ORDER"
TRIGGER_CHOICE = "TRIGGER_CHOICE"
STACK_RESOLVE = "STACK_RESOLVE"
COMBAT_DAMAGE_RESULT = "COMBAT_DAMAGE_RESULT"
GAME_OVER = "GAME_OVER"
ERROR = "ERROR"
PONG = "PONG"

CLIENT_PDU_TYPES = frozenset({
    PLAYER_READY, MULLIGAN_CHOICE, PRIORITY_PASS, CAST_SPELL, ACTIVATE_ABILITY,
    TRIGGER_ORDER_RESPONSE, TRIGGER_CHOICE_RESPONSE, DECLARE_ATTACKERS,
    DECLARE_BLOCKERS, ASSIGN_DAMAGE_ORDER, PLAY_LAND, DISCARD, CONCEDE, PING,
})


# --- Error codes (RFC Section 11) ------------------------------------------

INVALID_JSON = "INVALID_JSON"
ILLEGAL_DECK = "ILLEGAL_DECK"
UNKNOWN_TYPE = "UNKNOWN_TYPE"
STALE_ACTION = "STALE_ACTION"
NOT_YOUR_PRIORITY = "NOT_YOUR_PRIORITY"
ILLEGAL_ACTION = "ILLEGAL_ACTION"
ILLEGAL_TARGET = "ILLEGAL_TARGET"
TRIGGER_ORDER_INVALID = "TRIGGER_ORDER_INVALID"
TRIGGER_CHOICE_INVALID = "TRIGGER_CHOICE_INVALID"
INSUFFICIENT_MANA = "INSUFFICIENT_MANA"
WRONG_PHASE = "WRONG_PHASE"
DUPLICATE_ID = "DUPLICATE_ID"


# --- Game lifecycle states (RFC Section 6.1) ------------------------------

LOBBY = "LOBBY"
GAME_SETUP = "GAME_SETUP"
MULLIGAN = "MULLIGAN"
IN_GAME = "IN_GAME"


# --- Phases and steps, in turn order (RFC Section 10.2.4) -----------------

UNTAP = "UNTAP"
UPKEEP = "UPKEEP"
DRAW = "DRAW"
PRECOMBAT_MAIN = "PRECOMBAT_MAIN"
BEGIN_COMBAT = "BEGIN_COMBAT"
DECLARE_ATTACKERS_STEP = "DECLARE_ATTACKERS"
DECLARE_BLOCKERS_STEP = "DECLARE_BLOCKERS"
ASSIGN_DAMAGE_ORDER_STEP = "ASSIGN_DAMAGE_ORDER"
FIRST_STRIKE_DAMAGE = "FIRST_STRIKE_DAMAGE"
COMBAT_DAMAGE = "COMBAT_DAMAGE"
END_OF_COMBAT = "END_OF_COMBAT"
POSTCOMBAT_MAIN = "POSTCOMBAT_MAIN"
END_STEP = "END_STEP"
CLEANUP = "CLEANUP"

MAIN_PHASES = frozenset({PRECOMBAT_MAIN, POSTCOMBAT_MAIN})


# --- GAME_OVER reasons (RFC Section 6.6) ----------------------------------

REASON_LIFE_ZERO = "LIFE_ZERO"
REASON_DECK_EMPTY = "DECK_EMPTY"
REASON_CONCEDE = "CONCEDE"
REASON_DISCONNECT = "DISCONNECT"


# --- Stack item types (RFC Section 8.3) -----------------------------------

ITEM_SPELL = "SPELL"
ITEM_ABILITY = "ABILITY"
ITEM_TRIGGER_ABILITY = "TRIGGER_ABILITY"
