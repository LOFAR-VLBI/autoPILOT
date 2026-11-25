"""Archive ILoTSS products to SURF tape."""

from configparser import ConfigParser, Error as ConfigError
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
from urllib.parse import quote, urlsplit
from zipfile import ZipFile
import zlib


DEFAULT_DESTINATION = "maca_ilotss:LINC_calibrators"
DEFAULT_TARGET_DESTINATION = "maca_ilotss:LINC_target_NL"
DEFAULT_DELAY_DESTINATION = "maca_ilotss:VLBI_delay-calibration"
DEFAULT_DD_DESTINATION = "maca_ilotss:VLBI_dd-calibration"
DEFAULT_DDF_DESTINATION = "maca_ilotss:DDF-pipeline"
SURF_WEBDAV_HOST = "webdav.grid.surfsara.nl"


def _adler32(path):
    """Return the file's Adler-32 checksum as eight hex digits."""
    checksum = 1
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            checksum = zlib.adler32(block, checksum)
    return f"{checksum:08x}"


def _rclone_command(remote_path, rclone_config):
    """Resolve the archive config and return the rclone command prefix."""
    if rclone_config is None and remote_path.startswith("maca_ilotss:"):
        config_dir = os.environ.get("RCLONE_CONFIG_DIR")
        if not config_dir:
            raise ValueError("Set RCLONE_CONFIG_DIR or supply rclone_config for the maca_ilotss macaroon")
        rclone_config = Path(config_dir) / "maca_ilotss.conf"
    command = ["rclone"]
    if rclone_config is not None:
        rclone_config = Path(rclone_config).resolve()
        if not rclone_config.is_file():
            raise FileNotFoundError(f"rclone config not found: {rclone_config}")
        command += ["--config", str(rclone_config)]
    return command, rclone_config


def _surf_archive_metadata(remote_path, rclone_config):
    """Return the dCache size and Adler-32, or None for other remotes."""
    remote_name, separator, object_path = remote_path.partition(":")
    if rclone_config is None or not separator:
        return None
    # Defer to rclone when the effective endpoint or credentials may be overridden.
    prefix = f"RCLONE_CONFIG_{remote_name.upper().replace('-', '_')}_"
    if any(key.startswith((prefix, "RCLONE_WEBDAV_")) for key in os.environ):
        return None
    config = ConfigParser(interpolation=None, default_section="")
    try:
        config.read(rclone_config)
    except (ConfigError, UnicodeError):
        return None  # rclone also accepts encrypted configurations.
    if remote_name not in config:
        return None

    settings = config[remote_name]
    base = urlsplit(settings.get("url", ""))
    if (settings.get("type") != "webdav" or base.scheme != "https"
            or base.hostname != SURF_WEBDAV_HOST or base.query or base.fragment
            or base.username is not None):
        return None

    path = base.path.rstrip("/") + "/" + quote(object_path.lstrip("/"))
    headers = {"Want-Digest": "adler32"}
    if token := settings.get("bearer_token"):
        headers["Authorization"] = "Bearer " + token
    connection = HTTPSConnection(base.hostname, base.port, timeout=30)
    try:
        connection.request("HEAD", path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"dCache checksum request failed (HTTP {response.status})")
        size = response.getheader("Content-Length")
        checksum = re.fullmatch(r"adler32=([0-9a-f]{8})", response.getheader("Digest", ""), re.I)
        if size is None or checksum is None:
            raise RuntimeError("dCache did not return the archive size and Adler-32 checksum")
        return size, checksum.group(1).lower()
    finally:
        connection.close()


def _require_linc_products(results, product, extra=()):
    """Require the common LINC products and any run-specific files."""
    expected = (
        "cal_solutions.h5", f"*_LINC_{product}_summary.json",
        "inspection/**/*", "logs/**/*", "skymodels/**/*", *extra,
    )
    missing = [
        pattern for pattern in expected
        if not any(path.is_file() and path.stat().st_size > 0 for path in results.glob(pattern))
    ]
    if missing:
        raise ValueError(f"Missing or empty LINC {product} products in {results}: {', '.join(missing)}")


