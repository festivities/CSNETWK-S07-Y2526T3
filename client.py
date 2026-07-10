#!/usr/bin/env python3
"""
Entry point for an MTGNP Player Client.

    python client.py --player-id player_1 --deck decks/burn.txt --verbose

Run with --help for every option.  Verbose mode can also be toggled at any prompt
by typing 'verbose'.
"""

from mtgnp.client import main

if __name__ == "__main__":
    main()
