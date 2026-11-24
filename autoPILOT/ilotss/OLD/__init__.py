"""Launcher for the legacy ILoTSS monitor."""

from pathlib import Path
import runpy


def main(run_root=None):
    runpy.run_path(
        str(Path(__file__).resolve().with_name("monitor-ilotss.py")),
        init_globals={"_RUN_ROOT": Path.cwd() if run_root is None else run_root},
        run_name="__main__",
    )
