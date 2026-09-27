"""Command-line entry point for the control service."""

from __future__ import annotations

import argparse
import logging

from .errors import ConfigError, CraneFleetError
from .preflight import load_expectations, run_preflight
from .server import run
from .wiring import MEMORY_DIRECTORIES, build_app, open_persistent_store

DEFAULT_SITE_FILE = "config/site-expectations.json"
PREFLIGHT_EXIT_CODE = 2


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
    parser.add_argument(
        "--site-file",
        default=DEFAULT_SITE_FILE,
        help="site expectations file used for boot-time parameter verification",
    )
    parser.add_argument("--log-level", default="info", help="logging level")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("cranefleet")

    store = None
    if args.data_dir not in MEMORY_DIRECTORIES:
        try:
            expectations = load_expectations(args.site_file)
        except ConfigError as exc:
            logger.error(
                "无法加载站点备案文件 %s: %s\n"
                "持久化部署必须提供与本站点一致的备案文件，服务拒绝启动。",
                args.site_file,
                exc,
            )
            return PREFLIGHT_EXIT_CODE

        store, _ = open_persistent_store(args.data_dir)
        report = run_preflight(store, expectations)
        if not report.ok:
            for line in report.render(args.data_dir, args.site_file).splitlines():
                logger.error(line)
            return PREFLIGHT_EXIT_CODE
        if report.initialised:
            logger.warning(
                "数据目录 %s 为空，已按备案文件初始化站点参数（%s, safe_separation=%s）",
                args.data_dir,
                expectations.site_name,
                expectations.safe_separation,
            )
        logger.info("启动参数核对通过，站点=%s", expectations.site_name)

    try:
        app = build_app(args.data_dir, args.web_root, store=store)
    except CraneFleetError as exc:
        logger.error("服务装配失败: %s", exc)
        return PREFLIGHT_EXIT_CODE
    try:
        run(app.console, args.host, args.port)
    except KeyboardInterrupt:
        logger.info("interrupted, shutting down")
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
