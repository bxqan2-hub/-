from unittest.mock import patch

from paypal.models import SessionState
from paypal.session import PayPalSession


class FakeCurlSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers = {}
        self.proxies = {}
        self.trust_env = True

    def close(self):
        return None


def test_socks5h_proxy_keeps_reference_curl_transport(monkeypatch):
    monkeypatch.delenv("PAYPAL_HTTP_ENGINE", raising=False)
    with patch("paypal.session.CurlSession", FakeCurlSession):
        session = PayPalSession(
            SessionState(),
            proxy_url="socks5h://user:pass@proxy.test:3010",
            country="TH",
            locale="th_TH",
        )

    assert session.engine == "curl_cffi"
    assert session.client.kwargs == {"impersonate": "chrome"}
    assert session.client.proxies == {
        "http": "socks5h://user:pass@proxy.test:3010",
        "https": "socks5h://user:pass@proxy.test:3010",
    }
    assert session.client.headers["Accept-Language"].startswith("th-TH")
    assert session.client.trust_env is False


def test_explicit_httpx_override_is_preserved(monkeypatch):
    monkeypatch.setenv("PAYPAL_HTTP_ENGINE", "httpx")
    with patch("paypal.session.httpx.Client") as client_cls:
        session = PayPalSession(SessionState(), country="BR", locale="pt_BR")

    assert session.engine == "httpx"
    client_cls.assert_called_once()
