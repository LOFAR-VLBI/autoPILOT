"""Launcher for the legacy ILoTSS monitor."""

from pathlib import Path
import runpy


def main():
    runpy.run_path(
        str(Path(__file__).resolve().with_name("monitor-ilotss.py")),
        run_name="__main__",
    )
