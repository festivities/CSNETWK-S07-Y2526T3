"""
MTGNP - Magic: The Gathering Multiplayer Network Protocol, version 1.0.

An implementation of RFC 0001 (CSNETWK): a TCP-based, message-oriented,
client-server protocol for two-player simplified Magic: The Gathering games.

Module map (mirrors the Work Distribution Matrix):

    protocol.py   Message framing, PDU type names, error codes.
    verbose.py    Verbose mode: labelled logging of every PDU sent/received.
    cards.py      The fixed card catalog, loaded from the master card list.
    state.py      Authoritative game state and per-player visible-state filtering.
    mana.py       Mana payment validation and tapping of mana sources.
    effects.py    Card effects: spells, activated abilities, triggered abilities.
    engine.py     Shared engine plumbing: sequence numbers, send/await, errors.
    lifecycle.py  LOBBY, GAME_SETUP, MULLIGAN and GAME_OVER states.
    priority.py   Priority windows and the stack (LIFO) resolution loop.
    turns.py      Turn structure: every phase and step, in order.
    combat.py     Combat phase sub-state machine.
    server.py     TCP server: sockets, accepting clients, dispatch, heartbeat.
    client.py     Player client: rendering, user input, heartbeat.
"""

__version__ = "1.0"