def _validate_linc_calibrator_products(results):
    """Raise if expected LINC calibrator products are missing or empty."""
    raw_saved = (results / "solutions_raw").is_dir()
    parameters = results / "input_parms.json"
    if parameters.is_file():
        try:
            settings = json.loads(parameters.read_text())
        except ValueError as error:
            raise ValueError(f"Invalid LINC input parameters in {parameters}") from error
        if not isinstance(settings, dict):
            raise ValueError(f"Expected a JSON object in {parameters}")
        raw_saved = settings.get("save_raw_solutions", raw_saved)
    raw_products = []
    if raw_saved:
        raw_products.extend(f"solutions_raw/{name}.h5" for name in ("bandpass", "faraday", "ion", "polalign"))
    _require_linc_products(results, "calibrator", raw_products)


def _validate_linc_target_products(results):
    """Check target products and return the averaged Measurement Sets."""
    _require_linc_products(results, "target")
    ms_directory = results / "results"
    averaged_mses = sorted(ms_directory.glob("*_pre-cal.ms"))
    if not averaged_mses or any(not (ms / "table.dat").is_file() for ms in averaged_mses):
        raise ValueError(f"Missing or incomplete averaged Measurement Sets in {ms_directory}")
    return averaged_mses


def _published_run(run_directory, workflow):
    """Resolve a published run, its results directory and observation ID."""
    source = Path(run_directory).resolve()
    results_name = f"results_{workflow}"
    if source.name == results_name:
        source = source.parent
    match = re.fullmatch(rf"{workflow}_L(\d+)_[A-Za-z0-9_-]+", source.name)
    if not source.is_dir() or match is None:
        raise ValueError(f"Supply a published {workflow}_L<obsid>_<run> directory")
    return source, source / results_name, f"L{match.group(1)}"


def _archive_paths(
    source, paths, archive_directory, *, observation_id, destination, rclone_config,
    cleanup_tar, group=None, member_filter=None
):
    """Archive paths under their published run directory name."""
    members = ((path, (Path(source.name) / path.relative_to(source)).as_posix()) for path in paths)
    return _archive_members(
        source, members, archive_directory, f"{source.name}.tar", observation_id=observation_id,
        destination=destination, rclone_config=rclone_config, cleanup_tar=cleanup_tar,
        group=group, member_filter=member_filter,
    )


def _archive_members(
    source, members, archive_directory, archive_name, *, observation_id, destination,
    rclone_config, cleanup_tar, group=None, member_filter=None
):
    """Package named tar members and verify the upload."""
    if destination is None or not destination.strip():
        raise ValueError("An archive destination is required")
    destination = destination.strip().rstrip("/") or "/"
    separator = "" if destination.endswith((":", "/")) else "/"
    staging = Path(archive_directory)
    if group is not None:
        staging /= group
        destination += separator + group
        separator = "/"
    staging = staging.resolve()
    if staging == source or source in staging.parents:
        raise ValueError("archive_directory must be outside run_directory")
    command, rclone_config = _rclone_command(destination, rclone_config)

    staging.mkdir(parents=True, exist_ok=True)
    archive = staging / archive_name
    with tempfile.TemporaryDirectory(prefix=".archive-", dir=staging) as temporary:
        pending = Path(temporary) / archive.name
        with tarfile.open(pending, "w", dereference=False) as tar:
            for path, name in members:
                tar.add(path, arcname=name, filter=member_filter)
        size_bytes = pending.stat().st_size
        checksum = _adler32(pending)
        try:
            os.link(pending, archive)
        except FileExistsError:
            if (archive.is_symlink() or not archive.is_file()
                    or archive.stat().st_size != size_bytes or _adler32(archive) != checksum):
                raise FileExistsError(f"Conflicting local archive: {archive}") from None

    remote_path = destination + separator + archive.name
    subprocess.run(command + ["copyto", str(archive), remote_path,
                              "--immutable", "--checksum"], check=True)
    stored = _surf_archive_metadata(remote_path, rclone_config)
    if stored is None:
        subprocess.run(command + ["check", str(staging), destination,
                                  "--download", "--one-way", "--include", "/" + archive.name],
                       check=True)
        verification = "rclone_readback"
    elif stored != (str(size_bytes), checksum):
        raise RuntimeError(f"dCache size or checksum mismatch for {archive.name}")
    else:
        verification = "dcache_adler32"

    if cleanup_tar:
        archive.unlink(missing_ok=True)
    return {"status": "verified", "observation_id": observation_id,
            "archive_path": None if cleanup_tar else str(archive), "archive_adler32": checksum,
            "size_bytes": size_bytes, "remote_path": remote_path,
            "verification": verification}


