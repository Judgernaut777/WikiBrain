"""Minimal OpenAI-compatible chat client (stdlib only, no SDK dependency).

Works against any /v1/chat/completions endpoint: Ollama, LM Studio, llama.cpp
server, OpenRouter, OpenAI, Anthropic's compat endpoint, vLLM, ... The model
name and endpoint come from `[librarian]` config; the key (if any) from the
environment. Transport is a module function so tests can stub it offline.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from .config import LibrarianConfig

UA = "wiki-brain-librarian/0.1"


class ModelCallError(Exception):
    pass


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    """POST JSON, return parsed JSON. Stubbed in tests; raises ModelCallError."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:500]
        except Exception:
            pass
        raise ModelCallError(f"HTTP {e.code} from {url}: {detail}") from e
    except Exception as e:
        raise ModelCallError(f"model endpoint unreachable ({url}): {e}") from e


# Patched to a no-op in tests so retry backoff doesn't actually sleep.
_sleep = time.sleep


def _is_transient(err: "ModelCallError") -> bool:
    """A network hiccup or server-side 5xx worth retrying — as opposed to a 4xx
    (deterministic client error, e.g. a rejected response_format param) which must
    propagate immediately so callers can react."""
    m = str(err)
    return "unreachable" in m or "HTTP 5" in m


def _post_resilient(url, payload, headers, timeout, retries: int) -> dict:
    """`_post_json` with exponential-backoff retries on TRANSIENT failures only.
    The endpoint is treated as a remote API (agents on one box, inference on
    another), so a single connection blip or 5xx shouldn't abort a whole pass."""
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            return _post_json(url, payload, headers, timeout)
        except ModelCallError as e:
            if attempt < retries and _is_transient(e):
                _sleep(delay)
                delay *= 2
                continue
            raise


def reachable(cfg: LibrarianConfig, *, timeout: int = 5) -> tuple[bool, str]:
    """Cheap liveness probe against the configured base_url. Returns (True,
    "reachable") if the host answers at all — any HTTP response (even 404)
    means something is listening — and (False, <reason>) on a connection/
    timeout error, so callers (maintain's preflight, `wiki-librarian status`)
    can show WHY a down endpoint failed instead of just that it did. No model
    call."""
    url = str(cfg.get("base_url")).rstrip("/")
    headers = {"User-Agent": UA}
    key = cfg.api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(0)
        return True, "reachable"
    except urllib.error.HTTPError:
        return True, "reachable"  # the host answered; we only care that it is up
    except Exception as e:
        return False, str(e)


def chat(cfg: LibrarianConfig, task: str, messages: list[dict],
         *, json_object: bool = True) -> str:
    """One chat completion for `task`; returns the assistant message content.

    Tries `response_format: json_object` first (most servers honor it); if the
    server rejects the parameter, retries once without it.
    """
    url = str(cfg.get("base_url")).rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    key = cfg.api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": cfg.model_for(task),
        "messages": messages,
        "temperature": cfg.get("temperature"),
    }
    max_tokens = cfg.get("max_tokens")
    if max_tokens:  # 0/None -> omit, let the server decide
        payload["max_tokens"] = int(max_tokens)
    timeout = int(cfg.get("timeout"))
    retries = int(cfg.get("network_retries") or 0)
    if json_object:
        try:
            data = _post_resilient(url, {**payload, "response_format": {"type": "json_object"}},
                                   headers, timeout, retries)
            return _content(data)
        except ModelCallError as e:
            # Some servers 400 on response_format; fall through and try plain.
            if "HTTP 4" not in str(e):
                raise
    data = _post_resilient(url, payload, headers, timeout, retries)
    return _content(data)


# Reasoning models (Ornith, DeepSeek-R1, QwQ, …) emit a chain-of-thought preamble
# inline in the content before the answer. Strip the common wrappers so the
# downstream JSON parsers see the answer, not braces buried in the thinking.
_REASONING = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)


def strip_reasoning(text: str) -> str:
    return _REASONING.sub("", text).strip()


def _content(data: dict) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ModelCallError(f"malformed completion response: {json.dumps(data)[:300]}") from e
    if isinstance(content, str):
        content = strip_reasoning(content)
    if not isinstance(content, str) or not content.strip():
        raise ModelCallError("model returned empty content")
    return content
