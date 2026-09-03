# Repository is organised by function; nothing is installed as a package, so the
# sibling directories are put on sys.path before importing anything from them.
import os as _os, sys as _sys
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _sib in ("core", "hooks", "ops", "wiki"):
    _p = _os.path.join(_REPO, _sib)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import importlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class EmbedAuthBase(unittest.TestCase):
    """Each test runs against a throwaway COPILOT_HOME.

    config caches nothing across calls, but it resolves through env vars, so the
    modules are reimported per-test to pick up the temp environment. The live
    ~/.copilot config is never read or written.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cache = self.home / "embed-token.json"
        self._saved = {k: os.environ.get(k) for k in
                       ("COPILOT_HOME", "LTM_EMBED_ENDPOINT", "LTM_EMBED_TOKEN_CACHE")}
        os.environ["COPILOT_HOME"] = str(self.home)
        os.environ["LTM_EMBED_ENDPOINT"] = "https://example.invalid"
        os.environ["LTM_EMBED_TOKEN_CACHE"] = str(self.cache)

        import config
        importlib.reload(config)
        import vault_embed
        importlib.reload(vault_embed)
        import ltm_session_start
        importlib.reload(ltm_session_start)
        self.config, self.embed, self.hook = config, vault_embed, ltm_session_start

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()
        import config
        importlib.reload(config)

    def _write_token(self, offset_seconds: float):
        self.cache.write_text(
            json.dumps({"token": "dummy", "expires_on": time.time() + offset_seconds}),
            encoding="utf-8")


class TokenStatusTests(EmbedAuthBase):

    def test_missing_cache_reports_missing(self):
        st = self.embed.token_status()
        self.assertTrue(st["configured"])
        self.assertEqual(st["state"], "missing")

    def test_expired_token_reports_expired(self):
        self._write_token(-10)
        self.assertEqual(self.embed.token_status()["state"], "expired")

    def test_token_inside_skew_window_counts_as_expired(self):
        # A token with 60s left would die mid-request; treat it as expired so the
        # user is told to re-login BEFORE the failure, not after.
        self._write_token(60)
        self.assertEqual(self.embed.token_status()["state"], "expired")

    def test_healthy_token_reports_ok(self):
        self._write_token(9999)
        st = self.embed.token_status()
        self.assertEqual(st["state"], "ok")
        self.assertGreater(st["expires_in"], self.embed.EXPIRY_SKEW_SECONDS)

    def test_no_endpoint_reports_not_configured(self):
        os.environ["LTM_EMBED_ENDPOINT"] = ""
        st = self.embed.token_status()
        self.assertFalse(st["configured"])
        self.assertEqual(st["state"], "not-configured")

    def test_corrupt_cache_does_not_raise(self):
        self.cache.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.embed.token_status()["state"], "missing")


class AuthNudgeTests(EmbedAuthBase):
    """The session-start reminder to run `az login`."""

    def test_warns_when_token_missing(self):
        out = "\n".join(self.hook._section_embed_auth_nudge())
        self.assertIn("az login", out)
        self.assertIn("degraded", out)

    def test_warns_when_token_expired(self):
        self._write_token(-10)
        self.assertIn("az login", "\n".join(self.hook._section_embed_auth_nudge()))

    def test_silent_when_token_healthy(self):
        self._write_token(9999)
        self.assertEqual(self.hook._section_embed_auth_nudge(), [])

    def test_silent_when_embeddings_not_configured(self):
        # A user who never enabled semantic search must never be nagged about a
        # login they do not need.
        os.environ["LTM_EMBED_ENDPOINT"] = ""
        self.assertEqual(self.hook._section_embed_auth_nudge(), [])

    def test_never_raises_even_if_module_is_broken(self):
        # The nudge is optional; it must not be able to break session start.
        import vault_embed
        original = vault_embed.token_status
        vault_embed.token_status = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(self.hook._section_embed_auth_nudge(), [])
        finally:
            vault_embed.token_status = original


class EmbedFallbackTests(EmbedAuthBase):
    """embed() must degrade to [] rather than raising, on every failure path."""

    def test_empty_input_returns_empty(self):
        self.assertEqual(self.embed.embed([]), [])

    def test_no_endpoint_returns_empty(self):
        os.environ["LTM_EMBED_ENDPOINT"] = ""
        self.assertEqual(self.embed.embed(["hello"]), [])

    def test_unavailable_token_returns_empty_not_raises(self):
        def boom():
            raise self.embed.EmbedUnavailable("no az")
        original = self.embed._get_token
        self.embed._get_token = boom
        try:
            self.assertEqual(self.embed.embed(["hello"]), [])
        finally:
            self.embed._get_token = original

    def test_remote_request_uses_bearer_token(self):
        token = "fake-token-for-header-test"
        seen = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"data": []}'

        def fake_urlopen(request, timeout):
            seen["authorization"] = request.get_header("Authorization")
            seen["timeout"] = timeout
            return Response()

        with patch.object(self.embed.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(self.embed._post({"input": ["hello"]}, token), {"data": []})

        self.assertEqual(seen["authorization"], f"Bearer {token}")
        self.assertEqual(seen["timeout"], self.embed.REQUEST_TIMEOUT)


    def test_http_400_large_batch_is_bisected_and_order_preserved(self):
        self._write_token(9999)
        calls = []

        def fake_post(body, token):
            batch = body["input"]
            calls.append(len(batch))
            if len(batch) > 5:
                raise self.embed.urllib.error.HTTPError(
                    "https://example.invalid", 400, "Bad Request", {},
                    io.BytesIO(b'{"error":{"message":"maximum input tokens exceeded"}}'))
            return {
                "data": [
                    {"index": i, "embedding": [float(text)]}
                    for i, text in enumerate(batch)
                ]
            }

        with patch.object(self.embed, "_post", fake_post):
            result = self.embed.embed([str(i) for i in range(20)])

        self.assertEqual([row[0] for row in result], [float(i) for i in range(20)])
        self.assertEqual(calls[0], 20)
        self.assertTrue(all(size <= 5 for size in calls if size not in (20, 10)))

    def test_http_400_single_input_still_fails_cleanly(self):
        self._write_token(9999)

        def fail(_body, _token):
            raise self.embed.urllib.error.HTTPError(
                "https://example.invalid", 400, "Bad Request", {},
                io.BytesIO(b'{"error":{"message":"invalid dimensions"}}'))

        with patch.object(self.embed, "_post", fail):
            self.assertEqual(self.embed.embed(["hello"]), [])

    def test_tenant_mismatch_refreshes_cached_token_once(self):
        tokens = iter(("old-tenant-token", "correct-tenant-token"))
        calls = []

        def fake_post(body, token):
            calls.append((list(body["input"]), token))
            if token == "old-tenant-token":
                raise self.embed.urllib.error.HTTPError(
                    "https://example.invalid", 400, "Bad Request", {},
                    io.BytesIO(
                        b'{"error":{"message":"Token tenant old does not match resource tenant."}}'))
            return {
                "data": [
                    {"index": i, "embedding": [float(i)]}
                    for i, _ in enumerate(body["input"])
                ]
            }

        with patch.object(self.embed, "_get_token", lambda: next(tokens)), \
                patch.object(self.embed, "_post", fake_post):
            result = self.embed.embed(["one", "two"])

        self.assertEqual(result, [[0.0], [1.0]])
        self.assertEqual(
            calls,
            [
                (["one", "two"], "old-tenant-token"),
                (["one", "two"], "correct-tenant-token"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