def archive_linc_calibrator(
    run_directory, archive_directory, *, destination=DEFAULT_DESTINATION, rclone_config=None,
    cleanup_tar=False
):
    """Package and upload a published LINC calibrator run, then return a verified receipt."""
    source, results, observation_id = _published_run(run_directory, "LINC_calibrator")
    _validate_linc_calibrator_products(results)
    return _archive_paths(
        source, (source,), archive_directory, observation_id=observation_id, destination=destination,
        rclone_config=rclone_config, cleanup_tar=cleanup_tar,
    )


def _exclude_measurement_sets(member):
    """Skip Measurement Sets when building a tar."""
    return None if member.name.lower().endswith((".ms", ".ms.archive", ".dp3concat", ".dp3-concat")) else member


def archive_linc_target(
    run_directory, archive_directory, *, destination=DEFAULT_TARGET_DESTINATION,
    rclone_config=None, cleanup_tar=False
):
    """Archive LINC target diagnostics and averaged visibilities separately."""
    source, results, observation_id = _published_run(run_directory, "LINC_target")
    averaged_mses = _validate_linc_target_products(results)

    receipts = {}
    for group, paths, member_filter in (
        ("solutions-and-diagnostics", (results,), _exclude_measurement_sets),
        ("averaged-ms", averaged_mses, None),
    ):
        receipts[group] = _archive_paths(
            source, paths, archive_directory, observation_id=observation_id,
            destination=destination, rclone_config=rclone_config,
            cleanup_tar=cleanup_tar, group=group, member_filter=member_filter,
        )
    return receipts


def archive_pilot_delay_calibration(
    run_directory, archive_directory, *, destination=DEFAULT_DELAY_DESTINATION, rclone_config=None,
    cleanup_tar=False
):
    """Archive the published PILOT delay-calibration results without Measurement Sets."""
    source, results, observation_id = _published_run(run_directory, "VLBI_delay-calibration")
    if not any(path.is_file() and path.stat().st_size > 0 for path in results.glob("*.h5")):
        raise ValueError(f"Missing or empty delay-calibration solutions in {results}")
    return _archive_paths(
        source, (results,), archive_directory, observation_id=observation_id, destination=destination,
        rclone_config=rclone_config, cleanup_tar=cleanup_tar, member_filter=_exclude_measurement_sets,
    )


def archive_pilot_dd_calibration(
    run_directory, archive_directory, *, destination=DEFAULT_DD_DESTINATION, rclone_config=None,
    cleanup_tar=False
):
    """Archive the merged solution and diagnostics from a finished widefield DD-cal run."""
    source, results, observation_id = _published_run(run_directory, "VLBI_dd-calibration")
    merged = results / "merged.h5"
    if not merged.is_file() or merged.stat().st_size == 0:
        raise ValueError(f"Missing or empty DD calibration solution: {merged}")

    logs = (source / f"full_log_dd-calibration_{observation_id[1:]}.log",
            source / "logs_VLBI_dd-calibration.tar")
    root = Path(source.name)
    result_root = root / results.name
    merged_name = (result_root / merged.name).as_posix()
    members = [(merged.resolve(strict=True), merged_name)]
    members.extend((path, (result_root / path.name).as_posix())
                   for path in sorted(results.iterdir()) if path != merged)
    members.extend((log.resolve(strict=True), (root / log.name).as_posix()) for log in logs if log.is_file())

    def select_products(member):
        if member.name.lower().endswith(".h5") and member.name != merged_name:
            return None
        return _exclude_measurement_sets(member)

    return _archive_members(
        source, members, archive_directory, f"{source.name}.tar", observation_id=observation_id,
        destination=destination, rclone_config=rclone_config, cleanup_tar=cleanup_tar,
        member_filter=select_products,
    )


