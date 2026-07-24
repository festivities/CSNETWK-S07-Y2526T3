# MTGNP — Work Distribution Matrix

CSNETWK · T3 AY 2025–2026 · Machine Problem: Magic: The Gathering Multiplayer
Network Protocol (MTGNP) v1.0, per RFC 0001.

This matrix follows the format required by the machine problem instructions. It is
reproduced in `README.pdf`; this file is the source of truth kept alongside the code.

> **Before submission:** replace the handles below with each member's full name as
> registered in the course, and confirm every row against `git log` output.

| Member | Git author |
| ------ | ---------- |
| Member 1 | festivities `<admin@festivity.moe>` |
| Member 2 | Marco Gerard D. Mendoza `<marco_gerard_mendoza@dlsu.edu.ph>` |
| Member 3 | awynee `<164032813+awynee@users.noreply.github.com>` |

`P` = primary author (designed and wrote it) · `S` = supporting (reviewed, tested,
or contributed fixes).

| Task / Feature | Member 1 | Member 2 | Member 3 |
| -------------- | :------: | :------: | :------: |
| TCP Server: connection handling, framing, dispatch | **P** | S | |
| Game lifecycle: LOBBY, GAME_SETUP, MULLIGAN logic | S | **P** | |
| Turn & phase engine (all phases/steps, transitions) | | S | **P** |
| Priority & Stack logic, spell/ability resolution | S | | **P** |
| Combat system (attackers, blockers, damage) | | S | **P** |
| Client implementation & state rendering | S | | **P** |
| PDU serialisation/deserialisation (all 25 PDU types) | **P** | S | |
| Error handling, PING/PONG heartbeat, disconnect logic | **P** | | S |
| Verbose mode (client + server PDU logging, toggle on/off) | **P** | | S |
| Card catalog, mana payment & card effects | | **P** | S |
| Game state management & hidden-information filtering | | **P** | S |
| Testing & interoperability | **P** | S | S |
| README / documentation / AI disclosure | S | **P** | S |

## Where each task lives in the code

The project is deliberately split so that every matrix row maps onto one or two
files, which makes each member's contribution easy to locate and explain.

| Task / Feature | Primary | Files |
| -------------- | :-----: | ----- |
| TCP Server: connection handling, framing, dispatch | 1 | `mtgnp/server.py`, `server.py` |
| Game lifecycle: LOBBY, GAME_SETUP, MULLIGAN | 2 | `mtgnp/lifecycle.py` |
| Turn & phase engine | 3 | `mtgnp/turns.py` |
| Priority & Stack logic | 3 | `mtgnp/priority.py` |
| Combat system | 3 | `mtgnp/combat.py` |
| Client implementation & rendering | 3 | `mtgnp/client.py`, `client.py` |
| PDU serialisation / framing / error codes | 1 | `mtgnp/protocol.py` |
| Engine plumbing: seq_num, send/await, state-based actions, triggers | 1 | `mtgnp/engine.py` |
| Verbose mode | 1 | `mtgnp/verbose.py` |
| Card catalog | 2 | `mtgnp/cards.py`, `data/MTGNP_MASTER-CARD-LIST.tsv` |
| Mana payment | 2 | `mtgnp/mana.py` |
| Card effects (spells, abilities, triggers) | 2 | `mtgnp/effects.py` |
| Game state & hidden information | 2 | `mtgnp/state.py` |
| Sample decks | 2 | `decks/*.txt` |
| Testing | 1 | `tests/test_mtgnp.py` (protocol over sockets), `tests/test_rules.py` (rules) |

## Rubric coverage

Each base-rubric criterion and where it is implemented and demonstrated.

| Rubric criterion | Pts | Implemented in | Demonstrated by |
| ---------------- | :-: | -------------- | --------------- |
| Verbose mode (prerequisite) | — | `mtgnp/verbose.py` | `--verbose` flag; type `v` (server) / `verbose` (client) to toggle |
| TCP Server Setup & Client Accept | 10 | `mtgnp/server.py` | `ConnectionTests` |
| Message Framing | 5 | `mtgnp/protocol.py` | `FramingTests` |
| PDU Structure & seq_num | 5 | `mtgnp/protocol.py`, `mtgnp/engine.py` | `FramingTests`, `PriorityTests` |
| LOBBY & PLAYER_READY Handling | 10 | `mtgnp/lifecycle.py` | `LobbyTests` |
| GAME_SETUP & MULLIGAN | 5 | `mtgnp/lifecycle.py` | `SetupTests` |
| IN_GAME Phase & Step Transitions | 10 | `mtgnp/turns.py` | `TurnStructureTests` |
| GAME_OVER & Session Restart | 5 | `mtgnp/lifecycle.py` | `GameOverTests` |
| Game State Management & Hidden Info | 10 | `mtgnp/state.py` | `VisibleStateTests`, `SetupTests` |
| Priority & Stack Resolution (≥5 card effects) | 10 | `mtgnp/priority.py`, `mtgnp/effects.py` | `PriorityTests`, `EffectTests`, `StackResolutionTests` |
| Combat System | 10 | `mtgnp/combat.py` | `CombatTests`, `FirstStrikeTests`, `DamageOrderTests` |
| Client Sending & State Rendering | 5 | `mtgnp/client.py` | live demo |
| PING/PONG Heartbeat | 5 | `mtgnp/client.py`, `mtgnp/server.py` | `FramingTests.test_length_prefixed_frame_round_trip` |
| Error PDU Handling | 5 | `mtgnp/engine.py` + validation in each rules module | error-code tests throughout |
| Readability & Comments | 5 | whole codebase | RFC section references in every module |

The two bonus criteria (implementing *every* card ability, and extra features such
as a GUI or spectator client) are intentionally **not** attempted; this build
targets the 100-point base rubric.
