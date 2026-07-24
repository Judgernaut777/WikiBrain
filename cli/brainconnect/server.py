"""`brainconnect serve` — the HTTP transport onto the trusted memory ledger.

This is the network face AgentConnect's ``WikiBrainMemoryAdapter`` binds to
(default ``127.0.0.1:8787``). It serves exactly the routes that adapter calls,
and nothing else:

    POST /recall                     -> RecallPack            (api.recall)
    POST /capture                    -> CaptureResult         (api.capture_candidate)
    POST /candidates/{id}/promote    -> promoted claim        (api.promote)
    GET  /candidates?status=&limit=  -> {"count", "candidates"}  (api.pending)
    POST /feedback                   -> {"recorded": true}    (api.record_feedback)
    GET  /registry                   -> trusted capability claims (registry.trusted_view)
    GET  /registry/capabilities      -> alias of GET /registry
    GET  /health                     -> api.health

The ``/registry`` route is the ADR-0008 Lane-3 transport (docs/REGISTRY.md §6): a
READ-ONLY view of the capability registry restricted to TRUSTED, human-promoted
claims, which AgentConnect's ``RoutingEngine`` PULLS to weight a trusted source in
place of its self-conferred ``learned_quality``. BC serves only trusted claims; how
AC weights them lives in the AgentConnect repo, not here. It is bearer-authed like
every other non-health route, serves no pending/squatted fact as trusted, holds no
live state, and mutates nothing — a POST/PUT to it is rejected by the method
handling below. It needs zero models loaded (it reads the ledger, never ``:8080``).

Everything of consequence is a property of the API layer, not of this file:

* **Refusals use the canonical nested envelope.** Every failure is mapped with
  ``errors.classify`` / ``errors.http_status`` and answered with
  ``errors.envelope(exc)`` — ``{"error": {"code", "message", "retryable",
  "safety"?}}``. The taxonomy is never re-derived here (docs/CONTRACT.md).
* **Promotion stays human-only.** The HTTP surface does NOT accept a safety
  override: a payload carrying ``safety_override`` or ``override_reason`` is
  refused with ``forbidden``, and there is no other field that reaches the
  override. Overriding happens at the CLI, by a human, with a reason.
  NOTE: ``reviewer_type`` itself IS still a caller-supplied payload field
  here (see ``_promote`` / ``_PROMOTE_FIELDS`` below) — a bearer-token
  holder can declare ``"human"`` without being one. Documented, not yet
  closed: see docs/adr/0009-http-trust-boundary-honor-system.md (Finding A).
* **Optional bearer-token auth.** When a token is configured (``--token`` or
  ``BRAINCONNECT_TOKEN``), every route except ``GET /health`` requires an
  ``Authorization`` header carrying it (``Bearer <token>`` or the bare token,
  compared constant-time). A missing or wrong credential is ``forbidden`` —
  which AgentConnect's adapter surfaces as ``MemoryAuthorizationError``, the
  "never retry with the same credential" class.

  **Tokenless serve is UNAUTHENTICATED and MUST stay on loopback.** With no token
  configured, auth is off and EVERY non-health route is open to any caller that
  can reach the socket — including ``GET /registry``, which then publishes the
  trusted capability claims with no credential check at all. The default bind is
  ``127.0.0.1`` for exactly this reason: a tokenless ``brainconnect serve`` must
  never be bound to a non-loopback address or otherwise exposed beyond
  ``127.0.0.1``. Configure ``BRAINCONNECT_TOKEN`` before binding anywhere else.
  (The auth default is intentionally left as-is here — consistent with every
  other route — and is a deployment obligation, not a code toggle.)
* **Zero model calls.** Pure stdlib: ``http.server`` + a fresh ``Repo`` per
  request (WAL-safe, same pattern as the MCP server), so no third-party web
  framework enters the trust boundary.

The live database rule applies with full force here: the served DB is whatever
the resolved config (or ``BRAINCONNECT_DB``) points at. Point a test server at
a scratch DB, never at the live one.

**Startup does not silently migrate.** ``build_server`` opens with
``db.open_for_server`` (docs/MIGRATIONS.md): a behind-schema DB refuses to
start with a clear ``SchemaBehindError`` telling the operator to run
``brainconnect migrate``, unless ``auto_migrate=True`` (``--auto-migrate`` /
``BRAINCONNECT_AUTO_MIGRATE=1``) was opted into. Every per-request ``Repo.open``
thereafter passes ``migrate=False`` — the startup check already ran, and a
request handler must never trigger a migration.
"""
from __future__ import annotations

