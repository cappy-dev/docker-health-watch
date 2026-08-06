#!/usr/bin/env python3
"""
docker_health_watch.py - Watch Docker container health and alert on changes.

A small, dependency-light monitor that polls the Docker Engine API at a steady
interval, watches every container that declares a HEALTHCHECK, and fires a
webhook the moment any container flips to (or recovers from) an unhealthy state.

No Docker SDK required. Talks to the local Unix socket with the standard library
only, so it runs happily inside a slim container alongside your other services.

Usage:
    python3 docker_health_watch.py \\
        --webhook https://hooks.example.com/health \\
        --interval 30

Environment variables (optional, override flags for 12-factor deployments):
    DWH_WEBHOOK      webhook URL to POST JSON alerts to
    DWH_INTERVAL     poll interval in seconds (default 15)
    DWH_SOCKET       path to the Docker socket (default /var/run/docker.sock)
    DWH_HOSTNAME     pretty label for this host in alerts
    DWH_TIMEOUT      per-request socket timeout in seconds (default 10)

Exit codes:
    0  ran once successfully (--once)
    1  unrecoverable startup error
    2  alert delivery failed for a container (does not stop the loop)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from http.client import HTTPConnection
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Docker socket client
# ---------------------------------------------------------------------------

class DockerClient:
    """Tiny synchronous client over the Docker Engine Unix socket."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def _request(self, method: str, path: str) -> Any:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
            sock.sendall(
                f"{method} {path} HTTP/1.1\r\n"
                "Host: docker\r\n"
                "Connection: close\r\n\r\n".encode("ascii")
            )
            chunks = bytearray()
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.extend(data)
        finally:
            sock.close()

        raw = bytes(chunks)
        sep = raw.find(b"\r\n\r\n")
        if sep < 0:
            raise RuntimeError("malformed response from Docker socket")
        body = raw[sep + 4:]
        # Docker streams chunked encoding for list endpoints in some versions.
        if b"Transfer-Encoding: chunked" in raw[:sep]:
            body = _dechunk(body)
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"could not parse Docker response: {exc}") from exc

    def list_containers(self) -> Iterable[Dict[str, Any]]:
        return self._request("GET", "/containers/json?all=true")

    def inspect(self, container_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/containers/{container_id}/json")


def _dechunk(body: bytes) -> bytes:
    """Decode HTTP chunked-transfer-encoding bytes into a single buffer."""
    out = bytearray()
    i = 0
    while i < len(body):
        nl = body.find(b"\r\n", i)
        if nl < 0:
            break
        try:
            size = int(body[i:nl].split(b";")[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        start = nl + 2
        out.extend(body[start:start + size])
        i = start + size + 2
    return bytes(out)


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

class State:
    """Remembers per-container health so we only alert on actual transitions."""

    def __init__(self) -> None:
        self._seen: Dict[str, str] = {}

    def transition(self, container_id: str, new_state: str) -> Optional[str]:
        old = self._seen.get(container_id)
        self._seen[container_id] = new_state
        if old is None:
            # First time we spot a container: only react if it starts unhealthy.
            return new_state if new_state == "unhealthy" else None
        if old == new_state:
            return None
        return new_state

    def forget(self, container_id: str) -> None:
        self._seen.pop(container_id, None)


# ---------------------------------------------------------------------------
# Alert delivery
# ---------------------------------------------------------------------------

def send_webhook(url: str, payload: Dict[str, Any], timeout: float) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported webhook scheme: {parsed.scheme}")
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    body = json.dumps(payload).encode("utf-8")
    conn_cls = __import__("ssl").create_default_context()  # placeholder for clarity
    if parsed.scheme == "https":
        import ssl as _ssl
        context = _ssl.create_default_context()
        conn_cls = HTTPConnection(host, port, timeout=timeout)
        # Wrap with TLS. Keep simple: use HTTPSConnection for https.
        from http.client import HTTPSConnection
        conn = HTTPSConnection(host, port, timeout=timeout, context=context)
    else:
        conn = HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        resp = conn.getresponse()
        if resp.status >= 400:
            raise RuntimeError(f"webhook returned HTTP {resp.status}")
        resp.read()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def current_health(container: Dict[str, Any]) -> Optional[str]:
    """Read the (recent) health from a /containers/json item."""
    state = (container.get("State") or "")
    status = (container.get("Status") or "")
    # Inspect-only fallbacks for older API versions.
    if isinstance(state, dict):
        health = (state.get("Health") or {}).get("Status")
        return health
    if status.startswith("Up "):
        rest = status[3:]
        if rest.startswith("unhealthy"):
            return "unhealthy"
        if rest.startswith("healthy"):
            return "healthy"
    # No health info means the container has no HEALTHCHECK.
    return None


def build_alert(host: str, container: Dict[str, Any], health: str) -> Dict[str, Any]:
    return {
        "source": "docker-health-watch",
        "host": host,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "container": {
            "id": container.get("Id", ""),
            "name": (container.get("Names") or [""])[0].lstrip("/"),
            "image": container.get("Image", ""),
            "state": health,
        },
        "message": f"Container {(container.get('Names') or ['?'])[0].lstrip('/')} is now {health}",
    }


def poll_once(client: DockerClient, state: State, host: str, webhook: Optional[str], timeout: float) -> int:
    issues = 0
    try:
        containers = client.list_containers()
    except Exception as exc:  # pragma: no cover - operational
        sys.stderr.write(f"[dwh] could not list containers: {exc}\n")
        return issues
    seen_ids = set()
    for container in containers:
        cid = container.get("Id")
        if not cid:
            continue
        seen_ids.add(cid)
        health = current_health(container)
        if health is None:
            # Inspect for an authoritative healthcheck status when the list view
            # was ambiguous (older API or running-but-no-status-prefix cases).
            try:
                detail = client.inspect(cid)
                h = (
                    (detail.get("State") or {}).get("Health") or {}
                ).get("Status")
                if h:
                    health = h
            except Exception:
                # Without a HEALTHCHECK there is nothing to watch here.
                continue
        if health is None:
            continue
        transitioned = state.transition(cid, health)
        if transitioned is None:
            continue
        alerts = build_alert(host, container, transitioned)
        line = f"[dwh] {alerts['container']['name']}: {transitioned}"
        print(line)
        if webhook:
            try:
                send_webhook(webhook, alerts, timeout)
            except Exception as exc:
                issues = 2
                sys.stderr.write(f"[dwh] webhook delivery failed: {exc}\n")
    # Clean up memory for containers that were removed since last poll.
    for gone in set(state._seen) - seen_ids:
        state.forget(gone)
    return issues


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch Docker container health and alert on changes.",
    )
    parser.add_argument(
        "--webhook",
        default=env("DWH_WEBHOOK"),
        help="webhook URL to POST JSON alerts to (env: DWH_WEBHOOK)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(env("DWH_INTERVAL", "15") or "15"),
        help="poll interval in seconds (env: DWH_INTERVAL)",
    )
    parser.add_argument(
        "--socket",
        default=env("DWH_SOCKET", "/var/run/docker.sock"),
        help="path to the Docker socket (env: DWH_SOCKET)",
    )
    parser.add_argument(
        "--hostname",
        default=env("DWH_HOSTNAME") or socket.gethostname(),
        help="pretty label for this host in alerts (env: DWH_HOSTNAME)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(env("DWH_TIMEOUT", "10") or "10"),
        help="per-request socket timeout in seconds (env: DWH_TIMEOUT)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="poll a single time and exit (useful for cron wrappers)",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.socket):
        sys.stderr.write(f"[dwh] docker socket not found: {args.socket}\n")
        sys.stderr.write("[dwh] mount the socket or set DWH_SOCKET\n")
        return 1

    client = DockerClient(args.socket, args.timeout)
    state = State()

    if args.once:
        return poll_once(client, state, args.hostname, args.webhook, args.timeout)

    print(f"[dwh] watching {args.socket} every {args.interval:g}s (host={args.hostname})")
    while True:
        poll_once(client, state, args.hostname, args.webhook, args.timeout)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
