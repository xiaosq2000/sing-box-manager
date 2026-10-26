"""Private loopback HTTPS handler that calls the real PostgreSQL ingestion service.

This process exists only for the counter-export composition test. Its bearer
check is fixture authentication: a fixed synthetic token stands in for a node
credential that no production route or resolver has. The handler always uses the
same synthetic node UUID after that check. It adds no production route, bearer
resolver, registration or binding behavior.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

from sing_box_manager.control_plane import traffic_contract as tc

TRAFFIC_PATH = "/node/v1/traffic"
FIXTURE_TOKEN = os.environ["SBM_COUNTER_FIXTURE_TOKEN"]
FIXTURE_NODE_ID = UUID(os.environ["SBM_COUNTER_FIXTURE_NODE_ID"])
MODE = os.environ.get("SBM_COUNTER_FIXTURE_MODE", "normal")
BODY_DIR = Path(os.environ["SBM_COUNTER_FIXTURE_BODY_DIR"])
EVIDENCE = Path(os.environ["SBM_COUNTER_FIXTURE_EVIDENCE"])
EVIDENCE_LOCK = threading.Lock()
INGEST_PAGE = None
TRAFFIC_INGEST_ERROR = None


def private_write(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def append_event(event: dict[str, Any]) -> None:
    encoded = json.dumps(event, sort_keys=True)
    with EVIDENCE_LOCK, EVIDENCE.open("a", encoding="utf-8") as stream:
        stream.write(encoded + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def snapshot_rows() -> list[dict[str, Any]]:
    from sing_box_manager.control_plane.models import TrafficSnapshot

    rows = []
    for row in TrafficSnapshot.objects.order_by(
        "source_id", "credential_id", "kind", "accounting_date"
    ):
        payload = bytes(row.payload)
        rows.append(
            {
                "source_id": str(row.source_id),
                "credential_id": str(row.credential_id),
                "kind": row.kind,
                "accounting_date": row.accounting_date.isoformat(),
                "version": row.version,
                "digest": row.digest,
                "upload": row.upload,
                "download": row.download,
                "detail": row.detail,
                "payload_hex": payload.hex(),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "updated_at": row.updated_at.isoformat(),
            }
        )
    return rows


def receipt_count(raw: bytes) -> int | None:
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        return None
    receipts = document.get("receipts") if isinstance(document, dict) else None
    return len(receipts) if isinstance(receipts, list) else None


def tampered_response(raw: bytes) -> bytes:
    document = json.loads(raw)
    document["receipts"] = document["receipts"][:-1]
    return tc.canonical_json_bytes(document)


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    request_index = 0

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_json(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler hook
        type(self).request_index += 1
        request_index = type(self).request_index
        if self.path != TRAFFIC_PATH:
            self.send_json(404, b'{"error":"not_found"}')
            return
        length_text = self.headers.get("Content-Length", "")
        try:
            length = int(length_text)
        except ValueError:
            self.send_json(400, b'{"error":"invalid_length"}')
            return
        if length < 1 or length > tc.MAX_RAW_BODY_BYTES:
            self.send_json(413, b'{"error":"invalid_length"}')
            return
        raw = self.rfile.read(length)
        private_write(BODY_DIR / f"request-{request_index:03d}.json", raw)
        event: dict[str, Any] = {
            "request": request_index,
            "authorization_ok": False,
            "committed": False,
            "status": None,
            "receipt_count": None,
            "response_file": None,
        }
        expected = f"Bearer {FIXTURE_TOKEN}"
        if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            event["status"] = 401
            event["rows"] = snapshot_rows()
            append_event(event)
            self.send_json(401, b'{"error":"unauthorized"}')
            return

        event["authorization_ok"] = True
        if MODE == "error_status" and request_index == 1:
            event["status"] = 500
            event["rows"] = snapshot_rows()
            append_event(event)
            self.send_json(500, b'{"error":"fixture_failure"}')
            return

        try:
            receipt_bytes = INGEST_PAGE(FIXTURE_NODE_ID, raw)
        except TRAFFIC_INGEST_ERROR as exc:
            event["status"] = 409
            event["error"] = exc.code.value
            event["rows"] = snapshot_rows()
            append_event(event)
            self.send_json(409, json.dumps({"error": exc.code.value}).encode())
            return
        except Exception as exc:  # noqa: BLE001 - report a fixed class, never data
            event["status"] = 500
            event["error"] = type(exc).__name__
            event["rows"] = snapshot_rows()
            append_event(event)
            self.send_json(500, b'{"error":"fixture_exception"}')
            return

        event["committed"] = True
        event["receipt_count"] = receipt_count(receipt_bytes)
        event["rows"] = snapshot_rows()
        if MODE == "lost_reply" and request_index == 1:
            event["status"] = None
            append_event(event)
            self.close_connection = True
            return
        if MODE == "truncated" and request_index == 1:
            response_bytes = b'{"schema":1,"receipts":'
        elif MODE == "wrong_coverage" and request_index == 1:
            response_bytes = tampered_response(receipt_bytes)
        else:
            response_bytes = receipt_bytes
        event["status"] = 200
        event["receipt_count"] = receipt_count(response_bytes)
        response_name = f"response-{request_index:03d}.json"
        private_write(BODY_DIR / response_name, response_bytes)
        event["response_file"] = response_name
        append_event(event)
        self.send_json(200, response_bytes)


def main() -> None:
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "sing_box_manager.control_plane.django_settings"
    )
    import django

    django.setup()
    global INGEST_PAGE, TRAFFIC_INGEST_ERROR
    from sing_box_manager.control_plane.traffic_ingest import (
        TrafficIngestError,
        ingest_page,
    )

    INGEST_PAGE = ingest_page
    TRAFFIC_INGEST_ERROR = TrafficIngestError
    BODY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    EVIDENCE.touch(mode=0o600, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    httpd.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(
        certfile=os.environ["SBM_COUNTER_FIXTURE_CERT"],
        keyfile=os.environ["SBM_COUNTER_FIXTURE_KEY"],
    )
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    print(json.dumps({"port": httpd.server_address[1]}), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
