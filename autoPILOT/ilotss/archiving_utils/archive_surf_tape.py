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
import zlib


DEFAULT_DESTINATION = "maca_ilotss:LINC_calibrators"
SURF_WEBDAV_HOST = "webdav.grid.surfsara.nl"


def _adler32(path):
    """Return the file's Adler-32 checksum as eight hex digits."""
    checksum = 1
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            checksum = zlib.adler32(block, checksum)
    return f"{checksum:08x}"


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


def _validate_linc_calibrator_products(results):
    """Raise if expected LINC calibrator products are missing or empty."""
    expected = [
        "cal_solutions.h5",
        "*_LINC_calibrator_summary.json",
        "inspection/**/*",
        "logs/**/*",
        "skymodels/**/*",
    ]
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
    if raw_saved:
        expected.extend(f"solutions_raw/{name}.h5" for name in ("bandpass", "faraday", "ion", "polalign"))
    missing = [
        pattern for pattern in expected
        if not any(path.is_file() and path.stat().st_size > 0 for path in results.glob(pattern))
    ]
    if missing:
        raise ValueError(f"Missing or empty LINC calibrator products in {results}: {', '.join(missing)}")


def archive_linc_calibrator(
    run_directory, archive_directory, *, destination=DEFAULT_DESTINATION, rclone_config=None
):
    """Package and upload a published LINC calibrator run, then return a verified receipt."""
    source = Path(run_directory).resolve()
    staging = Path(archive_directory).resolve()
    match = re.fullmatch(r"LINC_calibrator_L(\d+)_[A-Za-z0-9_-]+", source.name)
    if not source.is_dir() or match is None:
        raise ValueError("Supply a published LINC_calibrator_L<obsid>_<run> directory")
    if staging == source or source in staging.parents:
        raise ValueError("archive_directory must be outside run_directory")
    _validate_linc_calibrator_products(source / "results_LINC_calibrator")
    if destination is None or not destination.strip():
        raise ValueError("An archive destination is required")
    destination = destination.strip().rstrip("/") or "/"
    if rclone_config is None and destination.startswith("maca_ilotss:"):
        config_dir = os.environ.get("RCLONE_CONFIG_DIR")
        if not config_dir:
            raise ValueError("Set RCLONE_CONFIG_DIR or supply rclone_config for the maca_ilotss macaroon")
        rclone_config = Path(config_dir) / "maca_ilotss.conf"
    if rclone_config is not None:
        rclone_config = Path(rclone_config).resolve()
        if not rclone_config.is_file():
            raise FileNotFoundError(f"rclone config not found: {rclone_config}")

    staging.mkdir(parents=True, exist_ok=True)
    archive = staging / f"{source.name}.tar"
    with tempfile.TemporaryDirectory(prefix=".calibrator-", dir=staging) as temporary:
        pending = Path(temporary) / archive.name
        with tarfile.open(pending, "w", dereference=False) as tar:
            tar.add(source, arcname=source.name)
        size_bytes = pending.stat().st_size
        checksum = _adler32(pending)
        try:
            os.link(pending, archive)
        except FileExistsError:
            if (archive.is_symlink() or not archive.is_file()
                    or archive.stat().st_size != size_bytes or _adler32(archive) != checksum):
                raise FileExistsError(f"Conflicting local archive: {archive}") from None

    separator = "" if destination.endswith((":", "/")) else "/"
    remote_path = destination + separator + archive.name
    command = ["rclone"]
    if rclone_config is not None:
        command += ["--config", str(rclone_config)]
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

    return {"status": "verified", "observation_id": "L" + match.group(1),
            "archive_path": str(archive), "archive_adler32": checksum,
            "size_bytes": size_bytes, "remote_path": remote_path,
            "verification": verification}
