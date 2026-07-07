#!/usr/bin/env python3
"""
Entry point for the MTGNP Game Server.

    python server.py --verbose

Run with --help for every option.  Verbose mode can also be toggled while the
server is running by typing 'v' and pressing Enter.
"""

from mtgnp.server import main

if __name__ == "__main__":
    main()
