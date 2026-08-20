import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from animew import auth


class TestVerifier(unittest.TestCase):
    def test_length_and_charset(self):
        v = auth.generate_code_verifier(64)
        self.assertEqual(len(v), 64)
        self.assertTrue(all(c in auth._VERIFIER_CHARS for c in v))

    def test_unique(self):
        self.assertNotEqual(auth.generate_code_verifier(), auth.generate_code_verifier())

    def test_rejects_bad_length(self):
        with self.assertRaises(ValueError):
            auth.generate_code_verifier(10)


class TestAuthorizeUrl(unittest.TestCase):
    def test_plain_pkce(self):
        verifier = "x" * 64
        url = auth.build_authorize_url("cid", verifier, "http://localhost:8765/callback", "st8")
        self.assertIn("code_challenge=" + verifier, url)  # plain: challenge == verifier
        self.assertIn("code_challenge_method=plain", url)
        self.assertIn("client_id=cid", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback", url)
        self.assertIn("state=st8", url)


class TestTokenStore(unittest.TestCase):
    def test_save_load_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = auth.TokenStore(Path(tmp) / "token.json")
            self.assertIsNone(store.load())
            store.save({"access_token": "a", "refresh_token": "r"})
            self.assertEqual(store.load()["access_token"], "a")
            self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o600)
            store.clear()
            self.assertIsNone(store.load())


class TestTokenRequests(unittest.TestCase):
    class FakeResp:
        def __init__(self, payload, status=200):
            self.payload = payload
            self.status_code = status
            self.text = str(payload)

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self.payload

    def test_exchange_code(self):
        with mock.patch.object(auth.requests, "post", return_value=self.FakeResp(
                {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})) as post:
            tokens = auth.exchange_code("cid", "sec", "CODE", "VERIF", "http://localhost:8765/callback")
            self.assertEqual(tokens["access_token"], "AT")
            self.assertIn("expires_at", tokens)
            body = post.call_args.kwargs["data"]
            self.assertEqual(body["client_secret"], "sec")
            self.assertEqual(body["code_verifier"], "VERIF")
            self.assertEqual(body["grant_type"], "authorization_code")

    def test_refresh_keeps_refresh_token(self):
        with mock.patch.object(auth.requests, "post", return_value=self.FakeResp(
                {"access_token": "AT2", "expires_in": 3600})):
            tokens = auth.refresh_access_token("cid", "", "RT")
            self.assertEqual(tokens["access_token"], "AT2")
            self.assertEqual(tokens["refresh_token"], "RT")  # preserved


class TestLogin(unittest.TestCase):
    def test_full_flow_with_fake_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            # simulate the browser hitting the callback
            def fake_browser(url):
                import threading
                import urllib.request
                qs = url.split("?", 1)[1]
                parts = dict(p.split("=", 1) for p in qs.split("&"))
                import urllib.parse
                state = urllib.parse.unquote(parts["state"])
                verifier = urllib.parse.unquote(parts["code_challenge"])
                threading.Thread(
                    target=lambda: urllib.request.urlopen(
                        f"http://localhost:8765/callback?code=THE_CODE&state={state}",
                        timeout=5,
                    ),
                    daemon=True,
                ).start()
                return verifier

            with mock.patch.object(auth.webbrowser, "open", side_effect=fake_browser):
                with mock.patch.object(auth.requests, "post", return_value=TestTokenRequests.FakeResp(
                        {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})):
                    tokens = auth.login("cid", "", "http://localhost:8765/callback", timeout=30)
                    self.assertEqual(tokens["access_token"], "AT")

    def test_timeout(self):
        with mock.patch.object(auth.webbrowser, "open"):
            with self.assertRaises(TimeoutError):
                auth.login("cid", "", "http://localhost:8765/callback", timeout=1)


if __name__ == "__main__":
    unittest.main()