def _ddf_image_members(source, summary):
    """Select the final LoTSS images and name the low-resolution PSF."""
    high = "image_full_ampphase_di_m.NS"
    members = []
    for stem in (high, "image_full_low_m"):
        for suffix in ("app.restored.fits", "int.restored.fits", "int.model.fits", "int.residual.fits",
                       "psf.fits", "DicoModel", "tessel.reg"):
            name = f"{stem}.{suffix}"
            path = source / name
            if name == "image_full_low_m.psf.fits" and (source / "image_full_low.psf.fits").is_file():
                path = source / "image_full_low.psf.fits"
            members.append((path, name))
        number = 1
        while (source / f"{stem}.mask{number:02d}.fits").is_file():
            number += 1
        if number > 1:
            name = f"{stem}.mask{number - 1:02d}.fits"
            members.append((source / name, name))

    shifted = []
    if summary.get("method", "none") != "none":
        members.append((source / "astromap.fits", "astromap.fits"))
        shifted.append(high)
    if summary.get("spectral_restored") == "true":
        shifted.extend(f"{high}_Band{band}" for band in range(3))
    for stem in shifted:
        for flux in ("app", "int"):
            name = f"{stem}_shift.{flux}.facetRestored.fits"
            members.append((source / name, name))
    return members


def _ddf_groups(source, split_uv):
    """Select the LoTSS tar groups for one completed ILoTSS observation."""
    mses = sorted(source.glob("*.ms.archive"))
    observations = {ms.name.partition("_")[0] for ms in mses}
    if len(observations) != 1 or any(not (ms / "table.dat").is_file() for ms in mses):
        raise ValueError(f"Expected compressed MSes for one observation in {source}")
    observation_id = observations.pop()
    if re.fullmatch(r"L\d+", observation_id) is None:
        raise ValueError(f"Invalid DDF observation ID: {observation_id}")

    summary = {}
    for line in (source / "summary.txt").read_text().splitlines():
        key, separator, value = line.partition(":")
        if separator:
            summary[key.strip()] = value.strip().lower()
    sky_model = [source / name for name in (
        "image_dirin_SSD_m.npy.ClusterCat.npy", "image_full_ampphase_di_m.NS.DicoModel",
        "image_full_ampphase_di_m.NS.tessel.reg",
    )]
    shared = sorted(set(source.glob("DDS*smoothed*.npz")) | set(source.glob("DDS*full_slow*.npz")))
    solutions = source / "SOLSDIR"
    per_ms = sorted(path for path in solutions.glob(f"{observation_id}_*") if path.is_dir())
    if not per_ms or not shared:
        raise ValueError(f"Missing DDF solution products in {source}")

    groups = {"images": _ddf_image_members(source, summary)}
    uv_groups = {"uv_misc": sky_model + shared, f"uv_{observation_id}": mses + per_ms} if split_uv else {
        "uv": mses + sky_model + shared + [solutions],
    }
    misc = [source / name for name in ("summary.txt", "logs", "mslist.txt", "big-mslist.txt")]
    misc_patterns = ("mslist-*txt", "*-fit_state.pickle", "*.png", "*crossmatch-results*",
                     "*crossmatch-*.fits", "*frequencies.txt")
    misc.extend(sorted({path for pattern in misc_patterns for path in source.glob(pattern)}))
    product_groups = {**uv_groups, "misc": misc}
    if summary.get("polcubes") == "true":
        product_groups["stokes_large"] = sorted(source.glob("image_full_low_QU.cube.*.fz"))
        patterns = ("image_full_*_stokesV*.dirty.*", "image_full_*_stokesV*.SmoothNorm.fits",
                    "image_full_vlow_QU.cube.*.fz")
        product_groups["stokes_small"] = sorted({p for pattern in patterns for p in source.glob(pattern)})
    if summary.get("do_dynspec") == "true":
        product_groups["dynspec"] = sorted(source.glob("DynSpecs*.tgz"))
    groups.update({name: [(path, path.relative_to(source).as_posix()) for path in paths]
                   for name, paths in product_groups.items()})

    missing = sorted({name for members in groups.values() for path, name in members if not path.exists()})
    if missing:
        raise ValueError(f"Missing DDF products in {source}: {', '.join(missing)}")
    return observation_id, groups, shared


