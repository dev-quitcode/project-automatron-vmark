"""Async network probes for Kamal preflight checks.

Each probe returns plain values (no exceptions). Failures are encoded in the
return type so the strategy can attach them to `PreflightCheck`s.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx


async def dns_resolves(domain: str) -> tuple[bool, list[str]]:
    """Resolve `domain` to A/AAAA records. Returns (ok, [addresses])."""
    if not domain:
        return False, []
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(domain, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, []
    addrs: list[str] = []
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0] if sockaddr else None
        if addr and addr not in addrs:
            addrs.append(str(addr))
    return bool(addrs), addrs


async def tcp_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    """Open a TCP connection to `host:port`. Returns True on success."""
    if not host:
        return False
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass
    _ = reader
    return True


async def ghcr_auth_probe(username: str, token: str) -> tuple[bool, int]:
    """Verify GHCR PAT can authenticate. Returns (ok, status_code).

    GHCR's `/token` endpoint accepts Basic auth and returns 200 + a bearer
    token JSON when credentials are valid.
    """
    if not username or not token:
        return False, 0
    url = "https://ghcr.io/token?service=ghcr.io&scope=repository:user/image:pull"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, auth=(username, token))
    except httpx.HTTPError:
        return False, 0
    return response.status_code == 200, response.status_code


async def runtime_health_probe(
    base_url: str,
    health_path: str,
    timeout: float = 10.0,
) -> tuple[bool, int]:
    """GET `base_url + health_path` and require a 2xx response."""
    if not base_url:
        return False, 0
    url = base_url.rstrip("/")
    if health_path:
        url = f"{url}{health_path if health_path.startswith('/') else '/' + health_path}"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return False, 0
    return 200 <= response.status_code < 300, response.status_code


def stub_probe_result(_: Any = None) -> tuple[bool, int]:  # pragma: no cover
    """Helper to keep type-narrowing simple in tests."""
    return False, 0
