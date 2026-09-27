"""Tests for boot-time parameter verification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cranefleet.__main__ import main
from cranefleet.config import CONFIG_KEY, SiteConfig, save_config
from cranefleet.errors import ConfigError, StoreNotFound
from cranefleet.preflight import (
    ITEM_DATA_DIR,
    ITEM_SAFE_SEPARATION,
    ITEM_WIND_ALARM_THRESHOLD,
    SiteExpectations,
    load_expectations,
    run_preflight,
)
from cranefleet.store import FileStore, MemoryStore

EXPECTED = SiteExpectations(
    site_name="华东三号工地",
    safe_separation=12.0,
    wind_alarm_threshold=10.0,
)


def write_site_file(directory: str, payload: dict | None = None) -> str:
    path = Path(directory) / "site.json"
    path.write_text(
        json.dumps(payload or EXPECTED.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    return str(path)


class ExpectationsLoadingTests(unittest.TestCase):
    def test_loads_valid_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_site_file(directory)
            loaded = load_expectations(path)
            self.assertEqual(loaded, EXPECTED)

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                load_expectations(Path(directory) / "missing.json")

    def test_bad_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site.json"
            path.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_expectations(path)

    def test_invalid_fields(self) -> None:
        cases = [
            {"safe_separation": 12.0, "wind_alarm_threshold": 10.0},
            {"site_name": "  ", "safe_separation": 12.0, "wind_alarm_threshold": 10.0},
            {"site_name": "工地", "safe_separation": 0, "wind_alarm_threshold": 10.0},
            {"site_name": "工地", "safe_separation": -1.0, "wind_alarm_threshold": 10.0},
            {"site_name": "工地", "safe_separation": "wide", "wind_alarm_threshold": 10.0},
            {"site_name": "工地", "safe_separation": 12.0, "wind_alarm_threshold": 0},
            {"site_name": "工地", "safe_separation": True, "wind_alarm_threshold": 10.0},
            ["not", "an", "object"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            for payload in cases:
                path = write_site_file(directory, payload)
                with self.subTest(payload=payload):
                    with self.assertRaises(ConfigError):
                        load_expectations(path)


class PreflightTests(unittest.TestCase):
    def test_empty_directory_is_seeded_and_passes(self) -> None:
        store = MemoryStore()
        report = run_preflight(store, EXPECTED)
        self.assertTrue(report.ok)
        self.assertTrue(report.initialised)
        self.assertEqual(store.keys(), [CONFIG_KEY])
        config = SiteConfig.from_dict(json.loads(store.load(CONFIG_KEY)))
        self.assertEqual(config.site_name, EXPECTED.site_name)
        self.assertEqual(config.safe_separation, EXPECTED.safe_separation)

    def test_existing_directory_of_same_site_passes(self) -> None:
        store = MemoryStore()
        save_config(store, SiteConfig(site_name="华东三号工地", safe_separation=12.0))
        store.save("zone/zone-1", b"{}")
        report = run_preflight(store, EXPECTED)
        self.assertTrue(report.ok)
        self.assertFalse(report.initialised)

    def test_foreign_site_directory_is_refused(self) -> None:
        store = MemoryStore()
        save_config(store, SiteConfig(site_name="华东一号工地", safe_separation=20.0))
        report = run_preflight(store, EXPECTED)
        self.assertFalse(report.ok)
        items = {mismatch.item for mismatch in report.mismatches}
        self.assertIn(ITEM_DATA_DIR, items)
        self.assertIn(ITEM_SAFE_SEPARATION, items)
        rendered = report.render("data", "site.json")
        self.assertIn("华东三号工地", rendered)
        self.assertIn("华东一号工地", rendered)

    def test_unmarked_directory_with_state_is_refused(self) -> None:
        store = MemoryStore()
        store.save("zone/zone-1", b"{}")
        report = run_preflight(store, EXPECTED)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.mismatches), 1)
        self.assertEqual(report.mismatches[0].item, ITEM_DATA_DIR)
        with self.assertRaises(StoreNotFound):
            store.load(CONFIG_KEY)

    def test_corrupt_site_config_is_refused(self) -> None:
        store = MemoryStore()
        store.save(CONFIG_KEY, b"{broken")
        report = run_preflight(store, EXPECTED)
        self.assertFalse(report.ok)
        self.assertEqual(report.mismatches[0].item, ITEM_DATA_DIR)

    def test_threshold_conflict_is_reported_even_when_site_matches(self) -> None:
        store = MemoryStore()
        save_config(store, SiteConfig(site_name="华东三号工地", safe_separation=15.0))
        report = run_preflight(store, EXPECTED)
        self.assertFalse(report.ok)
        self.assertEqual([m.item for m in report.mismatches], [ITEM_SAFE_SEPARATION])

    def test_wind_alarm_threshold_conflict_is_reported(self) -> None:
        store = MemoryStore()
        expectations = SiteExpectations(
            site_name="华东三号工地",
            safe_separation=12.0,
            wind_alarm_threshold=14.0,
        )
        report = run_preflight(store, expectations)
        self.assertFalse(report.ok)
        items = {mismatch.item for mismatch in report.mismatches}
        self.assertIn(ITEM_WIND_ALARM_THRESHOLD, items)
        mismatch = next(m for m in report.mismatches if m.item == ITEM_WIND_ALARM_THRESHOLD)
        self.assertEqual(mismatch.expected, 14.0)
        self.assertEqual(mismatch.actual, 10.0)

    def test_seeded_file_store_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_store = FileStore(directory)
            report = run_preflight(file_store, EXPECTED)
            self.assertTrue(report.ok)

            reopened = FileStore(directory)
            second = run_preflight(reopened, EXPECTED)
            self.assertTrue(second.ok)
            self.assertFalse(second.initialised)


class MainPreflightTests(unittest.TestCase):
    def test_fresh_persistent_directory_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = str(Path(directory) / "data")
            site_file = write_site_file(directory)
            with patch("cranefleet.__main__.run") as serve:
                code = main(
                    ["--data-dir", data_dir, "--site-file", site_file, "--web-root", "web"]
                )
            self.assertEqual(code, 0)
            serve.assert_called_once()

    def test_foreign_directory_refuses_to_serve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = str(Path(directory) / "data")
            seeded = FileStore(data_dir)
            save_config(seeded, SiteConfig(site_name="别的工地", safe_separation=5.0))
            site_file = write_site_file(directory)
            with patch("cranefleet.__main__.run") as serve:
                code = main(
                    ["--data-dir", data_dir, "--site-file", site_file, "--web-root", "web"]
                )
            self.assertEqual(code, 2)
            serve.assert_not_called()

    def test_missing_site_file_refuses_to_serve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("cranefleet.__main__.run") as serve:
                code = main(
                    [
                        "--data-dir",
                        str(Path(directory) / "data"),
                        "--site-file",
                        str(Path(directory) / "missing.json"),
                    ]
                )
            self.assertEqual(code, 2)
            serve.assert_not_called()

    def test_memory_mode_skips_preflight(self) -> None:
        with patch("cranefleet.__main__.run") as serve:
            code = main(["--data-dir", "memory"])
        self.assertEqual(code, 0)
        serve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
