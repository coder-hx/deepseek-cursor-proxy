from __future__ import annotations

import logging as stdlib_logging
import sys
import threading
from typing import Any


LOG = stdlib_logging.getLogger("deepseek_cursor_proxy")

DEFAULT_INFO_LOG_FORMAT = "%(message)s"
DEFAULT_WARNING_LOG_FORMAT = "%(levelname)s %(message)s"
VERBOSE_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


class ConsoleLogFormatter(stdlib_logging.Formatter):
    def __init__(self, *, verbose: bool) -> None:
        super().__init__()
        self.verbose = verbose
        self._verbose_formatter = stdlib_logging.Formatter(VERBOSE_LOG_FORMAT)
        self._info_formatter = stdlib_logging.Formatter(DEFAULT_INFO_LOG_FORMAT)
        self._warning_formatter = stdlib_logging.Formatter(DEFAULT_WARNING_LOG_FORMAT)

    def format(self, record: stdlib_logging.LogRecord) -> str:
        if self.verbose:
            return self._verbose_formatter.format(record)
        if record.levelno <= stdlib_logging.INFO:
            return self._info_formatter.format(record)
        return self._warning_formatter.format(record)


def configure_logging(*, verbose: bool) -> None:
    handler = stdlib_logging.StreamHandler()
    handler.setFormatter(ConsoleLogFormatter(verbose=verbose))
    stdlib_logging.basicConfig(
        level=stdlib_logging.INFO,
        handlers=[handler],
        force=True,
    )


class TerminalSpinner:
    hide_cursor = "\x1b[?25l"
    show_cursor = "\x1b[?25h"
    frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(
        self,
        *,
        enabled: bool,
        text: str,
        stream: Any | None = None,
        interval: float = 0.12,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled and bool(getattr(self.stream, "isatty", lambda: False)())
        self.text = text
        self.interval = interval
        self._stop = threading.Event()
        # External event (e.g. the server's shutdown flag) that also halts the
        # spinner. This keeps the daemon spinner thread from writing to stderr
        # while the interpreter is shutting down, which would otherwise abort
        # with `_enter_buffered_busy: could not acquire lock for <stderr>`.
        self._external_stop = stop_event
        self._thread: threading.Thread | None = None
        self._visible = False

    def start(self) -> "TerminalSpinner":
        if not self.enabled or self._thread is not None:
            return self
        if not self._write(self.hide_cursor):
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
        if self._visible:
            self._write("\r" + (" " * self._clear_width()) + "\r")
            self._visible = False
        self._write(self.show_cursor)

    def _run(self) -> None:
        index = 0
        while not self._stop.is_set() and not self._external_stopped():
            if not self._write("\r" + self.text.format(frame=self.frames[index])):
                return
            self._visible = True
            index = (index + 1) % len(self.frames)
            self._stop.wait(self.interval)

    def _external_stopped(self) -> bool:
        return self._external_stop is not None and self._external_stop.is_set()

    def _write(self, text: str) -> bool:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (ValueError, OSError):
            # Stream closed/closing (e.g. during interpreter shutdown).
            return False
        return True

    def _clear_width(self) -> int:
        return max(len(self.text.format(frame=frame)) for frame in self.frames)
