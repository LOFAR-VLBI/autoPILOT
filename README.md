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
    retrieve_surf_tape.py          # download and restore
  OLD/
    check_ddf_output.py
    cleanup_recalibration.py
    compress_archived.py
    create_ilotss_db.py
    monitor-ilotss.py              # legacy orchestration
```

The installed `monitor_ilotss` command runs the legacy script in `ilotss/OLD/`
and creates run directories in the current working directory.
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
    cleanup_tar=True,
)
```

The default destination is `maca_ilotss:LINC_calibrators`. Set
`RCLONE_CONFIG_DIR` to the directory containing `maca_ilotss.conf` or pass
its path as `rclone_config`. rclone uses the macaroon when it uploads, and
an authentication error fails the archive call. The function returns a
receipt after verification. Pass `destination` to choose a different archive.

Supply the **published successful run** or its `results_LINC_calibrator/`
directory. The caller remains responsible for scientific validation. The
archiver checks for the LINC calibrator solution file, JSON summary,
inspection products, logs and sky models. It requires all four
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
which requires download access. Set `cleanup_tar=True` to remove the staged tar
after verification. In that case the receipt has `archive_path=None`.
The receipt records `verified`, the tar size and Adler-32, and the verification
method after the archive check.

Restore an online archive using its remote path. Stage tape-only files with
SURF ADA before calling the restore function. [SURF staging documentation](https://doc.spider.surfsara.nl/en/latest/Pages/storage/ada-interface.html#staging)

```python
from autoPILOT.ilotss.archiving_utils.retrieve_surf_tape import retrieve_surf_archive

restored = retrieve_surf_archive(
    receipt["remote_path"], "/data/restored", rclone_config="/path/to/maca_ilotss.conf"
)
```

### LINC target archiving

Use `archive_linc_target(run_directory, archive_directory)` from
`archive_surf_tape`. It returns separate receipts for
`solutions-and-diagnostics` and `averaged-ms`. Both archives keep the published
run name, including its timestamp, as the tar filename. Under
`maca_ilotss:LINC_target_NL`, the first archive contains the non-MS products
under `results_LINC_target/`. The second contains the averaged
`*_pre-cal.ms` sets. Full-resolution `*.dp3concat` sets stay outside both
archives.

Use each receipt's `remote_path` with `retrieve_surf_archive`. Choose separate
output directories when restoring both archives from the same run.

### DDF-pipeline archiving and restore

`archive_ddf_pipeline` writes LoTSS-style tars under `maca_ilotss:DDF-pipeline/L<obsid>/`.
Restore the default split UV bundles together into a new run directory.

```python
from autoPILOT.ilotss.archiving_utils.retrieve_surf_tape import retrieve_ddf_pipeline

restored = retrieve_ddf_pipeline("L2019041", "/data/restored/L2019041")
```

Pass `products="images"` for images alone, or select other tar names without
the `.tar` suffix. The retriever includes `uv_misc` with `uv_L<obsid>` and
checks that the restored links resolve. You can also pass explicit tar paths.

### DD-calibration archiving

`archive_pilot_dd_calibration` archives a finished widefield run to
`maca_ilotss:VLBI_dd-calibration/<run>.tar`. It keeps `merged.h5`, the images,
QA products, configuration files and published logs. The individual solution
H5s and MeasurementSets are excluded. Restore by timestamped run name for
intermediate imaging or facet subtraction after DD-cal finalization.

```python
from autoPILOT.ilotss.archiving_utils.retrieve_surf_tape import retrieve_pilot_dd_calibration

restored = retrieve_pilot_dd_calibration(
    "VLBI_dd-calibration_L858656_2026_11_19-11_42_12", "/data/restored"
)
```

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
