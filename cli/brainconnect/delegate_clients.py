"""Thin HTTP client adapters for the two delegated engines (ADR 0008 Lane 4).

BrainConnect delegates ALL routing and placement math. These clients are the
network mouths that speak the *exact* shapes the two engines own — request in,
decision out — and contain **zero** routing/placement/scheduler logic of their
own. Per ADR 0008 the ranking, eligibility, residency, and queue math live in
AgentConnect (`RoutingEngine.route`) and ComputeConnect (`select_placement`);
BrainConnect never re-derives any of it.

Two contracts (from the Lane-4 recon):

* **AgentConnect capability router.** `RoutingEngine.route(ctx, status)` is a pure
  deterministic function returning a `RoutingDecision`. It is NOT surfaced as a
  clean "give me a decision" HTTP endpoint today (the router package is an MCP
  stdio server whose tools *execute* a generation, and the `agentconnect-api`
  HTTP surface exposes a different subtask-router shape). So BrainConnect binds to
  the faithful `RoutingContext -> RoutingDecision` contract through an
  **injectable** client: the HTTP implementation below POSTs a `RoutingContext`
  and expects a `RoutingDecision`, and the delegation trigger is smoked against an
  in-process fake honouring the same shape. When AgentConnect publishes a bare
  decision endpoint, only this file's URL/verb changes.

* **ComputeConnect placement estimate.** `POST /route/estimate` is a real,
  shipped endpoint. This client speaks its documented body and honours the
  `X-Privacy-Tier` header (which, by CC's `resolve_privacy_precedence`, can only
  *narrow* the body tier, never widen it).

Every transport failure — connection refused, timeout, non-2xx, unparseable body
— is raised as a single typed `DelegationClientError`. The trigger treats that
class as "engine unavailable" and falls back deterministically; it never crashes.

Network I/O is allowed here (it is a service call, not a model call). No API keys,
no model generation — the key-free, model-free CLI boundary is preserved.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol, runtime_checkable

#: Stable provider identifiers used in fallback reasons and provenance.
AGENTCONNECT = "agentconnect"
COMPUTECONNECT = "computeconnect"

#: Per-socket-operation timeout (connect / individual read).
DEFAULT_TIMEOUT = 5.0
#: Total wall-clock ceiling for one call, INCLUDING a slow-drip (slowloris) body
#: that keeps arriving just under the per-read timeout. Bounds the whole call so
#: a hostile server can never pin the trigger indefinitely.
DEFAULT_DEADLINE = 15.0
#: Hard cap on a response body. A hostile/oversized body is refused before it can
#: be fully buffered into memory (no OOM). 256 KiB dwarfs any real decision.
MAX_RESPONSE_BYTES = 256 * 1024


def _redact_url(url: str) -> str:
    """Strip any ``user:pass@`` userinfo from a URL so credentials embedded in a
    base URL are NEVER formatted into an error string or persisted to the DB via
    provenance. Host/port/path are preserved for diagnosability."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<url>"
    if "@" not in parts.netloc:
        return url
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class DelegationClientError(Exception):
    """A delegated engine could not be reached, or answered untransportably.

    Carries the `provider` so the trigger can record *which* engine was
    unavailable. This is the ONLY exception the HTTP clients raise for a
    reach/transport problem; the trigger catches it and falls back. It never
    escapes to crash the CLI.
    """

    def __init__(self, provider: str, message: str):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message


@runtime_checkable
class RoutingClient(Protocol):
    """Injectable AgentConnect capability-router client.

    `route` takes an assembled `RoutingContext` dict and returns a
    `RoutingDecision` dict. Any transport failure raises `DelegationClientError`.
    """

    def route(self, context: dict) -> dict: ...


@runtime_checkable
class EstimateClient(Protocol):
    """Injectable ComputeConnect placement-estimate client.

    `estimate` takes the `/route/estimate` body dict and an optional privacy
    header value, and returns the estimate dict. Any transport failure raises
    `DelegationClientError`.
    """

    def estimate(self, body: dict, *, privacy_header: str | None = None) -> dict: ...


