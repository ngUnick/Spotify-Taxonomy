"""Transient loopback callback used by the PKCE login flow."""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import time
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlsplit

from .errors import SpotifyAdapterError, SpotifyErrorKind

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"


@dataclass(frozen=True, slots=True)
class CallbackResult:
    state: str | None
    code: str | None
    error: str | None


class AuthorizationCallback(Protocol):
    @property
    def redirect_uri(self) -> str: ...

    def wait(self, timeout_seconds: float) -> CallbackResult: ...

    def close(self) -> None: ...


class LoopbackCallbackServer:
    """Single-flow HTTP listener bound only to the IPv4 loopback interface."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._result: CallbackResult | None = None
        self._monotonic = monotonic
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                parsed = urlsplit(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_error(404)
                    return
                values = parse_qs(parsed.query, keep_blank_values=True)
                outer._result = CallbackResult(
                    state=_one(values, "state"),
                    code=_one(values, "code"),
                    error=_one(values, "error"),
                )
                body = b"Authorization received. You may close this window."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self._server = HTTPServer((CALLBACK_HOST, 0), Handler)

    @property
    def redirect_uri(self) -> str:
        return f"http://{CALLBACK_HOST}:{self._server.server_port}{CALLBACK_PATH}"

    def wait(self, timeout_seconds: float) -> CallbackResult:
        deadline = self._monotonic() + timeout_seconds
        while self._result is None:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise SpotifyAdapterError(
                    SpotifyErrorKind.AUTHORIZATION_TIMEOUT,
                    "Spotify authorization timed out",
                )
            self._server.timeout = min(remaining, 1.0)
            self._server.handle_request()
        return self._result

    def close(self) -> None:
        self._result = None
        self._server.server_close()


def _one(values: dict[str, list[str]], name: str) -> str | None:
    items = values.get(name)
    return items[0] if items and len(items) == 1 else None
