from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import shutil
import subprocess
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from .logging import LOG


DEFAULT_NGROK_API_URL = "http://127.0.0.1:4040/api"


def local_tunnel_target(host: str, port: int) -> str:
    local_host = host.strip() or "127.0.0.1"
    if local_host in {"0.0.0.0", "::"}:
        local_host = "127.0.0.1"
    if ":" in local_host and not local_host.startswith("["):
        local_host = f"[{local_host}]"
    return f"http://{local_host}:{port}"


def parse_ngrok_public_url(payload: dict[str, Any]) -> str | None:
    records = payload.get("endpoints")
    if not isinstance(records, list):
        records = payload.get("tunnels")
    if not isinstance(records, list):
        return None

    public_urls = [
        public_url
        for record in records
        if isinstance(record, dict)
        for public_url in (record.get("url"), record.get("public_url"))
        if isinstance(public_url, str)
    ]
    for public_url in public_urls:
        if public_url.startswith("https://"):
            return public_url
    for public_url in public_urls:
        if public_url.startswith("http://"):
            return public_url
    return None


def ngrok_agent_urls(api_url: str) -> list[str]:
    normalized = api_url.rstrip("/")
    if normalized.endswith("/endpoints") or normalized.endswith("/tunnels"):
        return [normalized]
    return [f"{normalized}/endpoints", f"{normalized}/tunnels"]


@dataclass
class NgrokTunnel:
    target_url: str
    ngrok_url: str | None = None
    command: str = "ngrok"
    api_url: str = DEFAULT_NGROK_API_URL
    startup_timeout: float = 15.0

    process: subprocess.Popen[bytes] | None = None
    reused_existing: bool = False
    _output_lines: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    _reader_thread: threading.Thread | None = None

    def start(self) -> str:
        # If an ngrok agent is already running locally (a previous launch, or
        # the desktop app) reuse its public URL. ngrok's free plan only allows
        # a single simultaneous session, so spawning a second agent would just
        # exit immediately with ERR_NGROK_108.
        existing = self.existing_public_url()
        if existing is not None:
            LOG.info("reusing existing ngrok tunnel: %s", existing)
            self.reused_existing = True
            return existing

        if shutil.which(self.command) is None:
            raise RuntimeError(
                "ngrok is not installed or is not on PATH. Install it, then run "
                "`ngrok config add-authtoken <token>` once."
            )

        argv = [self.command, "http", self.target_url]
        if self.ngrok_url:
            argv.append(f"--url={self.ngrok_url}")

        self.process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._start_output_reader()
        try:
            return self.wait_for_public_url()
        except Exception:
            self.stop()
            raise

    def existing_public_url(self) -> str | None:
        for api_url in ngrok_agent_urls(self.api_url):
            try:
                with urlopen(api_url, timeout=1) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (OSError, URLError, json.JSONDecodeError):
                continue
            public_url = parse_ngrok_public_url(payload)
            if public_url:
                return public_url
        return None

    def wait_for_public_url(self) -> str:
        deadline = time.monotonic() + self.startup_timeout
        last_error = "ngrok did not report a public URL"
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(self._exit_error_message())
            for api_url in ngrok_agent_urls(self.api_url):
                try:
                    with urlopen(api_url, timeout=1) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    public_url = parse_ngrok_public_url(payload)
                    if public_url:
                        return public_url
                except (OSError, URLError, json.JSONDecodeError) as exc:
                    last_error = str(exc)
            time.sleep(0.25)
        raise RuntimeError(f"Timed out waiting for ngrok tunnel: {last_error}")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        LOG.info("stopping ngrok tunnel")
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def _start_output_reader(self) -> None:
        stream = self.process.stdout if self.process is not None else None
        if stream is None:
            return

        def _drain() -> None:
            try:
                for raw in stream:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        self._output_lines.append(line)
            except Exception:
                # The pipe closed when ngrok exited; nothing more to read.
                pass

        self._reader_thread = threading.Thread(target=_drain, daemon=True)
        self._reader_thread.start()

    def _captured_output(self) -> str:
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1)
        return "\n".join(self._output_lines).strip()

    def _exit_error_message(self) -> str:
        detail = self._captured_output()
        base = "ngrok exited before creating a tunnel"
        if not detail:
            return (
                f"{base} (no output captured). Run "
                f"`{self.command} http {self.target_url}` manually to see why; "
                "the usual causes are a missing authtoken "
                f"(`{self.command} config add-authtoken <token>`) or another "
                "ngrok session already running."
            )
        lowered = detail.lower()
        if "err_ngrok_108" in lowered or "simultaneous" in lowered:
            detail += (
                "\nhint: ngrok's free plan allows only one session at a time. "
                "Stop the other ngrok agent (close the desktop app or kill the "
                "stray ngrok.exe), then retry. The proxy now reuses an existing "
                "ngrok tunnel automatically when one is already running."
            )
        elif "authtoken" in lowered or "err_ngrok_105" in lowered:
            detail += (
                f"\nhint: run `{self.command} config add-authtoken <token>` once "
                "with the token from your ngrok dashboard."
            )
        return f"{base}: {detail}"
