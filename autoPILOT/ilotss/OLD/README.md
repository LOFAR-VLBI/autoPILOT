# Legacy ILoTSS scripts

`monitor-ilotss.py` is the original monolithic monitor, retained for reference
and compatibility while reusable functions are developed in the sibling
`calibrator_utils` and `archiving_utils` packages.

Both checkout launchers, `monitor-ilotss.py` and `autoPILOT/monitor-ilotss.py`,
forward here and create `ILoTSS-run-L<target>` at the repository root. The
installed `monitor_ilotss` command creates the directory in the working
directory.
The assessment import and template location follow the new package layout;
processing, upload and cleanup decisions retain the legacy behaviour.

In particular, the legacy monitor still uses its original archive functions.
It does not yet call the new verified calibrator archiver. Import utilities
from the sibling packages rather than importing this script: the monitor
executes orchestration at module scope and requires the processing environment.

The standalone maintenance scripts also live here.

- `check_ddf_output.py` checks DDF output.
- `cleanup_recalibration.py` cleans recalibration runs.
- `compress_archived.py` compresses archived products.
- `create_ilotss_db.py` creates and populates the legacy SQLite database.
