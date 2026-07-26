"""
MTGNP - Magic: The Gathering Multiplayer Network Protocol, version 1.0.

Our implementation of RFC 0001 (CSNETWK), a TCP based, message oriented,
client-server protocol for two-player simplified Magic: The Gathering games.

Module map, which follows the same order as the Work Distribution Matrix:

    protocol.py   Message framing, PDU type names, error codes.
    verbose.py    Verbose mode: labeled logging of every PDU sent and received.
    cards.py      The fixed card catalog, loaded from the master card list.
    state.py      The authoritative game state and the per-player filtering.
    mana.py       Mana payment checking and tapping of mana sources.
    effects.py    Card effects: spells, activated abilities, triggered abilities.
    engine.py     Shared engine parts: sequence numbers, send and wait, errors.
    lifecycle.py  The LOBBY, GAME_SETUP, MULLIGAN and GAME_OVER states.
    priority.py   Priority windows and the stack (LIFO) resolution loop.
    turns.py      Turn structure: every phase and step, in order.
    combat.py     The combat phase sub-state machine.
    server.py     TCP server: sockets, accepting clients, dispatch, heartbeat.
    client.py     Player client: rendering, user input, heartbeat.
"""

__version__ = "1.0"
