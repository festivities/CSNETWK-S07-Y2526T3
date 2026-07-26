#!/usr/bin/env python3
"""
The entry point of the MTGNP Game Server.

    python server.py --verbose

Run it with --help to see every option. You can also turn verbose mode on and
off while the server runs by typing 'v' and pressing Enter.
"""

from mtgnp.server import main

if __name__ == "__main__":
    main()
