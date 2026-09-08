from __future__ import annotations

import http.client
import errno
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, cast

import pytest

import toolcall_replay_lab.server as server_module
from toolcall_replay_lab.adapter import MAX_REQUEST_BYTES, MAX_UPLOAD_BYTES
from toolcall_replay_lab.server import LabHTTPServer, create_server

from conftest import ROOT


@pytest.fixture
def lab_server(tmp_path: Path) -> Iterator[tuple[str, int]]:
    assets = tmp_path / "static"
    assets.mkdir()
    (assets / "index.html").write_text(
        "<!doctype html><title>Trace laboratory</title>", encoding="utf-8"
    )
    (assets / "styles.css").write_text("body { color: #111; }\n", encoding="utf-8")
    (assets / "app.js").write_text("document.body.dataset.ready = 'true';\n", encoding="utf-8")
    server = create_server("127.0.0.1", 0, asset_root=assets)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        yield host, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(
    address: tuple[str, int],
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, http.client.HTTPMessage, bytes]:
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    content = response.read()
    response_headers = response.headers
    status = response.status
    connection.close()
    return status, response_headers, content


def _json(content: bytes) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(content))


def _assert_security_headers(headers: http.client.HTTPMessage) -> None:
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers.get("Access-Control-Allow-Origin") is None
    assert headers["Connection"] == "close"


def test_server_serializes_requests_with_a_bounded_listen_backlog() -> None:
    assert not issubclass(LabHTTPServer, ThreadingMixIn)
    assert LabHTTPServer.__bases__ == (HTTPServer,)
    assert LabHTTPServer.request_queue_size == 8


def test_health_and_scenario_api_are_json_no_store_with_security_headers(
    lab_server: tuple[str, int],
) -> None:
    health_status, health_headers, health_content = _request(lab_server, "GET", "/healthz")
    scenarios_status, scenarios_headers, scenarios_content = _request(
        lab_server, "GET", "/api/scenarios"
    )

    assert health_status == 200
    assert _json(health_content) == {"status": "ok"}
    assert scenarios_status == 200
    assert [item["id"] for item in _json(scenarios_content)["scenarios"]] == [
        "approved-update",
        "risky-broad-update",
    ]
    for headers in (health_headers, scenarios_headers):
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert headers["Cache-Control"] == "no-store"
        _assert_security_headers(headers)


def test_post_replay_reaches_the_real_evaluator(lab_server: tuple[str, int]) -> None:
    body = json.dumps(
        {"scenario_id": "risky-broad-update", "source": "builtin"},
        separators=(",", ":"),
    ).encode("utf-8")
    status, headers, content = _request(
        lab_server,
        "POST",
        "/api/replay",
        body=body,
        headers={"Content-Type": "application/json"},
    )

    response = _json(content)
    assert status == 200
    assert response["report"]["verdict"] == "FAIL"
    assert response["report"]["cases"][0]["diff"]["new_failures"] == [
        "lookup-scope",
        "no-directory-export",
        "update-needs-approval",
    ]
    assert response["determinism"]["verified"] is True
    assert response["timeline"]["candidate"][1]["tool"] == "directory.export"
    assert headers["Cache-Control"] == "no-store"
    _assert_security_headers(headers)