import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import api, candidates, errors, registry
from .db import Repo, open_for_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
TOKEN_ENV_VAR = "BRAINCONNECT_TOKEN"

#: Larger than any legitimate capture (safety caps text at 200k chars) but small
#: enough that a runaway client cannot balloon the process.
MAX_BODY_BYTES = 2_000_000

_PROMOTE_PATH = re.compile(r"^/candidates/([^/]+)/promote$")

#: Fields a promote payload may carry. `safety_override` / `override_reason` are
#: deliberately NOT here — see `_promote`.
_PROMOTE_FIELDS = {"promoted_by", "confidence", "scope", "note", "reviewer_type"}


def _drop_nones(payload: dict) -> dict:
    """Adapter clients send explicit nulls for unset optionals; treat them as
    absent so dataclass defaults apply instead of `None` leaking into fields
    that expect a string or a list."""
    return {k: v for k, v in payload.items() if v is not None}


# --- route handlers (repo in, JSON-able dict out; refuse by raising) ----------

def _recall(repo: Repo, payload: dict) -> dict:
    return api.recall(repo, _drop_nones(payload)).as_dict()


def _capture(repo: Repo, payload: dict) -> dict:
    payload = _drop_nones(payload)
    if not str(payload.get("text", "")).strip():
        raise api.ApiError("capture requires non-empty text")
    if not str(payload.get("origin_actor_id", payload.get("proposed_by", ""))).strip():
        raise api.ApiError(
            "capture requires origin_actor_id (or proposed_by): the ledger records "
            "who proposed a claim, and it may not be guessed")
    return api.capture_candidate(repo, payload).as_dict()


def _promote(repo: Repo, candidate_id: str, payload: dict) -> dict:
    # NOTE (docs/adr/0009-http-trust-boundary-honor-system.md, Finding A):
    # `reviewer_type` below is taken from the caller's payload and passed
    # straight to `candidates.promote`. Its `ReviewerNotPermitted` check is
    # structural on the CLI/Python path (actor type set by trusted local
    # context) but NOT over HTTP, where any bearer-token holder can declare
    # `"human"`. Documented, not enforced further, in this pass.
    payload = _drop_nones(payload)
    # The override is human-only and CLI-only (docs/CONTRACT.md). Refusing it as
    # `forbidden` — not `invalid_request` — is deliberate: an agent told
    # "invalid" fixes its payload and knocks again; an agent told "forbidden"
    # learns the override is not available to it at all.
    if payload.get("safety_override") or payload.get("override_reason"):
        raise candidates.ReviewerNotPermitted(
            "the HTTP surface does not accept a safety override; overriding a "
            "safety refusal is human-only, at the CLI "
            "(`brainconnect promote --safety-override --override-reason ...`)")
    unknown = set(payload) - _PROMOTE_FIELDS
    if unknown:
        raise api.ApiError(f"unknown promote fields: {', '.join(sorted(unknown))}")
    promoted_by = str(payload.get("promoted_by", "")).strip()
    if not promoted_by:
        raise api.ApiError("promote requires promoted_by")
    confidence = payload.get("confidence")
    if confidence is None:
        raise api.ApiError(
            "promote requires confidence (low|medium|high|verified): the ledger "
            "never guesses it, because profiles filter on it")
    result = api.promote(
        repo, candidate_id, reviewer=promoted_by, confidence=confidence,
        scope=payload.get("scope"),
        reviewer_type=str(payload.get("reviewer_type", "human")),
        note=payload.get("note"))
    # AgentConnect's adapter names the promoted claim `claim_id`; ours is `id`.
    # Echo both so neither consumer has to translate.
    result.setdefault("claim_id", result.get("id"))
    return result


def _candidates(repo: Repo, query: dict) -> dict:
    status = (query.get("status", ["pending"])[0] or "pending").strip()
    raw_limit = (query.get("limit", ["50"])[0] or "50").strip()
    try:
        limit = int(raw_limit)
    except ValueError:
        raise api.ApiError(f"limit must be an integer, got {raw_limit!r}") from None
    if limit < 1:
        raise api.ApiError("limit must be >= 1")
    rows = candidates.listing(repo, status=status, limit=min(limit, 500))
    return {"count": len(rows), "candidates": rows}


