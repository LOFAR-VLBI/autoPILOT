#!/usr/bin/env python3
"""Create the SQLite database used to monitor ILoTSS processing."""

from __future__ import annotations

import argparse
import pprint
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_TABLE_NAME = "processing_ilotss"
PIPELINE_STATUS_NOTHING = 0
DEFAULT_FREQ_MIN_MHZ = 120.0
DEFAULT_FREQ_MAX_MHZ = 168.0


# SQLite stores datetimes as ISO-8601 strings. Status values use the same initial
# value as PIPELINE_STATUS.nothing in flocs_processing.
COLUMNS = (
    "target_id INTEGER PRIMARY KEY",
    "field TEXT",
    "overall_status TEXT NOT NULL DEFAULT 'Not_started'",
    "project_code TEXT",
    "antenna_set TEXT",
    "integration_hours REAL",
    "dt REAL",
    "nchan INTEGER",
    "nsb INTEGER",
    f"freq_min REAL NOT NULL DEFAULT {DEFAULT_FREQ_MIN_MHZ}",
    f"freq_max REAL NOT NULL DEFAULT {DEFAULT_FREQ_MAX_MHZ}",
    "date TEXT",
    "calibrator1_id INTEGER",
    "calibrator1_dt REAL",
    "calibrator1_nchan INTEGER",
    "calibrator1_nsb INTEGER",
    "calibrator1_name TEXT",
    "calibrator1_date TEXT",
    "calibrator1_elevation_mean REAL",
    "calibrator1_location TEXT",
    "calibrator2_id INTEGER",
    "calibrator2_dt REAL",
    "calibrator2_nchan INTEGER",
    "calibrator2_nsb INTEGER",
    "calibrator2_name TEXT",
    "calibrator2_date TEXT",
    "calibrator2_elevation_mean REAL",
    "calibrator2_location TEXT",
    "calibrator_used_id INTEGER",
    "target_location TEXT",
    "priority INTEGER NOT NULL DEFAULT 0",
    "elevation_mean REAL",
    "elevation_min REAL",
    "elevation_max REAL",
    "nr_international INTEGER",
    "nr_core INTEGER",
    "nr_remote INTEGER",
    "ra REAL",
    "decl REAL",
    "username TEXT",
    "clustername TEXT",
    "processing_start_date TEXT",
    "processing_end_date TEXT",
    "gal_l REAL",
    "gal_b REAL",
    f"status_download INTEGER NOT NULL DEFAULT {PIPELINE_STATUS_NOTHING}",
    f"status_calibrator1 INTEGER NOT NULL DEFAULT {PIPELINE_STATUS_NOTHING}",
    f"status_calibrator2 INTEGER NOT NULL DEFAULT {PIPELINE_STATUS_NOTHING}",
    f"status_linc_target INTEGER NOT NULL DEFAULT {PIPELINE_STATUS_NOTHING}",
    f"status_ddf INTEGER NOT NULL DEFAULT {PIPELINE_STATUS_NOTHING}",
    f"status_vlbi_delay INTEGER NOT NULL DEFAULT {PIPELINE_STATUS_NOTHING}",
    f"status_vlbi_dd INTEGER NOT NULL DEFAULT {PIPELINE_STATUS_NOTHING}",
    f"status_vlbi_ddf_subtract INTEGER NOT NULL DEFAULT {PIPELINE_STATUS_NOTHING}",
    f"status_vlbi_intermediate_img INTEGER NOT NULL DEFAULT {PIPELINE_STATUS_NOTHING}",
    f"status_vlbi_facet_subtract INTEGER NOT NULL DEFAULT {PIPELINE_STATUS_NOTHING}",
    f"status_vlbi_facet_imaging INTEGER NOT NULL DEFAULT {PIPELINE_STATUS_NOTHING}",
    "cpu_hours_download REAL NOT NULL DEFAULT 0",
    "cpu_hours_calibrator1 REAL NOT NULL DEFAULT 0",
    "cpu_hours_calibrator2 REAL NOT NULL DEFAULT 0",
    "cpu_hours_linc_target REAL NOT NULL DEFAULT 0",
    "cpu_hours_ddf REAL NOT NULL DEFAULT 0",
    "cpu_hours_vlbi_delay REAL NOT NULL DEFAULT 0",
    "cpu_hours_vlbi_dd REAL NOT NULL DEFAULT 0",
    "cpu_hours_vlbi_ddf_subtract REAL NOT NULL DEFAULT 0",
    "cpu_hours_intermediate_img REAL NOT NULL DEFAULT 0",
    "cpu_hours_vlbi_facet_subtract REAL NOT NULL DEFAULT 0",
    "cpu_hours_vlbi_facet_imaging REAL NOT NULL DEFAULT 0",
    "cpu_hours_total REAL NOT NULL DEFAULT 0",
    "quicklook TEXT NOT NULL DEFAULT 'No'",
)

