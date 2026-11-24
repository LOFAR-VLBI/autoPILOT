#!/usr/bin/env python3
"""Compatibility launcher for the legacy ILoTSS monitor."""

from pathlib import Path

from autoPILOT.ilotss.OLD import main


if __name__ == "__main__":
    main(Path(__file__).resolve().parent)
