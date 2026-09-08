from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast

from .adapter import MAX_REQUEST_BYTES, LabError, ReplayLabAdapter

_STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
_MAX_ASSET_BYTES = 5_000_000
_READ_CHUNK_BYTES = 65_536
REQUEST_TIMEOUT_SECONDS = 5.0
_CLIENT_DISCONNECT_ERRNOS = {
    errno.ECONNABORTED,
    errno.ECONNRESET,
    errno.EPIPE,
    errno.ETIMEDOUT,
}
_CSP = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; "
    "script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'none'; frame-ancestors 'none'"
)


class LabHTTPServer(HTTPServer):
    request_queue_size = 8

    def __init__(
        self,
        address: tuple[str, int],
        asset_root: Path,
        adapter: ReplayLabAdapter,
    ) -> None:
        self.asset_root = asset_root
        self.adapter = adapter
        super().__init__(address, _LabHandler)


class _LabHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ToolCallReplayLab/0.1"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    @property
    def lab_server(self) -> LabHTTPServer:
        return cast(LabHTTPServer, self.server)

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _security_headers(self, cache_control: str) -> None:
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")

    def _send(self, status: int, content_type: str, content: bytes, cache: str) -> None:
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self._security_headers(cache)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)
        except OSError as error:
            if error.errno not in _CLIENT_DISCONNECT_ERRNOS:
                raise
            self.close_connection = True

    def _send_json(self, status: int, value: object) -> None:
        content = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", content, "no-store")

    def _send_error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message[:256]}})

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        self.close_connection = True
        if code == 501:
            self._send_error(405, "METHOD_NOT_ALLOWED", "Method is not allowed.")
        else:
            self._send_error(code, "HTTP_REQUEST_INVALID", "HTTP request is invalid.")

    def _authorized_origin(self) -> bool:
        hosts = self.headers.get_all("Host", failobj=[])
        if len(hosts) != 1:
            self._send_error(403, "HOST_FORBIDDEN", "Host is not allowed.")
            return False
        port = cast(tuple[str, int], self.lab_server.server_address)[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if hosts[0] not in allowed_hosts:
            self._send_error(403, "HOST_FORBIDDEN", "Host is not allowed.")
            return False
        origins = self.headers.get_all("Origin", failobj=[])
        expected_origin = f"http://{hosts[0]}"
        if len(origins) > 1 or (origins and origins[0] != expected_origin):
            self._send_error(403, "ORIGIN_FORBIDDEN", "Origin is not allowed.")
            return False
        return True

    def _static_bytes(self, filename: str) -> bytes | None:
        path = self.lab_server.asset_root / filename
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size > _MAX_ASSET_BYTES:
                return None
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                content = source.read(_MAX_ASSET_BYTES + 1)
            if len(content) > _MAX_ASSET_BYTES:
                return None
            return content
        except OSError:
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def do_GET(self) -> None:
        if not self._authorized_origin():
            return
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if self.path == "/api/scenarios":
            self._send_json(200, self.lab_server.adapter.list_scenarios())
            return
        asset = _STATIC_ROUTES.get(self.path)
        if asset is None:
            self._send_error(404, "ROUTE_NOT_FOUND", "Route does not exist.")
            return
        filename, content_type = asset
        content = self._static_bytes(filename)
        if content is None:
            self._send_error(404, "ASSET_NOT_FOUND", "Asset is not available.")
            return
        self._send(200, content_type, content, "no-cache")

    def _read_body(self) -> bytes | None:
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            self.close_connection = True
            self._send_error(
                400,
                "TRANSFER_ENCODING_UNSUPPORTED",
                "Transfer encoding is not supported.",
            )
            return None
        content_types = self.headers.get_all("Content-Type", failobj=[])
        if content_types != ["application/json"]:
            self.close_connection = True
            self._send_error(
                415, "MEDIA_TYPE_UNSUPPORTED", "Content-Type must be application/json."
            )
            return None
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if not lengths:
            self.close_connection = True
            self._send_error(411, "CONTENT_LENGTH_REQUIRED", "Content-Length is required.")
            return None
        if len(lengths) != 1:
            self.close_connection = True
            self._send_error(400, "CONTENT_LENGTH_INVALID", "Content-Length is invalid.")
            return None
        raw_length = lengths[0]
        if (
            not raw_length.isascii()
            or not raw_length.isdecimal()
            or len(raw_length) > len(str(MAX_REQUEST_BYTES))
        ):
            self.close_connection = True
            self._send_error(400, "CONTENT_LENGTH_INVALID", "Content-Length is invalid.")
            return None
        length = int(raw_length)
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            self._send_error(
                413,
                "REQUEST_TOO_LARGE",
                f"Request exceeds the {MAX_REQUEST_BYTES}-byte limit.",
            )
            return None
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            try:
                chunk = self.rfile.read(min(remaining, _READ_CHUNK_BYTES))
            except TimeoutError:
                self.close_connection = True
                self._send_error(408, "REQUEST_TIMEOUT", "Request body timed out.")
                return None
            except OSError:
                self.close_connection = True
                self._send_error(400, "REQUEST_TRUNCATED", "Request body is incomplete.")
                return None
            if not chunk:
                self.close_connection = True
                self._send_error(400, "REQUEST_TRUNCATED", "Request body is incomplete.")
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def do_POST(self) -> None:
        if not self._authorized_origin():
            return
        if self.path != "/api/replay":
            self.close_connection = True
            self._send_error(404, "ROUTE_NOT_FOUND", "Route does not exist.")
            return
        body = self._read_body()
        if body is None:
            return
        try:
            response = self.lab_server.adapter.replay_bytes(body)
        except LabError as error:
            self._send_error(error.status, error.code, error.message)
            return
        except Exception:
            self._send_error(500, "INTERNAL_ERROR", "The local replay could not be completed.")
            return
        self._send_json(200, response)

    def _method_not_allowed(self) -> None:
        self.close_connection = True
        self._send_error(405, "METHOD_NOT_ALLOWED", "Method is not allowed.")

    do_HEAD = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_TRACE = _method_not_allowed
    do_CONNECT = _method_not_allowed


def create_server(
    host: str = "127.0.0.1",
    port: int = 4173,
    *,
    asset_root: Path | None = None,
) -> LabHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("host must be loopback")
    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    root = asset_root if asset_root is not None else Path(__file__).with_name("static")
    return LabHTTPServer((host, port), root, ReplayLabAdapter())


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toolcall-replay-lab")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--asset-root", type=Path)
    parsed = parser.parse_args(arguments)
    if not 0 <= parsed.port <= 65_535:
        parser.error("--port must be between 0 and 65535")
    try:
        server = create_server(parsed.host, parsed.port, asset_root=parsed.asset_root)
    except OSError:
        print(
            "ERROR START_FAILED: local trace laboratory could not bind to loopback",
            file=sys.stderr,
        )
        return 2
    host, port = cast(tuple[str, int], server.server_address)
    print(f"ToolCall Replay laboratory: http://{host}:{port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
