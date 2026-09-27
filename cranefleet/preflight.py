"""Boot-time cross-checks between command-line settings and stored state.

The service powers on together with the machine, so nobody watches the
first seconds of a run. Preflight compares the command-line arguments,
the persisted site configuration and the restored snapshots before the
HTTP server starts; every disagreement is reported item by item and the
run aborts instead of serving with a known-bad setup.
"""

from __future__ import annotations

import json
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .cluster import Cluster, baseline_positions
from .config import SiteConfig
from .geometry import Point, bounds_of
from .store import SNAPSHOT_VERSION, decode_snapshot
from .wind import ALARM_THRESHOLD
from .wiring import MEMORY_DIRECTORIES

MAX_WIND_THRESHOLD = 50.0


@dataclass(frozen=True)
class Finding:
    """The outcome of checking one boot item."""

    item: str
    ok: bool
    detail: str

    def render(self) -> str:
        mark = "OK  " if self.ok else "FAIL"
        return f"[{mark}] {self.item}: {self.detail}"


@dataclass(frozen=True)
class Report:
    """Every finding of one preflight phase."""

    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return all(finding.ok for finding in self.findings)

    @property
    def failures(self) -> list[Finding]:
        return [finding for finding in self.findings if not finding.ok]

    def render(self) -> str:
        return "\n".join(finding.render() for finding in self.findings)


# ------------------------------------------------------------- environment


def check_data_dir(data_dir: str) -> Finding:
    """The data directory must exist (or be creatable) and accept writes."""

    if data_dir in MEMORY_DIRECTORIES:
        return Finding("data-dir", True, "ephemeral in-memory store")
    path = Path(data_dir)
    if path.exists() and not path.is_dir():
        return Finding("data-dir", False, f"{path} exists and is not a directory")
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path, prefix=".preflight-") as probe:
            probe.write(b"ok")
    except OSError as exc:
        return Finding("data-dir", False, f"{path} is not writable: {exc}")
    return Finding("data-dir", True, f"{path} is writable")


def check_snapshot(data_dir: str) -> Finding:
    """A stored boot snapshot must decode and match the current version."""

    if data_dir in MEMORY_DIRECTORIES:
        return Finding("snapshot", True, "ephemeral run, no snapshot")
    path = Path(data_dir) / "snapshot.json"
    if not path.exists():
        return Finding("snapshot", True, "no snapshot yet (first boot)")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        return Finding("snapshot", False, f"cannot read {path}: {exc}")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return Finding("snapshot", False, f"{path} is unreadable: {exc}")
    if not isinstance(document, dict):
        return Finding("snapshot", False, f"{path} is not a snapshot document")
    try:
        decode_snapshot(payload)
    except ValueError as exc:
        return Finding("snapshot", False, f"{path} is unreadable: {exc}")
    version = document.get("version")
    if version != SNAPSHOT_VERSION:
        return Finding(
            "snapshot",
            False,
            f"{path} has version {version!r}, expected {SNAPSHOT_VERSION}",
        )
    return Finding("snapshot", True, f"{path} matches snapshot version {SNAPSHOT_VERSION}")


def check_web_root(web_root: str) -> Finding:
    """The console frontend directory must hold the console page."""

    root = Path(web_root)
    if not root.is_dir():
        return Finding("web-root", False, f"{root} is not a directory")
    if not (root / "index.html").is_file():
        return Finding("web-root", False, f"{root} has no index.html")
    return Finding("web-root", True, f"{root} holds the console pages")


def check_listen(host: str, port: int) -> Finding:
    """The listen address must be syntactically usable."""

    if not host.strip():
        return Finding("listen", False, "host is empty")
    if not 1 <= port <= 65535:
        return Finding("listen", False, f"port {port} is out of range 1-65535")
    return Finding("listen", True, f"will listen on {host}:{port}")