def _value(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first non-None attribute supported by an AWLofar object."""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_datetime(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _first_sap(observation: Any) -> Any | None:
    return next(iter(_value(observation, "subArrayPointings", default=()) or ()), None)


def _pointing(sap: Any) -> tuple[float | None, float | None]:
    pointing = _value(sap, "pointing")
    if pointing is None:
        return None, None
    return (
        _value(pointing, "rightAscension", "ra"),
        _value(pointing, "declination", "dec"),
    )


def _field_name(observation: Any, sap: Any) -> str | None:
    name = _value(sap, "targetName", "target", "name")
    if name:
        return str(name)
    name = _value(observation, "processIdentifierName", "targetName", "name")
    return str(name) if name else None


def _project_name(observation: Any) -> str:
    project = observation.get_project()
    return str(_value(project, "name", default=project))


def _archive_location(uri: str | None) -> str | None:
    if not uri:
        return None
    uri_lower = uri.lower()
    if any(site in uri_lower for site in ("sara", "surfsara", "surf.nl")):
        return "SURF"
    if any(site in uri_lower for site in ("juelich", "fz-juelich", "juqueen")):
        return "Juelich"
    if any(site in uri_lower for site in ("poznan", "psnc")):
        return "Poznan"
    return uri.split(":", 1)[0]


def _sky_metadata(
    ra: float | None, dec: float | None, start: datetime | None, duration: float
) -> dict[str, float | None]:
    result = {
        "gal_l": None,
        "gal_b": None,
        "elevation_mean": None,
        "elevation_min": None,
        "elevation_max": None,
    }
    if ra is None or dec is None:
        return result

    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    from astropy.utils import iers

    # Elevation estimates must also work on processing hosts without Internet.
    iers.conf.auto_download = False

    coordinate = SkyCoord(ra=float(ra) * u.deg, dec=float(dec) * u.deg)
    result["gal_l"] = float(coordinate.galactic.l.deg)
    result["gal_b"] = float(coordinate.galactic.b.deg)
    if start is None:
        return result

    # Centre of the LOFAR core; eleven samples adequately capture an 8-hour track.
    lofar = EarthLocation.from_geodetic(
        lon=6.869837540 * u.deg, lat=52.915122495 * u.deg, height=15 * u.m
    )
    start = _as_utc(start)
    sample_times = [start + timedelta(seconds=duration * i / 10) for i in range(11)]
    elevations = coordinate.transform_to(
        AltAz(obstime=Time(sample_times), location=lofar)
    ).alt.deg
    result["elevation_mean"] = float(elevations.mean())
    result["elevation_min"] = float(elevations.min())
    result["elevation_max"] = float(elevations.max())
    return result


def _data_products(
    observation_id: int,
    sap: Any,
    freq_min: float = DEFAULT_FREQ_MIN_MHZ,
    freq_max: float = DEFAULT_FREQ_MAX_MHZ,
) -> list[Any]:
    from awlofar.main.aweimports import CorrelatedDataProduct

    sap_id = _value(sap, "subArrayPointingIdentifier")
    if sap_id is not None:
        query = CorrelatedDataProduct.subArrayPointing.subArrayPointingIdentifier == sap_id
    else:
        query = CorrelatedDataProduct.observation.observationId == observation_id
    query &= CorrelatedDataProduct.isValid == 1
    query &= CorrelatedDataProduct.minimumFrequency >= freq_min
    query &= CorrelatedDataProduct.maximumFrequency <= freq_max
    return list(query)


def _preferred_products(
    products: Iterable[Any], requested_sasid: int | None = None
) -> list[Any]:
    """Select the original SAS/MoM averaged product generation.

    An SAP query may return raw data and one or more pipeline generations. Each
    generation shares a process identifier. Prefer a MoM/SAS generation over
    later LDV products such as TPCOMP recompressions; those have different archive
    identifiers and processing parameters from the SAS dataset being monitored.
    """
    groups: dict[Any, list[Any]] = {}
    for product in products:
        process_id = _value(product, "processIdentifier")
        groups.setdefault(process_id, []).append(product)
    if not groups:
        return []

    if requested_sasid is not None:
        matching_products = [
            product
            for generation in groups.values()
            for product in generation
            if _sas_id([product]) == requested_sasid
        ]
        if not matching_products:
            return []
        return sorted(
            matching_products,
            key=lambda product: int(_value(product, "subband", default=-1)),
        )

    def generation_key(item: tuple[Any, list[Any]]) -> tuple[int, int, datetime]:
        process_id, generation = item
        representative = generation[0]
        pipeline = _value(representative, "pipeline")
        identifier_source = str(
            _value(
                pipeline,
                "observationIdSource",
                default=_value(representative, "processIdentifierSource", default=""),
            )
        ).upper()
        sas_generation = int(identifier_source in {"SAS", "MOM"})
        numeric_id = int(process_id) if process_id is not None else -1
        creation_dates = [
            value
            for product in generation
            if (value := _value(product, "creationDate")) is not None
        ]
        newest_creation = max(creation_dates, default=datetime.min)
        return sas_generation, numeric_id, newest_creation

    _, selected = max(groups.items(), key=generation_key)
    return sorted(selected, key=lambda product: int(_value(product, "subband", default=-1)))


def _sas_id(products: list[Any], fallback: Any = None) -> int | None:
    """Return the pipeline SAS ID represented by a coherent product generation."""
    if products:
        pipeline = _value(products[0], "pipeline")
        pipeline_id = _value(pipeline, "observationId")
        if pipeline_id is not None:
            return int(pipeline_id)
    return int(fallback) if fallback is not None else None


def _product_file_object(product: Any) -> Any | None:
    from awlofar.main.aweimports import FileObject

    return ((FileObject.data_object == product) & (FileObject.isValid > 0)).max(
        "creation_date"
    )


def _archive_location_for_products(products: Iterable[Any]) -> str | None:
    """Find an archive site using the first product with a valid file record."""
    # New archive/recompression generations are more likely to have the currently
    # valid FileObject after an original SAS product has been superseded.
    candidates = sorted(
        products,
        key=lambda product: _value(product, "creationDate", default=datetime.min),
        reverse=True,
    )
    for product in candidates:
        file_object = _product_file_object(product)
        if file_object is None:
            continue
        location = _value(file_object, "location")
        archive_location = _archive_location(
            str(location) if location else _value(file_object, "URI")
        )
        if archive_location is not None:
            return archive_location
    return None


def _calibrators_for(observation: Any, observations: Iterable[Any]) -> list[Any]:
    start = _value(observation, "startTime")
    duration = float(_value(observation, "duration", default=0))
    if start is None:
        return []
    window_end = start + timedelta(seconds=duration) + timedelta(hours=168)
    window_start = start - timedelta(hours=168)
    candidates = [
        item
        for item in observations
        if item is not observation
        and _value(item, "observationId") != _value(observation, "observationId")
        and float(_value(item, "duration", default=0)) < 3600
        and _value(item, "startTime") is not None
        and window_start < _value(item, "startTime") < window_end
    ]
    return sorted(candidates, key=lambda item: abs(_value(item, "startTime") - start))[:2]


def _observation_row(
    observation: Any,
    calibrators: list[Any],
    freq_min: float = DEFAULT_FREQ_MIN_MHZ,
    freq_max: float = DEFAULT_FREQ_MAX_MHZ,
    target_sasid: int | None = None,
) -> dict[str, Any] | None:
    observation_id = int(_value(observation, "observationId"))
    sap = _first_sap(observation)
    if sap is None:
        print(f"Skipping observation {observation_id}: it has no sub-array pointing")
        return None
    all_products = _data_products(observation_id, sap, freq_min, freq_max)
    products = _preferred_products(all_products, target_sasid)
    if not products:
        print(f"Skipping observation {observation_id}: it has no valid correlated data")
        return None

    first_product = products[0]
    target_sas_id = _sas_id(products, observation_id)
    ra, dec = _pointing(sap)
    start = _value(observation, "startTime")
    duration = float(_value(observation, "duration", default=0))
    calibrator_generations = []
    calibrator_all_generations = []
    calibrator_sas_ids = []
    for calibrator in calibrators:
        calibrator_sap_for_id = _first_sap(calibrator)
        calibrator_all_products = (
            _data_products(
                int(_value(calibrator, "observationId")),
                calibrator_sap_for_id,
                freq_min,
                freq_max,
            )
            if calibrator_sap_for_id is not None
            else []
        )
        calibrator_generation = _preferred_products(calibrator_all_products)
        calibrator_all_generations.append(calibrator_all_products)
        calibrator_generations.append(calibrator_generation)
        calibrator_sas_ids.append(
            _sas_id(calibrator_generation, _value(calibrator, "observationId"))
        )
    def calibrator_metadata(index: int) -> dict[str, Any]:
        calibrator = calibrators[index] if len(calibrators) > index else None
        generation = (
            calibrator_generations[index]
            if len(calibrator_generations) > index
            else []
        )
        product = generation[0] if generation else None
        calibrator_sap_for_metadata = _first_sap(calibrator) if calibrator else None
        calibrator_ra, calibrator_dec = (
            _pointing(calibrator_sap_for_metadata)
            if calibrator_sap_for_metadata is not None
            else (None, None)
        )
        calibrator_start = _value(calibrator, "startTime") if calibrator else None
        calibrator_duration = float(
            _value(calibrator, "duration", default=0) if calibrator else 0
        )
        calibrator_elevation_mean = (
            _sky_metadata(
                calibrator_ra,
                calibrator_dec,
                calibrator_start,
                calibrator_duration,
            )["elevation_mean"]
            if calibrator is not None
            else None
        )
        return {
            "sas_id": calibrator_sas_ids[index]
            if len(calibrator_sas_ids) > index
            else None,
            "dt": _value(
                product,
                "integrationInterval",
                "integrationTime",
                "timeIntegrationStep",
            ),
            "nchan": _value(
                product,
                "channelsPerSubband",
                "numberOfChannelsPerSubband",
                "nrOfChannelsPerSubband",
                "numberOfChannels",
            ),
            "nsb": len(generation) or None,
            "name": (
                _field_name(calibrator, calibrator_sap_for_metadata)
                if calibrator is not None
                and calibrator_sap_for_metadata is not None
                else None
            ),
            "date": _iso_datetime(
                calibrator_start
            ),
            "elevation_mean": calibrator_elevation_mean,
            "location": _archive_location_for_products(
                calibrator_all_generations[index]
                if len(calibrator_all_generations) > index
                else generation
            ),
        }

    calibrator1 = calibrator_metadata(0)
    calibrator2 = calibrator_metadata(1)

    row = {
        "target_id": target_sas_id,
        "field": _field_name(observation, sap),
        "overall_status": "Not_started",
        "project_code": _project_name(observation),
        "antenna_set": _value(observation, "antennaSet"),
        "integration_hours": duration / 3600,
        "dt": _value(
            first_product,
            "integrationInterval",
            "integrationTime",
            "timeIntegrationStep",
        ),
        "nchan": _value(
            first_product,
            "channelsPerSubband",
            "numberOfChannelsPerSubband",
            "nrOfChannelsPerSubband",
            "numberOfChannels",
        ),
        "nsb": len(products),
        "freq_min": freq_min,
        "freq_max": freq_max,
        "date": _iso_datetime(start),
        "calibrator1_id": calibrator1["sas_id"],
        "calibrator1_dt": calibrator1["dt"],
        "calibrator1_nchan": calibrator1["nchan"],
        "calibrator1_nsb": calibrator1["nsb"],
        "calibrator1_name": calibrator1["name"],
        "calibrator1_date": calibrator1["date"],
        "calibrator1_elevation_mean": calibrator1["elevation_mean"],
        "calibrator1_location": calibrator1["location"],
        "calibrator2_id": calibrator2["sas_id"],
        "calibrator2_dt": calibrator2["dt"],
        "calibrator2_nchan": calibrator2["nchan"],
        "calibrator2_nsb": calibrator2["nsb"],
        "calibrator2_name": calibrator2["name"],
        "calibrator2_date": calibrator2["date"],
        "calibrator2_elevation_mean": calibrator2["elevation_mean"],
        "calibrator2_location": calibrator2["location"],
        "calibrator_used_id": None,
        "target_location": _archive_location_for_products(all_products),
        "priority": 0,
        "nr_international": _value(observation, "nrStationsInternational"),
        "nr_core": _value(observation, "nrStationsCore"),
        "nr_remote": _value(observation, "nrStationsRemote"),
        "ra": ra,
        "decl": dec,
        "username": None,
        "clustername": None,
        "processing_start_date": None,
        "processing_end_date": None,
        "quicklook": "No",
    }
    row.update(_sky_metadata(ra, dec, start, duration))
    return row


def create_database(
    dbname: str | Path,
    table_name: str = DEFAULT_TABLE_NAME,
    print_when_finished: bool = False,
) -> None:
    """Create an ILoTSS processing table in ``dbname``.

    Date values should be inserted as ISO-8601 strings. If the table already
    exists, it is left intact and any supported schema migrations are applied.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        raise ValueError(f"Invalid SQLite table name: {table_name!r}")

    if _table_exists(dbname, table_name):
        print(
            f"Table {table_name!r} already exists in SQLite database "
            f"{str(dbname)!r}; leaving it unchanged"
        )
        _ensure_schema_columns(dbname, table_name)
        if print_when_finished:
            print_database(dbname, table_name)
        return

    create_sql = f'CREATE TABLE "{table_name}" (\n    '
    create_sql += ",\n    ".join(COLUMNS)
    create_sql += "\n);"

    print(f"Creating table {table_name!r} in SQLite database {str(dbname)!r}")
    with sqlite3.connect(dbname) as connection:
        connection.execute(create_sql)
    if print_when_finished:
        print_database(dbname, table_name)


def print_database(
    dbname: str | Path, table_name: str = DEFAULT_TABLE_NAME
) -> None:
    """Print every row in a database table as a dictionary."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        raise ValueError(f"Invalid SQLite table name: {table_name!r}")

    with sqlite3.connect(dbname) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f'SELECT * FROM "{table_name}" ORDER BY "target_id"'
        ).fetchall()

    print(f"\nContents of {table_name!r} in {str(dbname)!r} ({len(rows)} row(s)):")
    if not rows:
        print("[]")
        return
    for row in rows:
        pprint.pprint(dict(row), sort_dicts=False)


def _table_exists(dbname: str | Path, table_name: str) -> bool:
    with sqlite3.connect(dbname) as connection:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            is not None
        )


def _ensure_schema_columns(dbname: str | Path, table_name: str) -> None:
    """Apply supported column additions to a database made with an older schema."""
    with sqlite3.connect(dbname) as connection:
        existing = {
            row[1]
            for row in connection.execute(f'PRAGMA table_info("{table_name}")')
        }
        renamed_columns = {
            "id": "target_id",
            "sas_id_calibrator1": "calibrator1_id",
            "sas_id_calibrator2": "calibrator2_id",
            "antennaSet": "antenna_set",
            "proccessing_start_date": "processing_start_date",
            "integration": "integration_hours",
            "location": "target_location",
        }
        for old_column, new_column in renamed_columns.items():
            if old_column in existing and new_column not in existing:
                connection.execute(
                    f'ALTER TABLE "{table_name}" RENAME COLUMN '
                    f'"{old_column}" TO "{new_column}"'
                )
                existing.remove(old_column)
                existing.add(new_column)
                print(f"Renamed {old_column} to {new_column} in {table_name!r}")
        if "freq_min" not in existing:
            connection.execute(
                f'ALTER TABLE "{table_name}" ADD COLUMN freq_min REAL NOT NULL '
                f"DEFAULT {DEFAULT_FREQ_MIN_MHZ}"
            )
            print(f"Added freq_min column to {table_name!r}")
        if "freq_max" not in existing:
            connection.execute(
                f'ALTER TABLE "{table_name}" ADD COLUMN freq_max REAL NOT NULL '
                f"DEFAULT {DEFAULT_FREQ_MAX_MHZ}"
            )
            print(f"Added freq_max column to {table_name!r}")
        if "antenna_set" not in existing:
            connection.execute(
                f'ALTER TABLE "{table_name}" ADD COLUMN antenna_set TEXT'
            )
            print(f"Added antenna_set column to {table_name!r}")
        if "integration_hours" not in existing:
            connection.execute(
                f'ALTER TABLE "{table_name}" ADD COLUMN integration_hours REAL'
            )
            print(f"Added integration_hours column to {table_name!r}")
        if "processing_start_date" not in existing:
            connection.execute(
                f'ALTER TABLE "{table_name}" ADD COLUMN processing_start_date TEXT'
            )
            print(f"Added processing_start_date column to {table_name!r}")
        calibrator_columns = {
            "calibrator1_id": "INTEGER",
            "calibrator1_dt": "REAL",
            "calibrator1_nchan": "INTEGER",
            "calibrator1_nsb": "INTEGER",
            "calibrator1_name": "TEXT",
            "calibrator1_date": "TEXT",
            "calibrator1_elevation_mean": "REAL",
            "calibrator1_location": "TEXT",
            "calibrator2_id": "INTEGER",
            "calibrator2_dt": "REAL",
            "calibrator2_nchan": "INTEGER",
            "calibrator2_nsb": "INTEGER",
            "calibrator2_name": "TEXT",
            "calibrator2_date": "TEXT",
            "calibrator2_elevation_mean": "REAL",
            "calibrator2_location": "TEXT",
            "calibrator_used_id": "INTEGER",
        }
        for column, column_type in calibrator_columns.items():
            if column not in existing:
                connection.execute(
                    f'ALTER TABLE "{table_name}" ADD COLUMN "{column}" {column_type}'
                )
                print(f"Added {column} column to {table_name!r}")
        legacy_calibrator1_columns = {
            "calibrator_id": "calibrator1_id",
            "calibrator_dt": "calibrator1_dt",
            "calibrator_nchan": "calibrator1_nchan",
            "calibrator_nsb": "calibrator1_nsb",
            "calibrator_name": "calibrator1_name",
            "calibrator_date": "calibrator1_date",
        }
        for old_column, new_column in legacy_calibrator1_columns.items():
            if old_column in existing:
                connection.execute(
                    f'UPDATE "{table_name}" SET "{new_column}" = "{old_column}" '
                    f'WHERE "{new_column}" IS NULL'
                )
        if "quicklook" not in existing:
            connection.execute(
                f'ALTER TABLE "{table_name}" ADD COLUMN quicklook TEXT NOT NULL '
                "DEFAULT 'No'"
            )
            print(f"Added quicklook column to {table_name!r}")
        connection.execute(
            f'UPDATE "{table_name}" SET quicklook = \'No\' WHERE quicklook IS NULL'
        )
        if "overall_status" in existing:
            connection.execute(
                f'UPDATE "{table_name}" SET overall_status = \'Not_started\' '
                "WHERE overall_status IS NULL OR overall_status = 'Archived'"
            )


def _print_lta_object(label: str, obj: Any) -> None:
    """Print all readable public attributes exposed by an AWLofar object."""
    print(f"\n{'=' * 20} {label}: {type(obj).__name__} {'=' * 20}")
    attributes: dict[str, Any] = {}
    for name in sorted(name for name in dir(obj) if not name.startswith("_")):
        try:
            value = getattr(obj, name)
            if not callable(value):
                if isinstance(value, (list, tuple, set)) and len(value) > 5:
                    attributes[name] = (
                        f"<{type(value).__name__} containing {len(value)} items; "
                        f"first 3: {list(value)[:3]!r}>"
                    )
                else:
                    attributes[name] = value
        except Exception as error:  # An LTA relationship may fail to resolve.
            attributes[name] = f"<{type(error).__name__}: {error}>"
    pprint.pprint(attributes, sort_dicts=False, width=120)


def _pipeline_and_observation_for_sasid(project: str, sasid: int) -> tuple[Any, Any]:
    """Resolve an averaging-pipeline SAS ID to its parent observation."""
    from awlofar.main.aweimports import AveragingPipeline

    query = AveragingPipeline.select_all()
    if project != "ALL":
        query = query.project_only(project)
    query &= AveragingPipeline.isValid == 1
    query &= AveragingPipeline.observationId == str(sasid)
    pipelines = list(query)
    if not pipelines:
        raise RuntimeError(
            f"No valid AveragingPipeline with SAS ID {sasid} found in project "
            f"{project!r}"
        )

    pipeline = pipelines[0]
    for source_product in _value(pipeline, "sourceData", default=()) or ():
        source_observations = _value(source_product, "observations", default=()) or ()
        if source_observations:
            return pipeline, source_observations[0]
    raise RuntimeError(
        f"AveragingPipeline {sasid} has no source observation in the LTA metadata"
    )


def inspect_lta(project: str, sasid: int | None = None) -> None:
    """Print the metadata exposed by the LTA client for one observation."""
    try:
        from awlofar.database.Context import context
        from awlofar.main.aweimports import Observation
    except ImportError as error:
        raise RuntimeError(
            "The LOFAR LTA client is not installed. Install lofar_lta and configure "
            "$HOME/.awe/Environment.cfg as described in flocs-lta/README.md."
        ) from error

    context.set_project(project)
    query = Observation.select_all()
    if project != "ALL":
        query = query.project_only(project)
    query &= Observation.isValid == 1
    pipeline = None
    if sasid is not None:
        pipeline, observation = _pipeline_and_observation_for_sasid(project, sasid)
        observations = [observation]
    else:
        observations = list(query)
    if not observations:
        raise RuntimeError(f"No valid observation found in project {project!r}")

    observation = observations[0]
    if pipeline is not None:
        _print_lta_object("REQUESTED AVERAGING PIPELINE", pipeline)
    print(
        f"Inspecting observation {_value(observation, 'observationId')} from "
        f"{len(observations)} matching observation(s) in project {project!r}"
    )
    _print_lta_object("OBSERVATION", observation)

    sap = _first_sap(observation)
    if sap is None:
        print("\nObservation has no sub-array pointings.")
        return
    _print_lta_object("FIRST SUB-ARRAY POINTING", sap)
    pointing = _value(sap, "pointing")
    if pointing is not None:
        _print_lta_object("POINTING COORDINATES", pointing)
    correlator = _value(sap, "correlator")
    if correlator is not None:
        _print_lta_object("CORRELATOR SETTINGS", correlator)

    all_products = _data_products(int(_value(observation, "observationId")), sap)
    products = _preferred_products(all_products, sasid)
    process_ids = sorted(
        {
            _value(product, "processIdentifier")
            for product in all_products
            if _value(product, "processIdentifier") is not None
        }
    )
    print(f"\nAll valid correlated data-product count: {len(all_products)}")
    print(f"Available product process identifiers: {process_ids}")
    print(f"Selected coherent product-generation count: {len(products)}")
    if not products:
        return
    print(
        f"Selected pipeline SAS ID: "
        f"{_sas_id(products, _value(observation, 'observationId'))}"
    )
    product = products[0]
    _print_lta_object("FIRST CORRELATED DATA PRODUCT", product)
    storage = _value(product, "storage")
    if storage is not None:
        _print_lta_object("PRODUCT STORAGE", storage)
    pipeline = _value(product, "pipeline")
    if pipeline is not None:
        _print_lta_object("PRODUCT PIPELINE", pipeline)

    file_object = None
    file_product = None
    for candidate in products:
        file_object = _product_file_object(candidate)
        if file_object is not None:
            file_product = candidate
            break
    if file_object is None:
        print("\nNo valid file object found in the selected product generation.")
        return
    print(
        f"\nFile object obtained from subband "
        f"{_value(file_product, 'subband', default='unknown')}"
    )
    _print_lta_object("FIRST AVAILABLE FILE OBJECT", file_object)


def populate_database(
    dbname: str | Path,
    project: str = "LT10_010",
    table_name: str = DEFAULT_TABLE_NAME,
    minimum_duration_hours: float = 1.0,
    limit: int | None = None,
    print_when_finished: bool = True,
    freq_min: float = DEFAULT_FREQ_MIN_MHZ,
    freq_max: float = DEFAULT_FREQ_MAX_MHZ,
    sasid: int | None = None,
) -> tuple[int, int]:
    """Query the LOFAR LTA and insert or refresh target observation metadata.

    Valid observations at least ``minimum_duration_hours`` long are treated as
    targets. The two temporally nearest observations shorter than one hour in the
    same project are attached as calibrators.
    Existing processing state, CPU accounting, ownership, and priority are not
    overwritten when an observation is refreshed.

    Returns ``(inserted_or_updated, skipped)``.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        raise ValueError(f"Invalid SQLite table name: {table_name!r}")
    if minimum_duration_hours < 0:
        raise ValueError("minimum_duration_hours cannot be negative")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least one")
    if freq_min >= freq_max:
        raise ValueError("freq_min must be lower than freq_max")

    # Do this before contacting the LTA: archive queries can be slow, and a new
    # database should support create-and-populate in a single command.
    if not _table_exists(dbname, table_name):
        print(
            f"Table {table_name!r} does not exist in {str(dbname)!r}; creating it"
        )
        create_database(dbname, table_name)
    else:
        _ensure_schema_columns(dbname, table_name)

    try:
        from awlofar.database.Context import context
        from awlofar.main.aweimports import Observation
    except ImportError as error:
        raise RuntimeError(
            "The LOFAR LTA client is not installed. Install lofar_lta and configure "
            "$HOME/.awe/Environment.cfg as described in flocs-lta/README.md."
        ) from error

    context.set_project(project)
    current_project = context.get_current_project().name
    if project != "ALL" and current_project != project:
        raise ValueError(
            f"No LTA permission for project {project!r}; current project is "
            f"{current_project!r}"
        )

    query = Observation.select_all()
    if project != "ALL":
        query = query.project_only(project)
    query &= Observation.isValid == 1
    observations = list(query)
    if sasid is not None:
        _, requested_observation = _pipeline_and_observation_for_sasid(project, sasid)
        targets = [requested_observation]
    else:
        targets = [
            observation
            for observation in observations
            if float(_value(observation, "duration", default=0))
            >= minimum_duration_hours * 3600
        ]
    targets.sort(key=lambda item: int(_value(item, "observationId")))
    if limit is not None:
        targets = targets[:limit]

    short_observations = sum(
        float(_value(observation, "duration", default=0)) < 3600
        for observation in observations
    )
    print(f"Found {len(observations)} valid observation(s) in project {project!r}")
    print(
        f"Selected {len(targets)} target observation(s) lasting at least "
        f"{minimum_duration_hours:g} hour(s); found {short_observations} potential "
        "calibrator observation(s)"
    )
    if targets:
        print(
            f"Querying correlated data products fully contained in "
            f"{freq_min:g}-{freq_max:g} MHz"
        )
    rows = []
    skipped = 0
    for index, observation in enumerate(targets, start=1):
        observation_id = _value(observation, "observationId")
        print(f"[{index}/{len(targets)}] Reading observation {observation_id}")
        row = _observation_row(
            observation,
            _calibrators_for(observation, observations),
            freq_min,
            freq_max,
            sasid,
        )
        if row is None:
            skipped += 1
        else:
            rows.append(row)

    if not rows:
        print(f"No rows to write ({skipped} skipped)")
        if print_when_finished:
            print_database(dbname, table_name)
        return 0, skipped

    insert_columns = tuple(rows[0])
    placeholders = ", ".join("?" for _ in insert_columns)
    quoted_columns = ", ".join(f'"{column}"' for column in insert_columns)
    # These values come from the archive and are safe to refresh. Operational
    # columns such as priority and username remain under the monitor's control.
    refresh_columns = tuple(
        column
        for column in insert_columns
        if column
        not in {
            "target_id",
            "priority",
            "username",
            "clustername",
            "processing_start_date",
            "processing_end_date",
            "quicklook",
            "calibrator_used_id",
            "overall_status",
        }
    )
    updates = ", ".join(
        f'"{column}" = excluded."{column}"' for column in refresh_columns
    )
    sql = (
        f'INSERT INTO "{table_name}" ({quoted_columns}) VALUES ({placeholders}) '
        f'ON CONFLICT("target_id") DO UPDATE SET {updates}'
    )

    with sqlite3.connect(dbname) as connection:
        connection.executemany(
            sql, ([row[column] for column in insert_columns] for row in rows)
        )

    print(f"Inserted or refreshed {len(rows)} row(s); skipped {skipped}")
    if print_when_finished:
        print_database(dbname, table_name)
    return len(rows), skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dbname",
        nargs="?",
        help="SQLite database from which processing will be done.",
    )
    parser.add_argument(
        "--table-name",
        default=DEFAULT_TABLE_NAME,
        help=f"Database table to create (default: {DEFAULT_TABLE_NAME}).",
    )
    parser.add_argument(
        "--populate",
        action="store_true",
        help="Populate an existing table from the LOFAR LTA instead of creating it.",
    )
    parser.add_argument(
        "--inspect-lta",
        action="store_true",
        help="Print all LTA metadata for one observation without changing the database.",
    )
    parser.add_argument(
        "--project", default="LT10_010", help="LTA project to query."
    )
    parser.add_argument(
        "--minimum-duration-hours",
        type=float,
        default=1.0,
        help="Minimum target duration in hours (default: 1).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Only populate the first N matching observations (useful for testing).",
    )
    parser.add_argument(
        "--freq-min",
        type=float,
        default=DEFAULT_FREQ_MIN_MHZ,
        help=f"Minimum product frequency in MHz (default: {DEFAULT_FREQ_MIN_MHZ:g}).",
    )
    parser.add_argument(
        "--freq-max",
        type=float,
        default=DEFAULT_FREQ_MAX_MHZ,
        help=f"Maximum product frequency in MHz (default: {DEFAULT_FREQ_MAX_MHZ:g}).",
    )
    parser.add_argument(
        "--no-print",
        action="store_true",
        help="Do not print the populated database when finished.",
    )
    parser.add_argument(
        "--sasid",
        type=int,
        help=(
            "Populate only this averaging-pipeline SAS ID, or select it with "
            "--inspect-lta."
        ),
    )
    args = parser.parse_args()
    if args.inspect_lta:
        inspect_lta(args.project, args.sasid)
    elif args.populate or args.sasid is not None:
        if args.dbname is None:
            parser.error("dbname is required when populating the database")
        populate_database(
            args.dbname,
            project=args.project,
            table_name=args.table_name,
            minimum_duration_hours=args.minimum_duration_hours,
            limit=args.limit,
            print_when_finished=not args.no_print,
            freq_min=args.freq_min,
            freq_max=args.freq_max,
            sasid=args.sasid,
        )
    else:
        if args.dbname is None:
            parser.error("dbname is required when creating a database")
        create_database(args.dbname, args.table_name, print_when_finished=True)


if __name__ == "__main__":
    main()
