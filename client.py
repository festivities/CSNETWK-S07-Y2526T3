#!/usr/bin/env python3
"""
The entry point of an MTGNP Player Client.

    python client.py --player-id player_1 --deck decks/burn.txt --verbose

Run it with --help to see every option. You can also turn verbose mode on and
off at any prompt by typing 'verbose'.
"""

from mtgnp.client import main

if __name__ == "__main__":
    main()
