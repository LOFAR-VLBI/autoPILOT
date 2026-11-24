# autoPILOT
Contains scripts used for running the HBA component of the LoTSS survey.

## ILoTSS utilities

Run the examples below from the repository root, or install autoPILOT with
`python -m pip install .`. The ILoTSS code lives in the installed package:

```text
autoPILOT/ilotss/
  assess_calibrators.py           # compatibility import for the ILoTSS DAG
  calibrator_utils/
    assess_calibrators.py          # assessment and ranking
    build_template.py              # reference template generation
    calibrator_medians_cache.h5    # reference template
  archiving_utils/
    archive_surf_tape.py           # packaging and verified upload
  OLD/
    check_ddf_output.py
    cleanup_recalibration.py
    compress_archived.py
    create_ilotss_db.py
    monitor-ilotss.py              # legacy orchestration
```

The checkout scripts `monitor-ilotss.py` and `autoPILOT/monitor-ilotss.py` run
the legacy monitor with execution directories at the repository root. The
installed `monitor_ilotss` command uses the current working directory.
The archiver is a standalone utility and awaits integration into orchestration.
`autoPILOT/utils/calibrator_utils.py` serves the other legacy monitors.

### Calibrator assessment

```python
from autoPILOT.ilotss.calibrator_utils import DEFAULT_TEMPLATE
from autoPILOT.ilotss.calibrator_utils.assess_calibrators import assess_and_compare

ranked = assess_and_compare(DEFAULT_TEMPLATE, ["/path/to/cal_solutions.h5"])
```

Alternatively, use `python -m autoPILOT.ilotss.calibrator_utils.assess_calibrators
--template <template.h5> --h5 <cal_solutions.h5> ...`.

Build a new assessment cache from completed LINC calibrator runs.

```bash
python -m autoPILOT.ilotss.calibrator_utils.build_template \
  --input-glob '/data/calibrators/LINC_calibrator_L*/results_LINC_calibrator/cal_solutions.h5' \
  --output-dir /data/calibrator-template
```

The builder writes `calibrator_medians_cache.h5` and diagnostic plots. Use
`--recompute` to rebuild an existing cache, or `--write-linc-template` to
also write the separate LINC-format `template_median.h5`.

### Calibrator archiving

```python
from autoPILOT.ilotss.archiving_utils.archive_surf_tape import archive_linc_calibrator

receipt = archive_linc_calibrator(
    "/data/published/LINC_calibrator_L858674_2026_11_07-18_29_32",
    "/data/archive-staging",
    rclone_config="/path/to/maca_ilotss.conf",
)
```

The default destination is `maca_ilotss:LINC_calibrators`. Set
`RCLONE_CONFIG_DIR` to the directory containing `maca_ilotss.conf` or pass
its path as `rclone_config`. rclone uses the macaroon when it uploads, and
an authentication error fails the archive call. The function returns a
receipt after verification. Pass `destination` to choose a different archive.

Supply the **published successful run**. The caller remains responsible for
scientific validation. The archiver checks for the LINC calibrator solution file,
JSON summary, inspection products, logs and sky models. It requires all four
raw solution files when the run settings enable them. The tar includes every
entry in the run directory, including any Measurement Sets. It stores symbolic
links with their original targets.

The uncompressed tar uses the published directory name with a `.tar` suffix
and preserves the directory layout. Keep published runs stable while archiving.
A retry can reuse a same-name archive with matching size and checksum. A
conflicting archive causes an error.

Uploads use [rclone's immutable copy option](https://rclone.org/docs/#immutable).
With an explicit SURF WebDAV macaroon config, the archiver compares the local tar
with the size and Adler-32 checksum stored by dCache, using a metadata-only
request. Other configurations use [rclone readback](https://rclone.org/commands/rclone_check/),
which requires download access. The source products and local tar are retained.
The receipt records `verified`, the tar size and Adler-32, and the verification
method after the archive check.

This initial implementation covers calibrators. Target QA and final facet-data
retention, tape staging, scheduling, retry policy and database integration remain
separate work.

### Utility checks

```bash
python -m unittest discover -s tests -v
```

Archive tests use temporary files and mocked transfers. If `rclone` is installed,
an additional integration check transfers between two temporary local directories.
No test uploads to an archive service.

# Requirements

Pip-installable Python requirements are listed in `requirements.txt`. These can be installed via

```bash
pip install -r requirements.txt
```

Other requirements are:

* https://git.astron.nl/astron-sdc/lofar_stager_api.git
* https://github.com/mhardcastle/lotss-query
* https://github.com/mhardcastle/ddf-pipeline (ddf-pipeline/utils and ddf-pipeline/scripts on PYTHONPATH)
* https://github.com/LOFAR-VLBI/lofar-vlbi-pipeline

# Environment setup
The following environment variables should be set:

* `DDF_PIPELINE_CLUSTER`: the name of the cluster you are running on, e.g. `cosma`, `spider`, `Herts` etc.
* `DATA_DIR`: directory where data will be stored while processing.
* `LOFAR_SINGULARITY`: path to an Apptainer/Singularity container with the required processing software.
* `$HOME/.surveys` with surveys database login credentials.
* `$HOME/.stagingrc` with LTA login credentials.