def environment_report(data_dir: str, web_root: str, host: str, port: int) -> Report:
    """Check everything that must hold before the stored state is loaded."""

    return Report(
        findings=(
            check_data_dir(data_dir),
            check_snapshot(data_dir),
            check_web_root(web_root),
            check_listen(host, port),
        )
    )


# ------------------------------------------------------------------- state


def check_site_name(config: SiteConfig, expected_site: str | None) -> Finding:
    """The stored site name must match the site this machine serves."""

    name = config.site_name.strip()
    if not name:
        return Finding("site-name", False, "stored site_name is empty")
    if expected_site and expected_site.strip() and name != expected_site.strip():
        return Finding(
            "site-name",
            False,
            f"stored site_name {config.site_name!r} does not match "
            f"--expect-site {expected_site!r}; the data directory may belong to another site",
        )
    return Finding("site-name", True, f"site is {config.site_name!r}")


def check_safe_separation(
    config: SiteConfig, cluster: Cluster, boundary: list[Point]
) -> tuple[Finding, ...]:
    """The separation threshold must agree with the zone and the baseline."""

    safe = config.safe_separation
    if not math.isfinite(safe) or safe <= 0:
        return (
            Finding(
                "safe-separation",
                False,
                f"safe_separation must be a positive finite number, got {safe!r}",
            ),
        )
    findings: list[Finding] = [Finding("safe-separation", True, f"safe_separation is {safe} m")]
    if len(boundary) >= 2:
        bounds = bounds_of(boundary)
        diagonal = math.hypot(bounds.width(), bounds.height())
        if safe > diagonal:
            findings.append(
                Finding(
                    "safe-separation/boundary",
                    False,
                    f"safe_separation {safe} m exceeds the zone boundary diagonal "
                    f"{diagonal:.1f} m; no two cranes can ever satisfy it",
                )
            )
        else:
            findings.append(
                Finding(
                    "safe-separation/boundary",
                    True,
                    f"{safe} m fits the zone boundary (diagonal {diagonal:.1f} m)",
                )
            )
    pair = _closest_baseline_pair(baseline_positions(cluster))
    if pair is not None:
        first, second, distance = pair
        if distance < safe:
            findings.append(
                Finding(
                    "safe-separation/baseline",
                    False,
                    f"safe_separation {safe} m exceeds the closest baseline pair "
                    f"{first}/{second} at {distance:.1f} m; the fleet would boot "
                    "into a permanent conflict",
                )
            )
        else:
            findings.append(
                Finding(
                    "safe-separation/baseline",
                    True,
                    f"closest baseline pair {first}/{second} is {distance:.1f} m apart",
                )
            )
    return tuple(findings)


def check_wind_threshold(threshold: float = ALARM_THRESHOLD) -> Finding:
    """The wind alarm threshold must sit inside its sane range."""

    if not math.isfinite(threshold) or threshold <= 0 or threshold > MAX_WIND_THRESHOLD:
        return Finding(
            "wind-threshold",
            False,
            f"wind alarm threshold must be within (0, {MAX_WIND_THRESHOLD}] m/s, "
            f"got {threshold!r}",
        )
    return Finding("wind-threshold", True, f"wind alarm threshold is {threshold} m/s")


def state_report(
    config: SiteConfig,
    cluster: Cluster,
    boundary: list[Point],
    expected_site: str | None = None,
    wind_threshold: float = ALARM_THRESHOLD,
) -> Report:
    """Cross-check the restored configuration against the restored state."""

    findings: list[Finding] = [check_site_name(config, expected_site)]
    findings.extend(check_safe_separation(config, cluster, boundary))
    findings.append(check_wind_threshold(wind_threshold))
    return Report(findings=tuple(findings))


def _closest_baseline_pair(positions: dict[str, Point]) -> tuple[str, str, float] | None:
    identifiers = sorted(positions)
    best: tuple[str, str, float] | None = None
    for index, first in enumerate(identifiers):
        for second in identifiers[index + 1 :]:
            distance = positions[first].distance(positions[second])
            if best is None or distance < best[2]:
                best = (first, second, distance)
    return best
