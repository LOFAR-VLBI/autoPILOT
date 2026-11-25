"""Restore ILoTSS archives from SURF."""

import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

from .archive_surf_tape import (
    DEFAULT_DD_DESTINATION, DEFAULT_DDF_DESTINATION, _adler32, _rclone_command, _surf_archive_metadata,
)


def _restore_member(member, destination):
    """Keep symlinks as links while filtering paths written during extraction."""
    if member.issym():
        return tarfile.tar_filter(member, destination).replace(
            uid=None, gid=None, uname=None, gname=None
        )
    return tarfile.data_filter(member, destination)


def _archive_name(remote_path):
    """Get the tar filename from a source path."""
    remote_path = os.fspath(remote_path)
    _, _, object_path = remote_path.partition(":")
    name = Path(object_path or remote_path)
    if name.suffix != ".tar" or name.stem in (".", ".."):
        raise ValueError("Supply the path of an archived .tar file")
    if not hasattr(tarfile, "data_filter"):
        raise RuntimeError("Archive retrieval requires a Python version with tarfile extraction filters")
    return name


def _download_archive(remote_path, archive, command, rclone_config):
    """Download and verify an online archive."""
    subprocess.run(command + ["copyto", remote_path, str(archive)], check=True)
    stored = _surf_archive_metadata(remote_path, rclone_config)
    if stored is not None and stored != (str(archive.stat().st_size), _adler32(archive)):
        raise RuntimeError(f"dCache size or checksum mismatch for {archive.name}")


def retrieve_surf_archive(remote_path, output_directory, *, rclone_config=None):
    """Download an online tar and return the path to the restored run."""
    remote_path = os.fspath(remote_path)
    name = _archive_name(remote_path)

    output = Path(output_directory).resolve()
    restored = output / name.stem
    if os.path.lexists(restored):
        raise FileExistsError(f"Restore directory already exists: {restored}")
    command, rclone_config = _rclone_command(remote_path, rclone_config)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".retrieve-", dir=output) as temporary:
        archive = Path(temporary) / name.name
        extracted = Path(temporary) / "contents"
        _download_archive(remote_path, archive, command, rclone_config)

        extracted.mkdir()
        with tarfile.open(archive, "r:") as tar:
            tar.extractall(extracted, filter=_restore_member)
        run = extracted / restored.name
        if run.is_symlink() or not run.is_dir() or list(extracted.iterdir()) != [run]:
            raise ValueError(f"Expected one archived run directory named {restored.name}")
        if os.path.lexists(restored):
            raise FileExistsError(f"Restore directory already exists: {restored}")
        run.rename(restored)
    return restored


def _archive_path(archive_root, filename):
    """Join a filename to an rclone or local archive directory."""
    root = os.fspath(archive_root).rstrip("/") or "/"
    separator = "" if root.endswith((":", "/")) else "/"
    return f"{root}{separator}{filename}"


def retrieve_pilot_dd_calibration(
    run_name, output_directory, *, archive_root=DEFAULT_DD_DESTINATION, rclone_config=None
):
    """Restore the DD-calibration archive for a timestamped run name."""
    name = os.fspath(run_name).removesuffix(".tar")
    if re.fullmatch(r"VLBI_dd-calibration_L\d+_[A-Za-z0-9_-]+", name) is None:
        raise ValueError("Supply the timestamped VLBI_dd-calibration run name")
    remote_path = _archive_path(archive_root, f"{name}.tar")
    return retrieve_surf_archive(remote_path, output_directory, rclone_config=rclone_config)


def _ddf_observation_paths(observation, archive_root, products):
    """Get the archive paths for a DDF observation and its product groups."""
    match = re.fullmatch(r"[Ll]?(\d+)", str(observation))
    if match is None:
        raise ValueError("Supply a DDF observation ID such as L2019041 or a list of tar paths")
    observation_id = f"L{match[1]}"
    split_groups = ("images", "misc", "uv_misc", f"uv_{observation_id}")
    if isinstance(products, str):
        products = (products,)
    groups = split_groups if products is None else tuple(products)
    allowed = {*split_groups, "uv", "stokes_small", "stokes_large", "dynspec"}
    if not groups or any(group not in allowed for group in groups):
        raise ValueError(f"Unknown or empty DDF product selection: {groups}")

    return [_archive_path(archive_root, f"{observation_id}/{group}.tar") for group in groups]


def retrieve_ddf_pipeline(
    remote_paths, run_directory, *, products=None, archive_root=DEFAULT_DDF_DESTINATION, rclone_config=None
):
    """Restore an observation's split UV bundles, selected products, or explicit tar paths."""
    if isinstance(remote_paths, (str, int)):
        remote_paths = _ddf_observation_paths(remote_paths, archive_root, products)
    elif products is not None:
        raise ValueError("Select products by observation ID or supply explicit tar paths")
    paths = [os.fspath(path) for path in remote_paths]
    if not paths or len({path.rpartition("/")[0] for path in paths}) != 1:
        raise ValueError("Supply DDF tar paths from the same archive directory")
    names = [_archive_name(path).name for path in paths]
    split_uv = next(((path, name) for path, name in zip(paths, names)
                     if re.fullmatch(r"uv_L\d+\.tar", name)), None)
    if split_uv is not None and "uv_misc.tar" not in names:
        path, name = split_uv
        paths.insert(0, path.removesuffix(name) + "uv_misc.tar")
        names.insert(0, "uv_misc.tar")

    output = Path(run_directory)
    restored = output.parent.resolve() / output.name
    if os.path.lexists(restored):
        raise FileExistsError(f"Restore directory already exists: {restored}")
    command, rclone_config = _rclone_command(paths[0], rclone_config)
    restored.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".retrieve-ddf-", dir=restored.parent) as temporary:
        staging = Path(temporary)
        extracted = staging / "contents"
        extracted.mkdir()
        for remote_path, name in zip(paths, names):
            archive = staging / name
            _download_archive(remote_path, archive, command, rclone_config)
            with tarfile.open(archive, "r:") as tar:
                tar.extractall(extracted, filter="data")
            archive.unlink()
        for path in extracted.rglob("*"):
            if path.is_symlink() and (not path.exists() or not path.resolve().is_relative_to(extracted)):
                raise ValueError(f"Unresolved or external DDF symlink: {path.relative_to(extracted)}")
        if os.path.lexists(restored):
            raise FileExistsError(f"Restore directory already exists: {restored}")
        extracted.rename(restored)
    return restored