# --- HTTP implementations ----------------------------------------------------
def _read_bounded(resp, *, provider: str, safe_url: str, max_bytes: int,
                  start: float, deadline: float) -> bytes:
    """Read a response body under BOTH a byte cap and a wall-clock deadline.

    Uses ``read1`` (one underlying socket read per call, returns as soon as any
    bytes arrive) so a slow-drip server that trickles bytes just under the
    per-read socket timeout still cannot outlast the wall-clock ``deadline`` — we
    re-check elapsed time between chunks. Exceeding either bound raises the single
    `DelegationClientError` class the trigger already treats as an outage."""
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() - start > deadline:
            raise DelegationClientError(
                provider, f"deadline exceeded ({deadline:g}s) reading {safe_url}")
        try:
            chunk = resp.read1(65536)
        except (TimeoutError, socket.timeout) as e:
            raise DelegationClientError(
                provider, f"read timed out from {safe_url}") from e
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise DelegationClientError(
                provider,
                f"response body exceeded {max_bytes} bytes from {safe_url}")
        chunks.append(chunk)
    return b"".join(chunks)


def _post_json(url: str, payload: dict, *, provider: str, token: str | None,
               timeout: float, extra_headers: dict | None = None,
               deadline: float = DEFAULT_DEADLINE,
               max_bytes: int = MAX_RESPONSE_BYTES) -> dict:
    """POST JSON, return the parsed JSON object. Raise `DelegationClientError` on
    any transport/HTTP/decode/oversize/deadline failure (the whole point: one
    failure class). Never emits URL userinfo (credentials) in its messages."""
    safe_url = _redact_url(url)
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = _read_bounded(
                resp, provider=provider, safe_url=safe_url, max_bytes=max_bytes,
                start=start, deadline=deadline)
    except DelegationClientError:
        raise
    except urllib.error.HTTPError as e:
        raise DelegationClientError(provider, f"HTTP {e.code} from {safe_url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DelegationClientError(
            provider, f"unreachable {safe_url}: {type(e).__name__}") from e
    raw = raw_bytes.decode("utf-8", "ignore")
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        raise DelegationClientError(provider, f"non-JSON body from {safe_url}") from e
    if not isinstance(obj, dict):
        raise DelegationClientError(provider, f"non-object body from {safe_url}")
    return obj


class HttpRoutingClient:
    """POSTs a `RoutingContext` to an AgentConnect decision endpoint.

    The URL is injected because AgentConnect does not yet publish a bare
    decision endpoint (see module docstring); the *shape* is fixed. No routing
    math lives here — the request is forwarded verbatim and the decision returned
    verbatim.
    """

    def __init__(self, base_url: str, *, token: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT, path: str = "/route/decide",
                 deadline: float = DEFAULT_DEADLINE,
                 max_bytes: int = MAX_RESPONSE_BYTES):
        self._url = base_url.rstrip("/") + path
        self._token = token
        self._timeout = timeout
        self._deadline = deadline
        self._max_bytes = max_bytes

    def route(self, context: dict) -> dict:
        return _post_json(self._url, context, provider=AGENTCONNECT,
                          token=self._token, timeout=self._timeout,
                          deadline=self._deadline, max_bytes=self._max_bytes)


class HttpEstimateClient:
    """POSTs to ComputeConnect `POST /route/estimate` with the `X-Privacy-Tier`
    header. Real, shipped endpoint. No placement math here — body forwarded,
    estimate returned."""

    def __init__(self, base_url: str, *, token: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT, path: str = "/route/estimate",
                 deadline: float = DEFAULT_DEADLINE,
                 max_bytes: int = MAX_RESPONSE_BYTES):
        self._url = base_url.rstrip("/") + path
        self._token = token
        self._timeout = timeout
        self._deadline = deadline
        self._max_bytes = max_bytes

    def estimate(self, body: dict, *, privacy_header: str | None = None) -> dict:
        extra = {"X-Privacy-Tier": privacy_header} if privacy_header else None
        return _post_json(self._url, body, provider=COMPUTECONNECT,
                          token=self._token, timeout=self._timeout,
                          extra_headers=extra, deadline=self._deadline,
                          max_bytes=self._max_bytes)
