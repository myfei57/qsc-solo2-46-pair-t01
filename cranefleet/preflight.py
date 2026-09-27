"""Boot-time verification of the deployment parameters.

The service boots unattended together with the industrial PC, so it cannot rely
on an operator noticing that the persistent data directory was swapped for
another site's or that the parameters kept in different places disagree.

Before the service listens on any port, :func:`run_preflight` compares the live
values against a site-expectations file that lives outside the data directory:

* the data directory carries the site name it was initialised with, so a
  directory belonging to another site (or one carrying unmarked state) is
  detected;
* the site name persisted in the data directory matches the expectations;
* the safe-separation threshold and the wind alarm threshold match.

Every mismatch is collected in one pass so the operator gets the complete list
of problems; the process then refuses to start rather than serve with a
contradictory configuration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import config as site_config
from .config import CONFIG_KEY, SiteConfig
from .errors import ConfigError, StoreError, StoreNotFound
from .store import Store
from .wind import ALARM_THRESHOLD

ITEM_DATA_DIR = "data_dir"
ITEM_SITE_NAME = "site_name"
ITEM_SAFE_SEPARATION = "safe_separation"
ITEM_WIND_ALARM_THRESHOLD = "wind_alarm_threshold"


@dataclass(frozen=True)
class Mismatch:
    """One parameter that failed verification at boot."""

    item: str
    expected: object
    actual: object
    detail: str

    def render(self) -> str:
        return (
            f"  - {self.item}: {self.detail} "
            f"(期望: {self.expected!r}, 实际: {self.actual!r})"
        )


@dataclass(frozen=True)
class PreflightReport:
    """Outcome of a boot verification pass."""

    mismatches: list[Mismatch] = field(default_factory=list)
    initialised: bool = False

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def render(self, data_dir: str, site_file: str) -> str:
        lines = [
            "启动参数核对未通过，拒绝对外服务:",
            f"  数据目录: {data_dir}",
            f"  备案文件: {site_file}",
        ]
        lines.extend(mismatch.render() for mismatch in self.mismatches)
        return "\n".join(lines)


@dataclass(frozen=True)
class SiteExpectations:
    """The operator-maintained parameters the running service must agree with."""

    site_name: str
    safe_separation: float
    wind_alarm_threshold: float

    def to_dict(self) -> dict[str, object]:
        return {
            "site_name": self.site_name,
            "safe_separation": self.safe_separation,
            "wind_alarm_threshold": self.wind_alarm_threshold,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "SiteExpectations":
        if not isinstance(payload, dict):
            raise ConfigError("site expectations must be a JSON object")
        name = payload.get("site_name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("site expectations: site_name must be a non-empty string")
        separation = _positive_float(payload.get("safe_separation"), "safe_separation")
        alarm = _positive_float(payload.get("wind_alarm_threshold"), "wind_alarm_threshold")
        return cls(
            site_name=name.strip(),
            safe_separation=separation,
            wind_alarm_threshold=alarm,
        )


def _positive_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"site expectations: {field_name} must be a positive number")
    result = float(value)
    if result <= 0:
        raise ConfigError(f"site expectations: {field_name} must be positive")
    return result


def load_expectations(path: str | Path) -> SiteExpectations:
    """Load and validate the site-expectations file."""

    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"site expectations file not found: {file_path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read site expectations file: {file_path}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"site expectations file is not valid JSON: {file_path}") from exc
    return SiteExpectations.from_dict(payload)


def run_preflight(
    store: Store,
    expectations: SiteExpectations,
    *,
    alarm_threshold: float = ALARM_THRESHOLD,
) -> PreflightReport:
    """Verify every boot parameter against ``expectations``.

    A completely empty data directory is initialised from the expectations
    (first deployment). A directory that already holds other state but has no
    site configuration is refused as unmarked. Every disagreement found in the
    same pass is reported and nothing is rewritten.
    """

    try:
        identity_raw = store.load(CONFIG_KEY)
    except StoreNotFound:
        identity_raw = None
    except StoreError as exc:
        return PreflightReport(
            [
                Mismatch(
                    ITEM_DATA_DIR,
                    expectations.site_name,
                    "unreadable",
                    f"无法读取数据目录中的站点配置: {exc}",
                )
            ]
        )

    initialised = False
    if identity_raw is None:
        other_keys = [key for key in store.keys() if key != CONFIG_KEY]
        if other_keys:
            return PreflightReport(
                [
                    Mismatch(
                        ITEM_DATA_DIR,
                        expectations.site_name,
                        "unmarked",
                        "数据目录没有站点标识却已有其他状态，疑似其他站点的数据目录被换入",
                    )
                ]
            )
        _seed_new_directory(store, expectations)
        initialised = True

    mismatches: list[Mismatch] = []
    live: SiteConfig | None = None
    if identity_raw is not None:
        try:
            live = SiteConfig.from_dict(json.loads(identity_raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ConfigError) as exc:
            mismatches.append(
                Mismatch(
                    ITEM_DATA_DIR,
                    expectations.site_name,
                    "corrupt",
                    f"数据目录中的站点配置无法解析: {exc}",
                )
            )

    if live is not None:
        if live.site_name != expectations.site_name:
            mismatches.append(
                Mismatch(
                    ITEM_DATA_DIR,
                    expectations.site_name,
                    live.site_name,
                    "数据目录的站点标识不匹配，疑似挂载了其他站点的数据目录",
                )
            )
        if live.safe_separation != expectations.safe_separation:
            mismatches.append(
                Mismatch(
                    ITEM_SAFE_SEPARATION,
                    expectations.safe_separation,
                    live.safe_separation,
                    "安全距离阈值与备案不一致",
                )
            )

    if alarm_threshold != expectations.wind_alarm_threshold:
        mismatches.append(
            Mismatch(
                ITEM_WIND_ALARM_THRESHOLD,
                expectations.wind_alarm_threshold,
                alarm_threshold,
                "风速报警阈值（程序内置值）与备案不一致",
            )
        )

    return PreflightReport(mismatches, initialised=initialised)


def _seed_new_directory(store: Store, expectations: SiteExpectations) -> None:
    """Write the expectations into a fresh, otherwise empty data directory."""

    site_config.save_config(
        store,
        SiteConfig(
            site_name=expectations.site_name,
            safe_separation=expectations.safe_separation,
        ),
    )