def _registry(repo: Repo) -> dict:
    """The trusted-only capability registry view (ADR 0008 Lane 3).

    Pure serialization of `registry.trusted_view`: all trust resolution — the
    unforgeable registry marker, promoted-claim status, the pending/squatter
    exclusion — lives in registry.py. This handler reads nothing else and mutates
    nothing; it is reached only via `do_GET`, so it is structurally read-only.
    """
    return registry.trusted_view(repo)


def _feedback(repo: Repo, payload: dict) -> dict:
    payload = _drop_nones(payload)
    # AgentConnect speaks `memory_item_id`; the ledger speaks `claim_id`. Same
    # field — accept both, refuse a contradiction rather than guessing.
    if "memory_item_id" in payload:
        value = payload.pop("memory_item_id")
        if payload.get("claim_id") not in (None, "", value):
            raise api.ApiError(
                "conflicting feedback fields 'memory_item_id' and 'claim_id'")
        payload.setdefault("claim_id", value)
    if not str(payload.get("feedback", "")).strip():
        raise api.ApiError("feedback requires a feedback value")
    if not str(payload.get("actor_id", "")).strip():
        raise api.ApiError("feedback requires actor_id")
    api.record_feedback(repo, payload)
    return {"recorded": True}


# --- the HTTP shell -----------------------------------------------------------

