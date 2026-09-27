"""Unit tests for the boot-time preflight cross-checks."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cranefleet import __main__
from cranefleet.cluster import Cluster
from cranefleet.config import SiteConfig, save_config
from cranefleet.preflight import (
    check_data_dir,
    check_listen,
    check_safe_separation,
    check_site_name,
    check_snapshot,
    check_web_root,
    check_wind_threshold,
    environment_report,
    state_report,
)
from cranefleet.store import FileStore, encode_snapshot
from cranefleet.wiring import default_boundary, default_positions

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def default_cluster() -> Cluster:
    return Cluster(default_positions())


class DataDirCheckTests(unittest.TestCase):
    def test_memory_directory_passes(self) -> None:
        self.assertTrue(check_data_dir("memory").ok)

    def test_writable_directory_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "data")
            self.assertTrue(check_data_dir(target).ok)
            self.assertTrue(Path(target).is_dir())

    def test_file_in_place_of_directory_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "data"
            blocker.write_bytes(b"not a directory")
            finding = check_data_dir(str(blocker))
            self.assertFalse(finding.ok)
            self.assertIn("not a directory", finding.detail)


class SnapshotCheckTests(unittest.TestCase):
    def test_missing_snapshot_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(check_snapshot(directory).ok)

    def test_valid_snapshot_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_bytes(encode_snapshot({"config/site": b"{}"}))
            self.assertTrue(check_snapshot(directory).ok)

    def test_corrupt_snapshot_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "snapshot.json").write_bytes(b"not json")
            self.assertFalse(check_snapshot(directory).ok)

    def test_foreign_version_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_bytes(json.dumps({"version": 99, "data": {}}).encode("utf-8"))
            finding = check_snapshot(directory)
            self.assertFalse(finding.ok)
            self.assertIn("99", finding.detail)


class WebRootCheckTests(unittest.TestCase):
    def test_repository_web_root_passes(self) -> None:
        self.assertTrue(check_web_root(str(WEB_ROOT)).ok)

    def test_missing_directory_fails(self) -> None:
        self.assertFalse(check_web_root("/nonexistent/web").ok)

    def test_missing_index_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(check_web_root(directory).ok)


class ListenCheckTests(unittest.TestCase):
    def test_defaults_pass(self) -> None:
        self.assertTrue(check_listen("0.0.0.0", 8080).ok)

    def test_bad_ports_fail(self) -> None:
        self.assertFalse(check_listen("0.0.0.0", 0).ok)
        self.assertFalse(check_listen("0.0.0.0", 65536).ok)


class SiteNameCheckTests(unittest.TestCase):
    def test_default_passes_without_expectation(self) -> None:
        self.assertTrue(check_site_name(SiteConfig(), None).ok)

    def test_matching_expectation_passes(self) -> None:
        config = SiteConfig(site_name="华东三号工地")
        self.assertTrue(check_site_name(config, "华东三号工地").ok)

    def test_swapped_data_dir_fails(self) -> None:
        finding = check_site_name(SiteConfig(site_name="华东五号工地"), "华东三号工地")
        self.assertFalse(finding.ok)
        self.assertIn("华东五号工地", finding.detail)

    def test_blank_name_fails(self) -> None:
        self.assertFalse(check_site_name(SiteConfig(site_name="  "), None).ok)


class SafeSeparationCheckTests(unittest.TestCase):
    def test_defaults_pass(self) -> None:
        findings = check_safe_separation(SiteConfig(), default_cluster(), default_boundary())
        self.assertTrue(findings)
        self.assertTrue(all(finding.ok for finding in findings))

    def test_non_positive_fails(self) -> None:
        findings = check_safe_separation(
            SiteConfig(safe_separation=0.0), default_cluster(), default_boundary()
        )
        self.assertEqual(len(findings), 1)
        self.assertFalse(findings[0].ok)

    def test_threshold_bigger_than_boundary_fails(self) -> None:
        findings = check_safe_separation(
            SiteConfig(safe_separation=500.0), default_cluster(), default_boundary()
        )
        boundary = {finding.item: finding for finding in findings}
        self.assertFalse(boundary["safe-separation/boundary"].ok)

    def test_threshold_bigger_than_baseline_pair_fails(self) -> None:
        findings = check_safe_separation(
            SiteConfig(safe_separation=90.0), default_cluster(), default_boundary()
        )
        named = {finding.item: finding for finding in findings}
        self.assertTrue(named["safe-separation/boundary"].ok)
        self.assertFalse(named["safe-separation/baseline"].ok)
        self.assertIn("crane-1", named["safe-separation/baseline"].detail)
        self.assertIn("crane-2", named["safe-separation/baseline"].detail)


class WindThresholdCheckTests(unittest.TestCase):
    def test_default_passes(self) -> None:
        self.assertTrue(check_wind_threshold().ok)

    def test_out_of_range_fails(self) -> None:
        self.assertFalse(check_wind_threshold(0.0).ok)
        self.assertFalse(check_wind_threshold(500.0).ok)


class ReportTests(unittest.TestCase):
    def test_environment_report_defaults(self) -> None:
        report = environment_report("memory", str(WEB_ROOT), "127.0.0.1", 8080)
        self.assertTrue(report.ok)
        self.assertEqual(report.failures, [])
        self.assertIn("[OK  ]", report.render())

    def test_state_report_defaults(self) -> None:
        report = state_report(
            SiteConfig(), default_cluster(), default_boundary(), expected_site="华东三号工地"
        )
        self.assertTrue(report.ok)


class MainPreflightTests(unittest.TestCase):
    def test_main_serves_when_checks_pass(self) -> None:
        with mock.patch.object(__main__, "run") as mocked_run:
            exit_code = __main__.main(["--data-dir", "memory", "--web-root", str(WEB_ROOT)])
        self.assertEqual(exit_code, 0)
        mocked_run.assert_called_once()

    def test_main_refuses_a_swapped_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            save_config(FileStore(directory), SiteConfig(site_name="华东五号工地", safe_separation=20.0))
            with mock.patch.object(__main__, "run") as mocked_run:
                exit_code = __main__.main(
                    [
                        "--data-dir",
                        directory,
                        "--web-root",
                        str(WEB_ROOT),
                        "--expect-site",
                        "华东三号工地",
                    ]
                )
            self.assertEqual(exit_code, 1)
            mocked_run.assert_not_called()
            self.assertFalse((Path(directory) / "snapshot.json").exists())

    def test_main_refuses_a_conflicting_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            save_config(FileStore(directory), SiteConfig(safe_separation=90.0))
            with mock.patch.object(__main__, "run") as mocked_run:
                exit_code = __main__.main(
                    ["--data-dir", directory, "--web-root", str(WEB_ROOT)]
                )
            self.assertEqual(exit_code, 1)
            mocked_run.assert_not_called()

    def test_main_refuses_a_corrupt_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            FileStore(directory).save("config/site", b'{"site_name": "x", "safe_separation": "wide"}')
            with mock.patch.object(__main__, "run") as mocked_run:
                exit_code = __main__.main(
                    ["--data-dir", directory, "--web-root", str(WEB_ROOT)]
                )
            self.assertEqual(exit_code, 1)
            mocked_run.assert_not_called()

    def test_main_refuses_a_corrupt_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "snapshot.json").write_bytes(b"garbage")
            with mock.patch.object(__main__, "run") as mocked_run:
                exit_code = __main__.main(
                    ["--data-dir", directory, "--web-root", str(WEB_ROOT)]
                )
            self.assertEqual(exit_code, 1)
            mocked_run.assert_not_called()

    def test_main_refuses_a_missing_web_root(self) -> None:
        with mock.patch.object(__main__, "run") as mocked_run:
            exit_code = __main__.main(["--data-dir", "memory", "--web-root", "/nonexistent"])
        self.assertEqual(exit_code, 1)
        mocked_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
