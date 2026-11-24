"""Compatibility entry point; use autoPILOT.ilotss.calibrator_utils.assess_calibrators."""

from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autoPILOT.ilotss.calibrator_utils.assess_calibrators import *  # noqa: F401,F403,E402
from autoPILOT.ilotss.calibrator_utils.assess_calibrators import main


if __name__ == "__main__":
    main()