class BrainConnectServer(ThreadingHTTPServer):
    """Carries the resolved repo root + auth token down to request handlers."""

    daemon_threads = True

    def __init__(self, address, root, token: str | None):
        self.repo_root = root
        self.token = (token or "").strip() or None
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: BrainConnectServer  # for the type checker; set by http.server

    # -- plumbing --------------------------------------------------------------
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":  # a HEAD response carries headers only
            self.wfile.write(data)

    def _refuse(self, exc: BaseException) -> None:
        self._send(errors.http_status(exc), errors.envelope(exc))

    def _authorized(self) -> bool:
        token = self.server.token
        if token is None:
            return True
        supplied = (self.headers.get("Authorization") or "").strip()
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        return bool(supplied) and hmac.compare_digest(supplied, token)

    def _body(self) -> dict:
        length_raw = self.headers.get("Content-Length")
        try:
            length = int(length_raw or "")
        except ValueError:
            raise api.ApiError("request requires a Content-Length header") from None
        if length > MAX_BODY_BYTES:
            # Refused before reading, and draining this much is unsafe — so the
            # refusal must close the connection instead of leaving the unread
            # body to be misread as the next request line (see _drain_body).
            self.close_connection = True
            raise api.ApiError(
                f"request body exceeds {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            raise api.ApiError("request body is not valid JSON") from None
        if not isinstance(payload, dict):
            raise api.ApiError("request body must be a JSON object")
        return payload

    def _drain_body(self) -> None:
        """Read and discard a body the client may have sent, so an HTTP/1.1
        keep-alive connection stays parseable for the next request. Required
        before answering any refusal issued WITHOUT reading the body (auth,
        route miss, unknown method): the unread bytes would otherwise be
        parsed as the next request line on the same connection."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= MAX_BODY_BYTES:
            self.rfile.read(length)

    def _not_found(self, path: str) -> None:
        # Synthesized from the same vocabulary `errors.envelope` uses; a route
        # miss has no exception to classify but must speak the same envelope.
        code = errors.NOT_FOUND
        self._send(errors.HTTP_STATUS[code], {"error": {
            "code": code,
            "message": f"no such route: {path}",
            "retryable": errors.RETRYABLE[code],
        }})

    def _require_authorized(self) -> None:
        """Raise the canonical `forbidden` refusal unless the bearer check passes.
        Shared so the 403 message never drifts between the pre-parse gate in
        `do_POST` and `_dispatch`."""
        if not self._authorized():
            raise candidates.ReviewerNotPermitted(
                "this server requires a bearer token (Authorization header); "
                "the credential supplied was missing or wrong")

    def _dispatch(self, handler) -> None:
        """Auth, execute, map every refusal to the canonical envelope."""
        try:
            self._require_authorized()
            # migrate=False: the startup check in build_server already confirmed
            # (or brought current) the schema; a request must never migrate it.
            with Repo.open(self.server.repo_root,
                           write_projections=False, migrate=False) as repo:
                result = handler(repo)
            self._send(200, result)
        except Exception as exc:  # noqa: BLE001 — every failure must wear the envelope
            self._refuse(exc)

    def log_message(self, fmt, *args):  # stderr, one line, no hostname lookups
        import sys
        sys.stderr.write(f"brainconnect serve: {fmt % args}\n")

    def _method_not_allowed(self) -> None:
        """Answer any HTTP method this server does not implement with the
        canonical envelope — never the stdlib's HTML 501 page. `invalid_request`
        is the honest code from the existing taxonomy: the caller must send a
        different request (GET or POST), and retrying the same one is useless.
        """
        self._drain_body()
        code = errors.INVALID_REQUEST
        self._send(errors.HTTP_STATUS[code], {"error": {
            "code": code,
            "message": (f"unsupported method {self.command} for "
                        f"{urlsplit(self.path).path}; this server speaks GET "
                        "and POST only (docs/CONTRACT.md)"),
            "retryable": errors.RETRYABLE[code],
        }})

    def __getattr__(self, name: str):
        # http.server dispatches each request to `do_<METHOD>` and answers a
        # missing one with its built-in HTML 501 page. Every verb we do not
        # implement must wear the JSON envelope instead. (`__getattr__` fires
        # only when normal lookup fails, so do_GET / do_POST are untouched.)
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    # -- routes ----------------------------------------------------------------
    def do_GET(self):  # noqa: N802 — http.server API
        url = urlsplit(self.path)
        if url.path == "/health":
            # Liveness stays reachable without a credential: a probe that cannot
            # ask "are you degraded?" invents the answer from refusals instead.
            # migrate=False for the same reason as `_dispatch`: a health probe
            # must never be the thing that triggers a schema migration.
            try:
                with Repo.open(self.server.repo_root, migrate=False) as repo:
                    self._send(200, api.health(repo))
            except Exception as exc:  # noqa: BLE001
                self._refuse(exc)
            return
        if url.path == "/candidates":
            query = parse_qs(url.query)
            self._dispatch(lambda repo: _candidates(repo, query))
            return
        if url.path in ("/registry", "/registry/capabilities"):
            # Trusted-only capability claims for AgentConnect to pull (ADR 0008
            # Lane 3). Authed by `_dispatch` exactly like every other route; a
            # POST/PUT never reaches here, so the surface is read-only.
            self._dispatch(_registry)
            return
        self._not_found(url.path)

    def do_POST(self):  # noqa: N802 — http.server API
        url = urlsplit(self.path)
        promote = _PROMOTE_PATH.match(url.path)
        try:
            # Fail closed first: authenticate from the headers BEFORE the request
            # body is read or parsed, so an unauthenticated caller's bytes never
            # reach the JSON parser (POST has no open route — every path here is
            # credentialed; ToolConnect likewise authenticates before parsing).
            # A refused body is still drained — read and discarded, never
            # parsed — so the 403 does not corrupt a keep-alive connection.
            try:
                self._require_authorized()
            except candidates.ReviewerNotPermitted:
                self._drain_body()
                raise
            if url.path == "/recall":
                body = self._body()
                self._dispatch(lambda repo: _recall(repo, body))
            elif url.path == "/capture":
                body = self._body()
                self._dispatch(lambda repo: _capture(repo, body))
            elif url.path == "/feedback":
                body = self._body()
                self._dispatch(lambda repo: _feedback(repo, body))
            elif promote:
                body = self._body()
                cid = promote.group(1)
                self._dispatch(lambda repo: _promote(repo, cid, body))
            else:
                self._drain_body()  # unrouted body: never parsed, still drained
                self._not_found(url.path)
        except Exception as exc:  # noqa: BLE001 — a malformed body must still wear the envelope
            self._refuse(exc)


def build_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *,
                 token: str | None = None, root=None,
                 auto_migrate: bool | None = None) -> BrainConnectServer:
    """Bind and return the server without blocking (port 0 = ephemeral, for
    tests). Resolves the repo root once at launch, exactly like the MCP server,
    so requests are immune to the client's cwd.

    Opens via `db.open_for_server`: a behind-schema DB refuses to start
    (`SchemaBehindError`) unless `auto_migrate=True` or
    `BRAINCONNECT_AUTO_MIGRATE=1` is set — see the module docstring.
    """
    with open_for_server(root, auto_migrate=auto_migrate) as probe:
        resolved_root = probe.root
    return BrainConnectServer((host, port), resolved_root, token)


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *,
          token: str | None = None, root=None,
          auto_migrate: bool | None = None) -> None:
    """Run the HTTP server until interrupted."""
    httpd = build_server(host, port, token=token, root=root, auto_migrate=auto_migrate)
    bound = httpd.server_address
    mode = "token-required" if httpd.token else "open (no token configured)"
    print(f"brainconnect serve: listening on http://{bound[0]}:{bound[1]} [{mode}]")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
