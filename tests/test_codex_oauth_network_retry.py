# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import codex_oauth


class _Response:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {"access_token": "token-value", "expires_in": 3600}


class _Session:
    def __init__(self):
        self.calls = 0

    @staticmethod
    def _get_common_headers():
        return {}

    def post(self, url, headers=None, data=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("curl: (35) BoringSSL SSL_connect: connection closed abruptly")
        return _Response()


class CodexOauthNetworkRetryTests(unittest.TestCase):
    def test_exchange_token_retries_transient_ssl_failure(self):
        session = _Session()

        with patch.object(codex_oauth.time, "sleep") as sleep:
            result = codex_oauth.exchange_codex_token(session, "code", "verifier")

        self.assertEqual(result["access_token"], "token-value")
        self.assertEqual(session.calls, 2)
        sleep.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()