def test_post_allows_the_bounded_upload_plus_its_request_envelope(
    lab_server: tuple[str, int],
) -> None:
    event = {
        "schema_version": "1.0",
        "case_id": "bounded-update",
        "step": 1,
        "kind": "result",
        "status": "completed",
        "output": {"padding": "x" * 999_800},
    }
    body = json.dumps(
        {
            "scenario_id": "approved-update",
            "source": "upload",
            "trace": {"schema_version": "1.0", "events": [event]},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(body) > MAX_UPLOAD_BYTES

    status, _, content = _request(
        lab_server,
        "POST",
        "/api/replay",
        body=body,
        headers={"Content-Type": "application/json"},
    )

    assert status == 200
    assert _json(content)["timeline"]["candidate"][0] == event


@pytest.mark.parametrize(
    ("path", "content_type", "needle"),
    [
        ("/", "text/html; charset=utf-8", b"Trace laboratory"),
        ("/index.html", "text/html; charset=utf-8", b"Trace laboratory"),
        ("/styles.css", "text/css; charset=utf-8", b"color"),
        ("/app.js", "text/javascript; charset=utf-8", b"dataset.ready"),
    ],
)
def test_static_assets_are_served_from_an_exact_allowlist(
    lab_server: tuple[str, int], path: str, content_type: str, needle: bytes
) -> None:
    status, headers, content = _request(lab_server, "GET", path)

    assert status == 200
    assert headers["Content-Type"] == content_type
    assert headers["Cache-Control"] == "no-cache"
    assert needle in content
    _assert_security_headers(headers)


@pytest.mark.parametrize("path", ["/missing", "/static/", "/../README.md", "/index.html?x=1"])
def test_unknown_listing_traversal_and_query_routes_are_closed(
    lab_server: tuple[str, int], path: str
) -> None:
    status, headers, content = _request(lab_server, "GET", path)

    assert status == 404
    assert _json(content)["error"]["code"] == "ROUTE_NOT_FOUND"
    _assert_security_headers(headers)


def test_cross_origin_and_unrecognized_host_are_rejected(lab_server: tuple[str, int]) -> None:
    host, port = lab_server
    status_origin, _, origin_content = _request(
        lab_server,
        "GET",
        "/api/scenarios",
        headers={"Origin": "https://outside.invalid"},
    )
    status_host, _, host_content = _request(
        lab_server,
        "GET",
        "/api/scenarios",
        headers={"Host": f"outside.invalid:{port}"},
    )
    status_same, _, _ = _request(
        lab_server,
        "GET",
        "/api/scenarios",
        headers={"Host": f"{host}:{port}", "Origin": f"http://{host}:{port}"},
    )

    assert status_origin == 403
    assert _json(origin_content)["error"]["code"] == "ORIGIN_FORBIDDEN"
    assert status_host == 403
    assert _json(host_content)["error"]["code"] == "HOST_FORBIDDEN"
    assert status_same == 200


def test_origin_must_match_the_exact_accepted_host(lab_server: tuple[str, int]) -> None:
    host, port = lab_server
    status, _, content = _request(
        lab_server,
        "GET",
        "/api/scenarios",
        headers={"Host": f"{host}:{port}", "Origin": f"http://localhost:{port}"},
    )

    assert status == 403
    assert _json(content)["error"]["code"] == "ORIGIN_FORBIDDEN"


@pytest.mark.parametrize(
    ("headers", "code", "status"),
    [
        ({"Content-Type": "text/plain"}, "MEDIA_TYPE_UNSUPPORTED", 415),
        (
            {"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
            "TRANSFER_ENCODING_UNSUPPORTED",
            400,
        ),
    ],
)
def test_invalid_post_transport_is_rejected_before_body_read(
    lab_server: tuple[str, int], headers: dict[str, str], code: str, status: int
) -> None:
    actual_status, _, content = _request(
        lab_server,
        "POST",
        "/api/replay",
        body=b"{}",
        headers=headers,
    )

    assert actual_status == status
    assert _json(content)["error"]["code"] == code


def test_duplicate_content_type_cannot_smuggle_a_second_media_type(
    lab_server: tuple[str, int],
) -> None:
    body = b'{"scenario_id":"approved-update","source":"builtin"}'
    connection = socket.create_connection(lab_server, timeout=3)
    request = (
        "POST /api/replay HTTP/1.1\r\n"
        f"Host: {lab_server[0]}:{lab_server[1]}\r\n"
        "Content-Type: application/json\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body
    connection.sendall(request)
    response = b""
    while chunk := connection.recv(16_384):
        response += chunk
    connection.close()

    assert response.startswith(b"HTTP/1.1 415")
    assert b"MEDIA_TYPE_UNSUPPORTED" in response
    assert b'"report"' not in response


def test_missing_content_length_is_rejected_without_waiting_for_a_body(
    lab_server: tuple[str, int],
) -> None:
    connection = http.client.HTTPConnection(*lab_server, timeout=3)
    connection.putrequest("POST", "/api/replay")
    connection.putheader("Content-Type", "application/json")
    connection.endheaders()
    response = connection.getresponse()
    content = response.read()
    connection.close()

    assert response.status == 411
    assert _json(content)["error"]["code"] == "CONTENT_LENGTH_REQUIRED"


@pytest.mark.parametrize(
    "length_headers",
    [
        ["9" * 5_000],
        ["2", "2"],
    ],
)
def test_ambiguous_or_unreasonably_long_content_length_is_bounded(
    lab_server: tuple[str, int], length_headers: list[str]
) -> None:
    connection = socket.create_connection(lab_server, timeout=3)
    lines = [
        "POST /api/replay HTTP/1.1",
        f"Host: {lab_server[0]}:{lab_server[1]}",
        "Content-Type: application/json",
        *(f"Content-Length: {value}" for value in length_headers),
        "Connection: close",
        "",
        "",
    ]
    connection.sendall("\r\n".join(lines).encode("ascii"))
    response = b""
    while chunk := connection.recv(16_384):
        response += chunk
    connection.close()

    assert response.startswith(b"HTTP/1.1 400")
    assert b"CONTENT_LENGTH_INVALID" in response
    assert b"Traceback" not in response


def test_short_body_is_rejected_during_bounded_read(lab_server: tuple[str, int]) -> None:
    connection = socket.create_connection(lab_server, timeout=3)
    request = (
        "POST /api/replay HTTP/1.1\r\n"
        f"Host: {lab_server[0]}:{lab_server[1]}\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 12\r\n"
        "Connection: close\r\n\r\n{}"
    ).encode("ascii")
    connection.sendall(request)
    connection.shutdown(socket.SHUT_WR)
    response = b""
    while chunk := connection.recv(16_384):
        response += chunk
    connection.close()

    assert response.startswith(b"HTTP/1.1 400")
    assert b"REQUEST_TRUNCATED" in response
    assert b"Traceback" not in response


def test_stalled_body_read_has_a_bounded_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = tmp_path / "static"
    assets.mkdir()
    monkeypatch.setattr(server_module, "REQUEST_TIMEOUT_SECONDS", 0.05, raising=False)
    server = create_server("127.0.0.1", 0, asset_root=assets)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = cast(tuple[str, int], server.server_address)
    connection = socket.create_connection(address, timeout=1)
    try:
        request = (
            "POST /api/replay HTTP/1.1\r\n"
            f"Host: {address[0]}:{address[1]}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 2\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        connection.sendall(request)
        response = b""
        while chunk := connection.recv(16_384):
            response += chunk
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.startswith(b"HTTP/1.1 408")
    assert b"REQUEST_TIMEOUT" in response
    assert b"Traceback" not in response


def test_oversize_declared_body_is_rejected_without_reading_or_partial_result(
    lab_server: tuple[str, int],
) -> None:
    connection = socket.create_connection(lab_server, timeout=3)
    request = (
        "POST /api/replay HTTP/1.1\r\n"
        f"Host: {lab_server[0]}:{lab_server[1]}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {MAX_REQUEST_BYTES + 1}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    connection.sendall(request)
    response = b""
    while chunk := connection.recv(16_384):
        response += chunk
    connection.close()

    assert response.startswith(b"HTTP/1.1 413")
    assert b"REQUEST_TOO_LARGE" in response
    assert b'"report"' not in response
    assert b"Traceback" not in response


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS"])
def test_unapproved_methods_return_bounded_json(lab_server: tuple[str, int], method: str) -> None:
    status, headers, content = _request(lab_server, method, "/api/replay")

    assert status == 405
    assert _json(content) == {
        "error": {"code": "METHOD_NOT_ALLOWED", "message": "Method is not allowed."}
    }
    assert len(content) < 512
    _assert_security_headers(headers)


def test_unknown_http_method_is_closed_as_method_not_allowed(lab_server: tuple[str, int]) -> None:
    status, headers, content = _request(lab_server, "BREW", "/api/replay")

    assert status == 405
    assert _json(content)["error"]["code"] == "METHOD_NOT_ALLOWED"
    _assert_security_headers(headers)


def test_adapter_contract_errors_are_bounded_json_without_paths_or_tracebacks(
    lab_server: tuple[str, int],
) -> None:
    body = b'{"scenario_id":"approved-update","source":"upload","trace":{"schema_version":"1.0","events":[]}}'
    status, _, content = _request(
        lab_server,
        "POST",
        "/api/replay",
        body=body,
        headers={"Content-Type": "application/json"},
    )

    assert status == 422
    assert _json(content)["error"]["code"] == "TRACE_EMPTY"
    assert len(content) < 512
    assert b"/tmp/" not in content
    assert b"Traceback" not in content


def test_http_adapter_rejects_nested_unsafe_integers_with_a_bounded_error(
    lab_server: tuple[str, int],
) -> None:
    body = json.dumps(
        {
            "scenario_id": "approved-update",
            "source": "upload",
            "trace": {
                "schema_version": "1.0",
                "events": [
                    {
                        "schema_version": "1.0",
                        "case_id": "bounded-update",
                        "step": 1,
                        "kind": "result",
                        "status": "completed",
                        "output": {"nested": [{"value": 9_007_199_254_740_992}]},
                    }
                ],
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")

    status, _, content = _request(
        lab_server,
        "POST",
        "/api/replay",
        body=body,
        headers={"Content-Type": "application/json"},
    )

    assert status == 400
    assert _json(content)["error"] == {
        "code": "REQUEST_NUMBER_INVALID",
        "message": "Request integers must be within JavaScript's safe integer range.",
    }
    assert len(content) < 256
    assert b"Traceback" not in content
    assert str(ROOT).encode("utf-8") not in content


def test_server_refuses_non_loopback_bind_addresses() -> None:
    with pytest.raises(ValueError, match="loopback"):
        create_server("0.0.0.0", 4173)


def test_module_entrypoint_bounds_port_bind_failure() -> None:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "toolcall_replay_lab.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == (
        "ERROR START_FAILED: local trace laboratory could not bind to loopback\n"
    )
    assert len(completed.stderr.encode("utf-8")) <= 160
    assert "Traceback" not in completed.stderr
    assert str(ROOT) not in completed.stderr


def test_module_entrypoint_suppresses_client_reset_tracebacks() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "toolcall_replay_lab.server", "--port", "0"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stderr is not None
        startup = process.stderr.readline()
        port = int(startup.rstrip().rsplit(":", 1)[1])
        request = (
            f"GET /api/scenarios HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        for _ in range(3):
            with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
                connection.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, 0),
                )
                connection.sendall(request)
        response_request = (
            f"GET /app.js HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        for _ in range(3):
            with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
                connection.sendall(response_request)
                assert connection.recv(1)
                connection.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, 0),
                )
        partial_request = (
            "POST /api/replay HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 100\r\n"
            "Connection: close\r\n\r\n{"
        ).encode("ascii")
        for _ in range(3):
            with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
                connection.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, 0),
                )
                connection.sendall(partial_request)
        time.sleep(0.1)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 0
    assert stdout == ""
    assert stderr == ""


class _EmissionProbe:
    command = "GET"

    def __init__(self, failure_stage: str, failure_errno: int) -> None:
        self.failure_stage = failure_stage
        self.failure_errno = failure_errno
        self.close_connection = False
        self.wfile = self

    def _reach(self, stage: str) -> None:
        if stage == self.failure_stage:
            raise OSError(self.failure_errno, "injected response emission failure")

    def send_response(self, status: int) -> None:
        del status
        self._reach("status")

    def send_header(self, name: str, value: str) -> None:
        del name, value
        self._reach("header")

    def _security_headers(self, cache_control: str) -> None:
        del cache_control

    def end_headers(self) -> None:
        self._reach("header-end")

    def write(self, content: bytes) -> None:
        del content
        self._reach("body")


@pytest.mark.parametrize("stage", ["status", "header", "header-end", "body"])
@pytest.mark.parametrize(
    "disconnect_errno",
    [errno.ECONNABORTED, errno.ECONNRESET, errno.EPIPE, errno.ETIMEDOUT],
)
def test_complete_response_emission_suppresses_only_expected_disconnects(
    stage: str,
    disconnect_errno: int,
) -> None:
    probe = _EmissionProbe(stage, disconnect_errno)

    server_module._LabHandler._send(
        cast(Any, probe),
        200,
        "text/plain; charset=utf-8",
        b"response",
        "no-store",
    )

    assert probe.close_connection is True


@pytest.mark.parametrize("stage", ["status", "header", "header-end", "body"])
def test_complete_response_emission_does_not_mask_unexpected_os_errors(stage: str) -> None:
    probe = _EmissionProbe(stage, errno.EBADF)

    with pytest.raises(OSError) as caught:
        server_module._LabHandler._send(
            cast(Any, probe),
            200,
            "text/plain; charset=utf-8",
            b"response",
            "no-store",
        )

    assert caught.value.errno == errno.EBADF


def test_module_entrypoint_stops_cleanly_on_keyboard_interrupt() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "toolcall_replay_lab.server", "--port", "0"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stderr is not None
        startup = process.stderr.readline()
        assert startup.startswith("ToolCall Replay laboratory: http://127.0.0.1:")
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 0
    assert stdout == ""
    assert stderr == ""
