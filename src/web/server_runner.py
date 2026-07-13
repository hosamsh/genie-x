"""Utilities for running the web server with explicit shutdown diagnostics."""

from __future__ import annotations

import signal
from types import FrameType

import uvicorn

from src.shared.logging.logger import get_logger

logger = get_logger(__name__)


def _signal_name(sig: int | None) -> str:
    if sig is None:
        return "unknown"

    try:
        return signal.Signals(sig).name
    except ValueError:
        return f"signal {sig}"


class LoggedUvicornServer(uvicorn.Server):
    """Uvicorn server that records and logs the shutdown reason."""

    def __init__(self, config: uvicorn.Config):
        super().__init__(config)
        self.shutdown_signal: int | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self.shutdown_signal = sig

        if sig == signal.SIGINT:
            logger.info("[WEB] Shutdown requested via Ctrl+C (SIGINT)")
        elif sig == signal.SIGTERM:
            logger.info("[WEB] Shutdown requested via process termination (SIGTERM)")
        else:
            logger.info(f"[WEB] Shutdown requested via {_signal_name(sig)}")

        super().handle_exit(sig, frame)


def run_web_server(app_path: str, host: str, port: int) -> int:
    """Run the web server and return an explicit process exit code."""

    config = uvicorn.Config(
        app_path,
        host=host,
        port=port,
        reload=False,
    )
    server = LoggedUvicornServer(config)

    try:
        server.run()
    except KeyboardInterrupt:
        if server.shutdown_signal is None:
            logger.info("[WEB] Shutdown requested via KeyboardInterrupt")
        return 0
    except Exception:
        logger.exception("[WEB] Server crashed with an unhandled exception")
        return 1

    if server.shutdown_signal is not None:
        logger.info(f"[WEB] Server shutdown complete after {_signal_name(server.shutdown_signal)}")
        return 0

    if not server.started:
        logger.error("[WEB] Server exited before startup completed")
        return 1

    logger.info("[WEB] Server stopped without an OS signal")
    return 0