def _npz_signature(path):
    """Read the stored size and checksum of each NumPy array."""
    with ZipFile(path) as archive:
        return tuple((entry.filename, entry.file_size, entry.CRC) for entry in archive.infolist())


def _ddf_solution_links(source, shared):
    """Find portable targets for solution links and verified duplicate files."""
    candidates = {}
    for path in shared:
        parts = path.stem.rsplit("_", 2)
        if len(parts) == 3:
            stage, _, version = parts
            candidates.setdefault(f"killMS.{stage}_{version}.sols.npz", {})[path.name] = path

    links = {}
    signatures = {}
    for path in sorted((source / "SOLSDIR").glob("*/*.npz")):
        targets = candidates.get(path.name, {})
        if path.is_symlink():
            target = targets.get(path.readlink().name)
            if target is None:
                raise ValueError(f"Solution link has no archived target: {path}")
        elif targets:
            signature = _npz_signature(path)
            target = None
            for candidate in targets.values():
                if candidate not in signatures:
                    signatures[candidate] = _npz_signature(candidate)
                if signatures[candidate] == signature:
                    target = candidate
                    break
            if target is None:
                continue
        else:
            continue
        links[path.relative_to(source).as_posix()] = os.path.relpath(target, path.parent)
    return links


def _ddf_archive_metadata(groups, scratch, timestamp):
    """Generate FITS headers and product lists without editing the run."""
    from astropy.io import fits

    headers = scratch / "fits_headers"
    headers.mkdir()
    for members in groups.values():
        for path, name in members:
            if Path(name).suffix in (".fits", ".fz"):
                with fits.open(path) as hdus:
                    for index, hdu in enumerate(hdus):
                        hdu.header.totextfile(headers / f"{Path(name).name}.{index}.hdr")
    groups["misc"].append((headers, headers.name))

    for group in ("images", "misc", "stokes_large", "stokes_small"):
        if group in groups:
            readme = scratch / f"{group}.txt"
            names = sorted(name for _, name in groups[group])
            readme.write_text(f"Contents of {group}.tar\n\n" + "\n".join(names) + "\n")
            groups[group].append((readme, "README.txt"))
    for path in scratch.rglob("*"):
        os.utime(path, (timestamp, timestamp))


def archive_ddf_pipeline(
    run_directory, archive_directory, *, destination=DEFAULT_DDF_DESTINATION, split_uv=True,
    rclone_config=None, cleanup_tar=False
):
    """Archive DDF products under the observation ID using the LoTSS tar layout."""
    source = Path(run_directory).resolve()
    observation_id, groups, shared = _ddf_groups(source, split_uv)
    links = _ddf_solution_links(source, shared)

    def portable_solution(member):
        if member.name in links:
            member.type = tarfile.SYMTYPE
            member.linkname = links[member.name]
            member.size = 0
        return member

    with tempfile.TemporaryDirectory(prefix="ddf-archive-") as temporary:
        _ddf_archive_metadata(groups, Path(temporary), (source / "summary.txt").stat().st_mtime)
        return {
            group: _archive_members(
                source, members, archive_directory, f"{group}.tar", observation_id=observation_id,
                destination=destination, rclone_config=rclone_config, cleanup_tar=cleanup_tar,
                group=observation_id, member_filter=portable_solution,
            )
            for group, members in groups.items()
        }
