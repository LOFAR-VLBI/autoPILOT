#!/usr/bin/env python3
"""Checkout launcher for the legacy ILoTSS monitor."""

from pathlib import Path
import sys


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from autoPILOT.ilotss.OLD import main

    main(root)
