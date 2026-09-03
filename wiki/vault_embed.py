#!/usr/bin/env python3
"""Azure OpenAI embeddings for the vault — standard library only.

Deliberately talks to the REST API over `urllib` rather than importing the
`openai` package. This repo's best property is that it needs nothing from PyPI
(see README: stdlib-only apart from optional pyyaml), and an embedding call is
one HTTP POST. Adding a SDK to issue it would be a poor trade.

Auth is a bearer token from the Azure CLI, so it inherits whatever `az login`
the user already has — no keys in config, nothing to leak into a commit.

**Why the token is cached to disk with its expiry.** `az account get-access-token`
costs roughly 1-2 seconds. The session-start hook runs on *every* session under a
10,000 ms budget where the rest of the work is ~40 ms, so calling `az` there
would dominate startup. Instead this module writes `{token, expires_on}` to
`config.get("embed_token_cache")`, and the hook reads that file in well under a
millisecond to decide whether to remind the user to re-run `az login`.

Nothing here raises on failure. A missing endpoint, an expired login, a network
error or a throttled response all return empty results, and callers fall back to
lexical search. Semantic search is an enhancement; it must never be able to take
the vault down.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BUILD = Path(__file__).resolve().parent
if str(BUILD) not in sys.path:
    sys.path.insert(0, str(BUILD))
import config

# Azure AD resource for Azure OpenAI / Cognitive Services data-plane calls.
TOKEN_RESOURCE = "https://cognitiveservices.azure.com"

# Refresh a little before the real expiry so a long request cannot start with a
# token that dies mid-flight.
EXPIRY_SKEW_SECONDS = 300

# Azure OpenAI caps a single embeddings request; batching keeps the round trips
# down without risking a 400 on an oversized body.
BATCH_SIZE = 64

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3


class EmbedUnavailable(Exception):
    """Raised internally, never propagated to callers of embed()."""


# ------------------------------------------------------------------ auth
def _read_token_cache() -> dict:
    p = Path(config.get("embed_token_cache"))
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_token_cache(token: str, expires_on: float) -> None:
    p = Path(config.get("embed_token_cache"))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"token": token, "expires_on": expires_on}),
                       encoding="utf-8")
        tmp.replace(p)
        # The file holds a live bearer token. Best-effort lock-down; Windows
        # ACLs are not chmod, so this is defence in depth rather than a
        # guarantee, and the token is short-lived by design.
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def token_status() -> dict:
    """Cheap, side-effect-free view of the cached credential.

    Reads one small JSON file and shells out to nothing, so the session-start
    hook can call it without a latency penalty. Returns:

        configured  bool  an embedding endpoint is set at all
        state       str   'ok' | 'expired' | 'missing' | 'not-configured'
        expires_in  int   seconds remaining (may be negative), 0 if unknown
    """
    endpoint = config.get("embed_endpoint")
    if not endpoint:
        return {"configured": False, "state": "not-configured", "expires_in": 0}
    cache = _read_token_cache()
    exp = cache.get("expires_on")
    if not cache.get("token") or not exp:
        return {"configured": True, "state": "missing", "expires_in": 0}
    remaining = int(float(exp) - time.time())
    state = "ok" if remaining > EXPIRY_SKEW_SECONDS else "expired"
    return {"configured": True, "state": state, "expires_in": remaining}


def _get_token() -> str:
    cache = _read_token_cache()
    exp = cache.get("expires_on")
    if cache.get("token") and exp and float(exp) - time.time() > EXPIRY_SKEW_SECONDS:
        return cache["token"]
    try:
        r = subprocess.run(
            ["az", "account", "get-access-token", "--resource", TOKEN_RESOURCE,
             "--output", "json"],
            capture_output=True, text=True, timeout=60,
            # az is a batch file on Windows; without a shell it is not found.
            shell=(os.name == "nt"))
    except (OSError, subprocess.SubprocessError) as e:
        raise EmbedUnavailable(f"could not run az: {e}") from e
    if r.returncode != 0:
        raise EmbedUnavailable(
            "az account get-access-token failed — run `az login`. "
            + (r.stderr or "").strip()[:300])
    try:
        payload = json.loads(r.stdout)
        tok = payload["accessToken"]
    except (json.JSONDecodeError, KeyError) as e:
        raise EmbedUnavailable(f"unexpected az output: {e}") from e
    # `expires_on` is epoch seconds on modern az; older builds only give the
    # local-time string `expiresOn`, which is ambiguous across DST. Prefer the
    # epoch field and fall back to a conservative fixed lifetime.
    try:
        expires_on = float(payload.get("expires_on") or 0) or (time.time() + 3000)
    except (TypeError, ValueError):
        expires_on = time.time() + 3000
    _write_token_cache(tok, expires_on)
    return tok


# ------------------------------------------------------------- embedding
def _endpoint_url() -> str:
    base = config.get("embed_endpoint").rstrip("/")
    dep = config.get("embed_deployment")
    ver = config.get("embed_api_version")
    return f"{base}/openai/deployments/{dep}/embeddings?api-version={ver}"


def _post(body: dict, token: str) -> dict:
    req = urllib.request.Request(
        _endpoint_url(),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def dimensions() -> int:
    try:
        return int(config.get("embed_dimensions"))
    except (TypeError, ValueError):
        return 1536


def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts, or return [] if embeddings are unavailable for any reason.

    Returning [] rather than raising is deliberate: every caller is a retrieval
    path that has a working lexical fallback, and a vault that goes dark because
    a token lapsed would be worse than one that quietly searches less well.
    """
    if not texts or not config.get("embed_endpoint"):
        return []
    try:
        token = _get_token()
    except EmbedUnavailable as e:
        print(f"[vault_embed] {e}", file=sys.stderr)
        return []

    dims = dimensions()
    tenant_refreshed = False

    def send(batch: list[str]) -> list[list[float]]:
        nonlocal token, tenant_refreshed
        body = {"input": batch, "dimensions": dims}
        for attempt in range(MAX_RETRIES):
            try:
                data = _post(body, token)
                # The API may return items out of order; `index` is authoritative.
                items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
                return [it["embedding"] for it in items]
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    payload = json.loads(e.read().decode("utf-8"))
                    detail = payload.get("error", {}).get("message", "")
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                    pass
                if e.code == 401 and attempt == 0:
                    # Token rejected despite looking fresh — force a refresh once.
                    try:
                        _write_token_cache("", 0)
                        token = _get_token()
                        continue
                    except EmbedUnavailable:
                        return []
                if (e.code == 400
                        and "does not match resource tenant" in detail.casefold()
                        and not tenant_refreshed):
                    # The disk cache can outlive an `az account set` switch.
                    # Refresh once using the now-active subscription instead of
                    # recursively splitting a request whose payload is valid.
                    tenant_refreshed = True
                    try:
                        _write_token_cache("", 0)
                        token = _get_token()
                        continue
                    except EmbedUnavailable:
                        return []
                if e.code == 400 and len(batch) > 1:
                    # Azure OpenAI applies a token budget to the whole embeddings
                    # request, not just to each input. Vault chunks are bounded,
                    # but 64 otherwise-valid chunks can exceed that aggregate
                    # budget. Bisect until the service accepts each sub-batch.
                    mid = len(batch) // 2
                    left = send(batch[:mid])
                    if len(left) != mid:
                        return []
                    right = send(batch[mid:])
                    if len(right) != len(batch) - mid:
                        return []
                    return left + right
                if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                    # Honour Retry-After when the service supplies it; otherwise
                    # back off exponentially.
                    wait = e.headers.get("Retry-After") if e.headers else None
                    try:
                        delay = float(wait) if wait else 2 ** attempt
                    except (TypeError, ValueError):
                        delay = 2 ** attempt
                    time.sleep(min(delay, 30))
                    continue
                suffix = f" — {detail[:300]}" if detail else ""
                print(f"[vault_embed] HTTP {e.code}: {e.reason}{suffix}",
                      file=sys.stderr)
                return []
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                    KeyError) as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                print(f"[vault_embed] {type(e).__name__}: {e}", file=sys.stderr)
                return []
        return []

    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        vectors = send(batch)
        if len(vectors) != len(batch):
            return []
        out.extend(vectors)
    return out


def embed_one(text: str) -> list[float] | None:
    got = embed([text])
    return got[0] if got else None


if __name__ == "__main__":
    st = token_status()
    print(f"endpoint   : {config.get('embed_endpoint') or '(not configured)'}")
    print(f"deployment : {config.get('embed_deployment')}")
    print(f"dimensions : {dimensions()}")
    print(f"token      : {st['state']} ({st['expires_in']}s remaining)")
    if st["configured"]:
        v = embed_one("smoke test")
        print(f"smoke test : {'OK, dim ' + str(len(v)) if v else 'FAILED'}")
