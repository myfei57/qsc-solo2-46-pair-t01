"""Command-line entry point for the control service."""

from __future__ import annotations

import argparse
import logging

from . import preflight
from .config import ConfigError
from .server import run
from .wiring import build_app

LOGGER = logging.getLogger("cranefleet")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cranefleet", description="CraneFleet control service")
    parser.add_argument("--host", default="127.0.0.1", help="listen address")
    parser.add_argument("--port", type=int, default=8080, help="listen port")
    parser.add_argument(
        "--data-dir",
        default="data",
        help="persistent data directory, or 'memory' for an ephemeral run",
    )
    parser.add_argument("--web-root", default="web", help="console frontend directory")
    parser.add_argument("--log-level", default="info", help="logging level")
    parser.add_argument(
        "--expect-site",
        default=None,
        help="site name this machine must serve; boot aborts when the stored "
        "configuration names a different site",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    environment = preflight.environment_report(args.data_dir, args.web_root, args.host, args.port)
    _log_report(environment)
    if not environment.ok:
        return _refuse(environment)
    try:
        app = build_app(args.data_dir, args.web_root)
    except ConfigError as exc:
        LOGGER.error("preflight [FAIL] config: %s", exc)
        return 1
    except (KeyError, ValueError) as exc:
        LOGGER.error("preflight [FAIL] stored-state: persisted data cannot be loaded: %r", exc)
        return 1
    state = preflight.state_report(
        app.console.config,
        app.console.cluster,
        app.console.zone.boundary_copy(),
        expected_site=args.expect_site,
    )
    _log_report(state)
    if not state.ok:
        # The data directory is suspect: leave it untouched (no shutdown
        # flush) and refuse to serve.
        return _refuse(state)
    try:
        run(app.console, args.host, args.port)
    except KeyboardInterrupt:
        LOGGER.info("interrupted, shutting down")
    finally:
        app.shutdown()
    return 0


def _log_report(report: preflight.Report) -> None:
    for finding in report.findings:
        if finding.ok:
            LOGGER.info("preflight %s", finding.render())
        else:
            LOGGER.error("preflight %s", finding.render())


def _refuse(report: preflight.Report) -> int:
    LOGGER.error(
        "preflight failed: %s; refusing to serve",
        "; ".join(finding.item for finding in report.failures),
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
