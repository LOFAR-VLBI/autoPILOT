"""Calibrator retention, integrity and failure behaviour; no live archive access."""

import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zlib

from autoPILOT.ilotss.archiving_utils import archive_surf_tape
from autoPILOT.ilotss.archiving_utils.archive_surf_tape import archive_linc_calibrator


class CalibratorArchivingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / "LINC_calibrator_L858674_2026_11_07-18_29_32"
        self.staging = self.root / "archives"
        self.products = {
            "results_LINC_calibrator/cal_solutions.h5": b"final reusable solutions",
            "results_LINC_calibrator/solutions_raw/bandpass.h5": b"raw bandpass",
            "results_LINC_calibrator/solutions_raw/faraday.h5": b"raw faraday",
            "results_LINC_calibrator/solutions_raw/ion.h5": b"raw clock and TEC",
            "results_LINC_calibrator/solutions_raw/polalign.h5": b"raw polalign",
            "results_LINC_calibrator/3C196_LINC_calibrator_summary.json": b'{"metrics": {}}',
            "results_LINC_calibrator/inspection/bandpass.png": b"QA plot",
            "results_LINC_calibrator/logs/3C196_summary.log": b"LINC logs",
            "results_LINC_calibrator/input_parms.json": b'{"save_raw_solutions": true}',
            "results_LINC_calibrator/skymodels/calibrator.skymodel": b"calibrator model",
            "results_LINC_calibrator/skymodels/calibrator.sourcedb/table.dat": b"casacore sky model",
            "logs_LINC_calibrator.tar": b"processing logs and versions",
        }
        for name, data in self.products.items():
            path = self.run / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def assert_source_unchanged(self):
        for name, data in self.products.items():
            self.assertEqual((self.run / name).read_bytes(), data)

    def archive_with_mock_transfer(self):
        with patch.object(archive_surf_tape.subprocess, "run"):
            return archive_linc_calibrator(self.run, self.staging, destination="archive:calibrators")

    def test_named_tar_keeps_all_exported_products(self):
        result = self.archive_with_mock_transfer()
        self.assertEqual(result["status"], "verified")
        archive = Path(result["archive_path"])
        self.assertEqual(archive.name, f"{self.run.name}.tar")
        self.assertEqual(result["size_bytes"], archive.stat().st_size)
        self.assertEqual(result["archive_adler32"], f"{zlib.adler32(archive.read_bytes()):08x}")
        with tarfile.open(archive) as tar:
            self.assertEqual({entry.name for entry in tar if entry.isfile()},
                             {f"{self.run.name}/{name}" for name in self.products})
            for name, payload in self.products.items():
                self.assertEqual(tar.extractfile(f"{self.run.name}/{name}").read(), payload)
        self.assert_source_unchanged()

    def test_repeat_reuses_identical_archive(self):
        first = self.archive_with_mock_transfer()
        archive = Path(first["archive_path"])
        before = archive.stat().st_mtime_ns
        second = self.archive_with_mock_transfer()
        self.assertEqual(first, second)
        self.assertEqual(archive.stat().st_mtime_ns, before)

    def test_changed_products_fail_and_keep_same_name_archive(self):
        first = self.archive_with_mock_transfer()
        archive = Path(first["archive_path"])
        original = archive.read_bytes()
        (self.run / "logs_LINC_calibrator.tar").write_bytes(b"changed processing logs")
        with self.assertRaisesRegex(FileExistsError, "Conflicting local archive"):
            self.archive_with_mock_transfer()
        self.assertEqual(list(self.staging.resolve().iterdir()), [archive])
        self.assertEqual(archive.read_bytes(), original)

    def test_existing_corrupt_local_archive_is_not_overwritten(self):
        first = self.archive_with_mock_transfer()
        archive = Path(first["archive_path"])
        archive.write_bytes(b"corrupted archive")
        with self.assertRaises(FileExistsError):
            self.archive_with_mock_transfer()
        self.assertEqual(archive.read_bytes(), b"corrupted archive")
        self.assert_source_unchanged()

    def test_incomplete_linc_products_are_rejected(self):
        results = self.run / "results_LINC_calibrator"
        for product, message in (
            ("cal_solutions.h5", r"cal_solutions\.h5"),
            ("3C196_LINC_calibrator_summary.json", r"_LINC_calibrator_summary\.json"),
            ("inspection/bandpass.png", "inspection/"),
            ("logs/3C196_summary.log", "logs/"),
            ("solutions_raw/ion.h5", r"solutions_raw/ion\.h5"),
        ):
            with self.subTest(product=product):
                path = results / product
                original = path.read_bytes()
                path.write_bytes(b"")
                try:
                    with self.assertRaisesRegex(ValueError, message):
                        self.archive_with_mock_transfer()
                    self.assertFalse(self.staging.exists())
                finally:
                    path.write_bytes(original)

    def test_raw_solutions_and_runner_log_tar_are_optional_when_raw_saving_is_disabled(self):
        results = self.run / "results_LINC_calibrator"
        shutil.rmtree(results / "solutions_raw")
        (results / "input_parms.json").write_text('{"save_raw_solutions": false}')
        (self.run / "logs_LINC_calibrator.tar").unlink()
        self.assertEqual(self.archive_with_mock_transfer()["status"], "verified")

    def test_symlink_products_are_archived_as_links(self):
        links = {"linked.h5": "results_LINC_calibrator/cal_solutions.h5",
                 "linked-dir": "results_LINC_calibrator/inspection",
                 "dangling": "not-in-run"}
        for name, target in links.items():
            (self.run / name).symlink_to(target)
        result = self.archive_with_mock_transfer()
        with tarfile.open(result["archive_path"]) as tar:
            for name, target in links.items():
                entry = tar.getmember(f"{self.run.name}/{name}")
                self.assertTrue(entry.issym())
                self.assertEqual(entry.linkname, target)
            self.assertNotIn(f"{self.run.name}/linked-dir/bandpass.png", tar.getnames())

    def test_measurement_sets_are_included_with_or_without_ms_suffix(self):
        names = ("raw.MS", "working.dp3concat")
        for name in names:
            ms = self.run / name
            ms.mkdir()
            (ms / "table.dat").write_bytes(b"measurement set")
            for subtable in ("ANTENNA", "DATA_DESCRIPTION", "SPECTRAL_WINDOW"):
                (ms / subtable).mkdir()
        result = self.archive_with_mock_transfer()
        with tarfile.open(result["archive_path"]) as tar:
            for name in names:
                member = tar.extractfile(f"{self.run.name}/{name}/table.dat")
                self.assertEqual(member.read(), b"measurement set")

    def test_staging_inside_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            archive_linc_calibrator(self.run, self.run / "archives")

    def test_failed_packaging_leaves_no_partial_tar(self):
        with patch.object(tarfile.TarFile, "add", side_effect=OSError("test read failure")):
            with self.assertRaisesRegex(OSError, "test read failure"):
                self.archive_with_mock_transfer()
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_upload_uses_immutable_copy_and_readback_before_reporting_verified(self):
        config = self.root / "rclone.conf"
        config.write_text("test config")
        with patch.object(archive_surf_tape.subprocess, "run") as run:
            receipt = archive_linc_calibrator(self.run, self.staging,
                                         destination="archive:LINC_calibrators", rclone_config=config)
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["verification"], "rclone_readback")
        self.assertEqual(run.call_count, 2)
        copy, check = [call.args[0] for call in run.call_args_list]
        self.assertEqual(copy[:4], ["rclone", "--config", str(config.resolve()), "copyto"])
        self.assertIn("--immutable", copy)
        self.assertEqual(copy[5], receipt["remote_path"])
        self.assertIn("--download", check)
        self.assertIn("--one-way", check)
        self.assertEqual(check[-1], "/" + Path(receipt["archive_path"]).name)
        self.assertTrue(all(call.kwargs == {"check": True} for call in run.call_args_list))
        self.assert_source_unchanged()

    def test_default_surf_upload_checks_size_and_adler32_without_readback(self):
        config = self.root / "maca_ilotss.conf"
        config.write_text(
            "[maca_ilotss]\n"
            "type = webdav\n"
            "url = https://webdav.grid.surfsara.nl:2880/pnfs/project/\n"
            "vendor = other\n"
            "bearer_token = fixture-token\n"
        )
        for fault in (None, "checksum", "size", "missing digest"):
            with (
                self.subTest(fault=fault),
                patch.dict(os.environ, {"RCLONE_CONFIG_DIR": str(self.root)}),
                patch.object(archive_surf_tape.subprocess, "run") as run,
                patch.object(archive_surf_tape, "HTTPSConnection") as connect,
            ):
                connection = connect.return_value
                response = connection.getresponse.return_value
                response.status = 200

                def getheader(name, default=None):
                    archive, = self.staging.glob("*.tar")
                    size = archive.stat().st_size
                    checksum = zlib.adler32(archive.read_bytes())
                    if fault == "size":
                        size += 1
                    if fault == "checksum":
                        checksum ^= 1
                    headers = {"Content-Length": str(size)}
                    if fault != "missing digest":
                        headers["Digest"] = f"adler32={checksum:08x}"
                    return headers.get(name, default)

                response.getheader.side_effect = getheader
                if fault is None:
                    receipt = archive_linc_calibrator(self.run, self.staging)
                    self.assertEqual(receipt["status"], "verified")
                    self.assertEqual(receipt["verification"], "dcache_adler32")
                else:
                    with self.assertRaisesRegex(RuntimeError, "dCache"):
                        archive_linc_calibrator(self.run, self.staging)
                run.assert_called_once()
                archive, = self.staging.glob("*.tar")
                command = run.call_args.args[0]
                self.assertEqual(command[:4], ["rclone", "--config", str(config.resolve()), "copyto"])
                self.assertEqual(command[5], f"maca_ilotss:LINC_calibrators/{archive.name}")
                connection.request.assert_called_once_with(
                    "HEAD", f"/pnfs/project/LINC_calibrators/{archive.name}",
                    headers={"Authorization": "Bearer fixture-token", "Want-Digest": "adler32"},
                )
                response.read.assert_not_called()
                connection.close.assert_called_once()
                self.assert_source_unchanged()

    def test_upload_destination_and_default_config_are_required(self):
        with self.assertRaisesRegex(ValueError, "archive destination"):
            archive_linc_calibrator(self.run, self.staging, destination=None)
        with patch.dict(os.environ, {"RCLONE_CONFIG_DIR": ""}):
            with self.assertRaisesRegex(ValueError, "RCLONE_CONFIG_DIR"):
                archive_linc_calibrator(self.run, self.staging)
        with patch.dict(os.environ, {"RCLONE_CONFIG_DIR": str(self.root)}):
            with self.assertRaisesRegex(FileNotFoundError, r"maca_ilotss\.conf"):
                archive_linc_calibrator(self.run, self.staging)
        self.assertFalse(self.staging.exists())

    def test_surf_upload_authentication_failure_comes_from_rclone(self):
        config = self.root / "maca_ilotss.conf"
        config.write_text(
            "[maca_ilotss]\ntype = webdav\nurl = https://webdav.grid.surfsara.nl/pnfs/project\n"
        )
        error = subprocess.CalledProcessError(1, "rclone copyto", stderr="401 Unauthorized")
        with (
            patch.object(archive_surf_tape.subprocess, "run", side_effect=error) as run,
            patch.object(archive_surf_tape, "_surf_archive_metadata") as metadata,
        ):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                archive_linc_calibrator(self.run, self.staging, rclone_config=config)
        self.assertIs(raised.exception, error)
        run.assert_called_once()
        self.assertIn("copyto", run.call_args.args[0])
        metadata.assert_not_called()
        self.assertTrue((self.staging / f"{self.run.name}.tar").is_file())

    def test_failed_copy_or_readback_retains_local_data_and_does_not_report_success(self):
        for failures in ([subprocess.CalledProcessError(1, "copyto")],
                         [None, subprocess.CalledProcessError(1, "check")]):
            with self.subTest(failures=failures):
                with patch.object(archive_surf_tape.subprocess, "run", side_effect=failures):
                    with self.assertRaises(subprocess.CalledProcessError):
                        archive_linc_calibrator(self.run, self.staging, destination="archive:calibrators")
                self.assertEqual(len(list(self.staging.glob("*.tar"))), 1)
                self.assert_source_unchanged()

    @unittest.skipUnless(shutil.which("rclone"), "rclone executable not installed")
    def test_real_rclone_to_local_destination_retries_and_preserves_conflicts(self):
        # This uses two temporary local directories, never an archive service.
        destination = self.root / "remote"
        first = archive_linc_calibrator(self.run, self.staging, destination=str(destination))
        self.assertEqual(first["status"], "verified")
        self.assertEqual(archive_linc_calibrator(self.run, self.staging, destination=str(destination)), first)
        remote_tar = Path(first["remote_path"])
        content = remote_tar.read_bytes()
        corrupted = bytes([content[0] ^ 1]) + content[1:]
        remote_tar.write_bytes(corrupted)
        with self.assertRaises(subprocess.CalledProcessError):
            archive_linc_calibrator(self.run, self.staging, destination=str(destination))
        self.assertEqual(remote_tar.read_bytes(), corrupted)
        self.assert_source_unchanged()

    @unittest.skipUnless(shutil.which("rclone"), "rclone executable not installed")
    def test_real_rclone_readback_detects_corruption_after_copy(self):
        original_run = subprocess.run

        def copy_then_corrupt(command, **kwargs):
            result = original_run(command, **kwargs)
            if command[1] == "copyto":
                remote_tar = Path(command[3])
                data = remote_tar.read_bytes()
                remote_tar.write_bytes(bytes([data[0] ^ 1]) + data[1:])
            return result

        with patch.object(archive_surf_tape.subprocess, "run", side_effect=copy_then_corrupt):
            with self.assertRaises(subprocess.CalledProcessError):
                archive_linc_calibrator(self.run, self.staging, destination=str(self.root / "remote"))
        self.assertEqual(len(list(self.staging.glob("*.tar"))), 1)
        self.assert_source_unchanged()


if __name__ == "__main__":
    unittest.main()
