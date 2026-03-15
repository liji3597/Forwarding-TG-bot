from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from tg_forwarder.core.engine import ForwardingEngine


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tg-forwarder")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _run(config_path: Path) -> None:
    engine = ForwardingEngine(config_path=config_path)
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.request_shutdown)
        except (NotImplementedError, RuntimeError):
            pass

    await engine.start()


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    _setup_logging(args.log_level)

    logger = logging.getLogger("tg_forwarder")
    logger.info("starting tg-forwarder with config=%s", args.config)

    try:
        asyncio.run(_run(args.config))
    except KeyboardInterrupt:
        logger.info("shutdown requested by keyboard interrupt")
    except Exception:
        logger.exception("fatal error")
        raise


if __name__ == "__main__":
    main()
