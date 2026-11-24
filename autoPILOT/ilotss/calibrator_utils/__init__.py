"""ILoTSS calibrator assessment and selection utilities.

Import assessment functions from ``autoPILOT.ilotss.calibrator_utils.assess_calibrators``.
"""

from pathlib import Path


DEFAULT_TEMPLATE = Path(__file__).resolve().parent / "calibrator_medians_cache.h5"
