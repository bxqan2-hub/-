"""Contract tests for the protocol-only local Chromium transport."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests import Response
from requests.structures import CaseInsensitiveDict

from core.abais_protocol import local_browser_session as mod


def response(status=200, payload=None, location=None):
    r = Response()
    r.status_code = status
    r.headers = CaseInsensitiveDict({'location': location} if location else {})
    r._content = json.dumps(payload or {}).encode()
    r._content_consumed = True
    return r


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(mod, 'check_stop_requested', lambda: None)
    s = mod.LocalBrowserSession(proxy='socks5h://proxy.example:1080', email='user@example.test')
    yield s
    s.close()


@pytest.mark.parametrize('selected,actual', [('151','151.0.1.2'),('152','152.0.1.2'),('153','153.0.1.2'),('latest','153.0.1.2')])
def test_selected_kernel_is_runtime_not_hardcoded(monkeypatch, selected, actual):
    import playwright.sync_api
    from core import roxy_registration, browser_exit_geo
    monkeypatch.setattr(mod.roxy_cfg, 'ROXY_CORE_VERSION', selected)
    monkeypatch.setattr(mod, 'check_stop_requested', lambda: None)
    monkeypatch.setattr(mod.LocalBrowserSession, 'get', Mock(return_value=response(200)))
    client = Mock()
    client._last_profile_create_summary = {'locale':'en-GB'}
    client.open_profile.return_value = SimpleNamespace(
        profile_id='local-'+'7'*32, created_by_run=True, debugger_address='127.0.0.1:9222',
        raw={'data':{'coreVersion':actual.split('.')[0]}}, preflight_exit_geo={'ip':'203.0.113.7'},
    )
    page = Mock()
    page.evaluate.return_value = {'user_agent':'Chrome/'+actual, 'ua_full_version':actual, 'language':'en-GB','timezone':'Europe/London'}
    browser = Mock()
    browser.version = actual
    browser.contexts = [Mock()]
    browser.contexts[0].pages = []
    browser.contexts[0].new_page.return_value = page
    pw = Mock()
    pw.chromium.connect_over_cdp.return_value = browser
    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', lambda: SimpleNamespace(start=lambda:pw))
    monkeypatch.setattr(mod, 'RoxyBrowserClient', Mock(return_value=client))
    monkeypatch.setattr(browser_exit_geo, 'probe_playwright_context_exit_geo', lambda *a,**kw:{'ip':'203.0.113.7','timezone':'Europe/London','country':'GB'})
    # Keep the real exit equality gate, no synthetic profile pool or curl session.
    with mod.LocalBrowserSession(proxy='http://proxy.example',email='u@example.test') as session:
        assert session.profile['core_version'] == actual
        assert session.profile['requested_core'] == selected
        assert session.profile['headless'] is True
        client.open_profile.assert_called_once_with(headless=True, require_proxy_exit_ip=True,
            on_profile_ready=mod.bind_roxy_profile, proxy_probe_stop_check=mod.check_stop_requested)
    client.close_profile.assert_called_once_with('local-'+'7'*32)
    client.delete_profile.assert_called_once_with('local-'+'7'*32)
    session.close()
    client.delete_profile.assert_called_once()


@pytest.mark.parametrize('failure', ['core','geo','identity'])
def test_start_failure_cleans_profile(monkeypatch, transport, failure):
    import playwright.sync_api
    from core import browser_exit_geo
    client = transport.client
    client.open_profile = Mock(return_value=SimpleNamespace(profile_id='local-'+'7'*32,created_by_run=True,
        debugger_address='127.0.0.1:9222',raw={'data':{'coreVersion':'151'}},preflight_exit_geo={'ip':'203.0.113.7'}))
    client.close_profile = Mock();client.delete_profile = Mock()
    browser=Mock();browser.version='153.0.1.2' if failure=='core' else '151.0.1.2';browser.contexts=[Mock()]
    browser.contexts[0].pages=[]
    browser.contexts[0].new_page.return_value.evaluate.return_value={'user_agent':'Chrome/151.0.1.2','language':'en-US','timezone':'wrong'}
    pw=Mock();pw.chromium.connect_over_cdp.return_value=browser
    monkeypatch.setattr(playwright.sync_api,'sync_playwright',lambda:SimpleNamespace(start=lambda:pw))
    monkeypatch.setattr(browser_exit_geo,'probe_playwright_context_exit_geo',lambda *a,**kw:{'ip':'203.0.113.8' if failure=='geo' else '203.0.113.7','timezone':'Europe/London'})
    transport.get=Mock(return_value=response(200))
    transport.requested_core='151'
    with pytest.raises(RuntimeError):transport.__enter__()
    client.close_profile.assert_called_once();client.delete_profile.assert_called_once();pw.stop.assert_called_once()


@pytest.mark.parametrize('url', ['http://chatgpt.com/','https://evil.example/','https://chatgpt.com.evil.example/','https://user@chatgpt.com/','https://chatgpt.com:444/'])
def test_remote_targets_are_exact_allowlist(transport,url):
    transport._fetch=Mock()
    with pytest.raises(RuntimeError,match='protocol_target_origin'):transport.get(url)
    transport._fetch.assert_not_called()


@pytest.mark.parametrize('override', [{'proxies':{'https':'http://different'}},{'impersonate':'chrome146'},{'verify':False}])
def test_per_request_transport_override_stops_before_send(transport,override):
    transport._fetch=Mock()
    with pytest.raises(RuntimeError,match='protocol_transport_override'):transport.get('https://chatgpt.com/',**override)
    transport._fetch.assert_not_called()


def test_redirect_semantics_and_cross_origin_credentials(transport):
    transport._fetch=Mock(side_effect=[response(302,location='https://auth.openai.com/final'),response(200)])
    transport.post('https://chatgpt.com/start',data='a=b',headers={'Authorization':'Bearer private','openai-sentinel-token':'private','Content-Type':'text/plain'})
    args=transport._fetch.call_args.args
    assert args[0]=='GET' and args[1]=='https://auth.openai.com/final' and args[3] is None
    assert not args[2]


def test_manual_redirect_not_followed_and_307_preserves_same_origin_body(transport):
    transport._fetch=Mock(return_value=response(302,location='/final'))
    assert transport.get('https://chatgpt.com/start',allow_redirects=False).status_code==302
    transport._fetch.assert_called_once()
    transport._fetch=Mock(side_effect=[response(307,location='/final'),response(200)])
    transport.post('https://chatgpt.com/start',data='private')
    assert transport._fetch.call_args.args[0]=='POST' and transport._fetch.call_args.args[3]=='private'


def test_post_307_across_origin_does_not_leak_body(transport):
    transport._fetch=Mock(return_value=response(307,location='https://auth.openai.com/final'))
    with pytest.raises(RuntimeError,match='protocol_cross_origin_body_redirect'):
        transport.post('https://chatgpt.com/start',data='private')
    transport._fetch.assert_called_once()


def test_redirect_limit_is_bounded(transport):
    transport._fetch=Mock(return_value=response(302,location='/loop'))
    with pytest.raises(RuntimeError,match='protocol_redirect_limit'):transport.get('https://chatgpt.com/start')
    assert transport._fetch.call_count==16


def test_mfa_requires_same_browser_email_match(transport):
    transport._fetch=Mock(return_value=response(200,{'accessToken':'private','user':{'email':'other@example.test'}}))
    with pytest.raises(RuntimeError,match='protocol_session_account_mismatch'):transport.get('https://chatgpt.com/api/auth/session')
    with pytest.raises(RuntimeError,match='protocol_mfa_identity_unverified'):transport.post('https://chatgpt.com/backend-api/accounts/mfa/enroll')
    assert transport._fetch.call_count==1
    transport._fetch.return_value=response(200,{'accessToken':'private','user':{'email':'USER@example.test'}})
    transport.get('https://chatgpt.com/api/auth/session')
    transport._fetch.return_value=response(200,{'success':True})
    assert transport.post('https://chatgpt.com/backend-api/accounts/mfa/enroll',headers={'Authorization':'Bearer private'}).status_code==200
    transport._fetch.return_value=response(401)
    transport.get('https://chatgpt.com/api/auth/session')
    with pytest.raises(RuntimeError,match='protocol_mfa_identity_unverified'):transport.post('https://chatgpt.com/backend-api/accounts/mfa/enroll')


def test_fetch_uses_browser_native_headers_and_redacts_failures(transport):
    page,cdp=Mock(),Mock()
    transport._page=Mock(return_value=(page,cdp))
    def evaluate(script,arg):
        assert script==mod._FETCH_JS
        assert arg['headers']=={'Authorization':'Bearer private'}
        transport._on_request_paused(cdp, {'requestId':'fixture','request':{'url':arg['url'],'method':arg['method']},'responseStatusCode':200,'responseHeaders':[{'name':'Content-Type','value':'application/json'}]})
        return {'status':200,'text':'{}'}
    page.url='https://chatgpt.com/'
    page.evaluate.side_effect=evaluate
    r=transport.get('https://chatgpt.com/test',headers={'User-Agent':'Chrome/146','sec-ch-ua':'fake','Accept-Language':'wrong','Cookie':'private','Authorization':'Bearer private'})
    assert r.status_code==200 and r.json()=={}
    page.evaluate.side_effect=RuntimeError('Execution context was destroyed PRIVATE PASSWORD TOKEN COOKIE')
    with pytest.raises(RuntimeError) as error:transport.post('https://chatgpt.com/test',data='PRIVATE')
    assert 'PRIVATE' not in str(error.value)
    assert 'operation=protocol_request phase=api_fetch method=POST' in str(error.value)
    assert 'reason=context_destroyed' in str(error.value)
    assert not transport._pending_fetch
    assert not any(call.args[0] == 'Fetch.disable' for call in cdp.send.call_args_list)


def test_sdk_is_executed_in_same_profile_and_errors_are_redacted(transport):
    page=Mock();page.url=mod.OPENAI_AUTH+'/create-account';transport._page=Mock(return_value=(page,Mock()))
    page.evaluate.side_effect=[True,{'openai-sentinel-token':'fixture'}]
    assert transport.build_headers('device','authorize_continue')=={'openai-sentinel-token':'fixture'}
    script,args=page.evaluate.call_args.args
    assert 'window.SentinelSDK.token(flow)' in script and args=={'flow':'authorize_continue','device':'device'}
    page.evaluate.side_effect=[True,RuntimeError('SECRET')]
    with pytest.raises(RuntimeError,match='^stage=protocol_browser_sdk$'):transport.build_headers('device','flow')


def test_no_proxy_is_rejected_before_profile_creation():
    with pytest.raises(RuntimeError,match='protocol_proxy_missing'):mod.LocalBrowserSession(proxy='',email='u@example.test')


def test_root_and_redirect_urls_normalized_before_capture(transport):
    transport._fetch=Mock(side_effect=[response(302,location='https://auth.openai.com#fragment'),response(200)])
    transport.get('https://chatgpt.com')
    assert transport._fetch.call_args_list[0].args[1]=='https://chatgpt.com/'
    assert transport._fetch.call_args_list[1].args[1]=='https://auth.openai.com/'


def test_sdk_load_failure_is_redacted(monkeypatch, transport):
    page=Mock();page.url=mod.OPENAI_AUTH+'/log-in'
    page.evaluate.side_effect=RuntimeError('PRIVATE-TOKEN cookie=SECRET')
    transport._page=Mock(return_value=(page,Mock()))
    with pytest.raises(RuntimeError,match='^stage=protocol_browser_sdk$'):transport.build_headers('device','flow')


def test_missing_mfa_bearer_stops_before_network(transport):
    transport._identity_verified=True
    transport._fetch=Mock()
    with pytest.raises(RuntimeError,match='protocol_mfa_token_missing'):transport.post('https://chatgpt.com/backend-api/accounts/mfa/enroll')
    transport._fetch.assert_not_called()


def test_worker_borrows_browser_transport_without_curl_or_node(monkeypatch, transport):
    from core.abais_protocol import protocol_register
    curl=Mock(side_effect=AssertionError('curl created'))
    node=Mock(side_effect=AssertionError('node VM created'))
    monkeypatch.setattr(protocol_register.requests,'Session',curl)
    monkeypatch.setattr(protocol_register,'get_sentinel_vm_pool',node)
    worker=protocol_register.ChatGPTProtocolRegister(session=transport,otp_callback=lambda:'123456')
    worker.user_agent='Chrome/153.0.0.0';worker.sentinel=transport
    assert worker.session is transport and worker._session_factory is None
    assert worker._common_headers('https://auth.openai.com/')['user-agent']=='Chrome/153.0.0.0'
    curl.assert_not_called();node.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError("PRIVATE"), ValueError("PRIVATE")])
def test_failed_session_read_clears_previous_mfa_identity(transport, failure):
    transport._fetch = Mock(return_value=response(200, {
        "accessToken": "current-token", "user": {"email": "user@example.test"},
    }))
    transport.get("https://chatgpt.com/api/auth/session")
    transport._fetch.side_effect = failure
    with pytest.raises(type(failure)):
        transport.get("https://chatgpt.com/api/auth/session")
    transport._fetch.reset_mock()
    with pytest.raises(RuntimeError, match="protocol_mfa_identity_unverified"):
        transport.post("https://chatgpt.com/backend-api/accounts/mfa/enroll",
                       headers={"Authorization": "Bearer current-token"})
    transport._fetch.assert_not_called()


@pytest.mark.parametrize("bearer", ["Bearer ", "Bearer other-token"])
def test_mfa_token_must_match_verified_browser_session(transport, bearer):
    transport._fetch = Mock(return_value=response(200, {
        "accessToken": "current-token", "user": {"email": "user@example.test"},
    }))
    transport.get("https://chatgpt.com/api/auth/session")
    transport._fetch.reset_mock()
    with pytest.raises(RuntimeError, match="protocol_mfa_token_(missing|mismatch)"):
        transport.post("https://chatgpt.com/backend-api/accounts/mfa/enroll",
                       headers={"Authorization": bearer})
    transport._fetch.assert_not_called()


@pytest.mark.parametrize("failure_at", ["summary", "browser", "playwright", "profile", "delete"])
def test_cleanup_attempts_all_resources_after_each_failure(monkeypatch, transport, failure_at, caplog):
    transport.opened = SimpleNamespace(profile_id="local-owned-fixture", created_by_run=True)
    transport.browser = Mock()
    transport._playwright = Mock()
    transport.client = Mock()
    targets = {"browser": transport.browser.close, "playwright": transport._playwright.stop,
               "profile": transport.client.close_profile, "delete": transport.client.delete_profile}
    if failure_at == "summary":
        monkeypatch.setattr(mod, "summarize_performance_logs", Mock(side_effect=RuntimeError("PRIVATE")))
    else:
        targets[failure_at].side_effect = RuntimeError("PRIVATE")
    transport.close()
    transport.close()
    for target in targets.values():
        target.assert_called_once()
    transport.client.http.close.assert_called_once()
    assert "PRIVATE" not in caplog.text


@pytest.fixture
def native_transport(monkeypatch, tmp_path):
    """Real Chromium + loopback server; no accounts, external hosts or proxy pool."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import threading
    from playwright.sync_api import sync_playwright

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            requests.append((self.command, self.path, len(body.encode())))
            if self.path in ("/api/accounts/create_account", "/api/auth/session", "/registered-shell"):
                if self.path == "/api/accounts/create_account":
                    payload = b'{"continue_url":"/registered-shell"}'
                elif self.path == "/api/auth/session":
                    payload = b'{"accessToken":"fixture-web-token","user":{"email":"fixture@example.test"}}'
                else:
                    payload = b'<html><head><title>Registered fixture</title></head><body><script src="/cdn/assets/fixture-12345678.js"></script></body></html>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html" if self.path == "/registered-shell" else "application/json")
                if self.path == "/api/accounts/create_account":
                    self.send_header("Set-Cookie", "account=fixture; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path == "/cdn/assets/fixture-12345678.js":
                payload = b"window.fixtureAsset = true;/*" + b"x" * 262144 + b"*/"
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path in ("/redirect", "/keep-body"):
                self.send_response(302 if self.path == "/redirect" else 307)
                self.send_header("Location", "/echo")
                self.send_header("Set-Cookie", "step=fixture; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
                return
            if self.path in ("/create-account/password", "/log-in"):
                payload = b"<html><head><title>Fixture auth</title></head><body><script nonce='fixture'>window.documentLoaded = true; setTimeout(() => { window.SentinelSDK = {token: async flow => JSON.stringify({id: 'fixture-device', flow, c: 'fixture'})}; }, 20);</script></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Security-Policy", "script-src 'nonce-fixture'")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            payload = json.dumps({"method": self.command, "body": body,
                                  "referer": self.headers.get("Referer"),
                                  "cookie": self.headers.get("Cookie"),
                                  "user_agent": self.headers.get("User-Agent"),
                                  "fetch_mode": self.headers.get("Sec-Fetch-Mode")}).encode()
            self.send_response(429 if self.path == "/limited" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setattr(mod, "_ALLOWED_ORIGINS", frozenset([base]))
        monkeypatch.setattr(mod, "check_stop_requested", lambda: None)
        monkeypatch.setattr(mod.roxy_cfg, "ROXY_CACHE_DIR", str(tmp_path / "cache"))
        browser = pw.chromium.launch(headless=True)
        session = mod.LocalBrowserSession(proxy="http://fixture.invalid:1", email="fixture@example.test")
        session.fixture_requests = requests
        session.browser = browser
        session.context = browser.new_context()
        session.context.on("page", session._observe_page)
        session._page(base)[0].goto(base + "/", wait_until="domcontentloaded")
        try:
            yield session, base
        finally:
            session.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@pytest.mark.parametrize("path,method,body", [("/redirect", "GET", ""), ("/keep-body", "POST", "fixture-body")])
def test_native_redirect_cookie_and_body_semantics(native_transport, path, method, body):
    session, base = native_transport
    result = session.post(base + path, data="fixture-body")
    observed = result.json()
    assert result.status_code == 200 and len(result.history) == 1
    assert observed["cookie"] == "step=fixture"
    assert observed["method"] == method and observed["body"] == body
    assert session.context.cookies()[0]["httpOnly"] is True


def test_native_http_errors_and_browser_headers_preserved(native_transport):
    session, base = native_transport
    result = session.get(base + "/limited", headers={"User-Agent": "fake", "sec-fetch-mode": "navigate"})
    assert result.status_code == 429
    assert result.json()["user_agent"] != "fake"
    assert result.json()["fetch_mode"] == "cors"
    with pytest.raises(Exception):
        result.raise_for_status()


def test_mfa_uses_fresh_web_token_without_replacing_oauth_export(monkeypatch, transport):
    from core.abais_protocol.protocol_register import ChatGPTProtocolRegister
    from core.abais_protocol import mfa
    transport._fetch = Mock(return_value=response(200, {
        "accessToken": "current-web-token", "user": {"email": "user@example.test"},
    }))
    bind = Mock(return_value={"secret": "fixture-secret", "activated": True})
    monkeypatch.setattr(mfa, "bind_totp_2fa", bind)
    worker = ChatGPTProtocolRegister(session=transport)
    result = worker._finalize_registration_result({"email": "user@example.test", "access_token": "oauth-token"})
    bind.assert_called_once_with(transport, "current-web-token")
    assert result["access_token"] == "oauth-token"
    assert result["totp_2fa"]["bound"] is True


@pytest.mark.parametrize("payload,status", [
    ({"accessToken": "other-token", "user": {"email": "other@example.test"}}, 200),
    ({"accessToken": "current-token", "user": {"email": "user@example.test"}}, 401),
    ({}, 200),
])
def test_mfa_identity_failure_never_enrolls(monkeypatch, transport, payload, status):
    from core.abais_protocol.protocol_register import ChatGPTProtocolRegister
    from core.abais_protocol import mfa
    transport._fetch = Mock(return_value=response(status, payload))
    bind = Mock()
    monkeypatch.setattr(mfa, "bind_totp_2fa", bind)
    worker = ChatGPTProtocolRegister(session=transport)
    with pytest.raises(RuntimeError, match="stage=protocol_"):
        worker._finalize_registration_result({"email": "user@example.test", "access_token": "oauth-token"})
    bind.assert_not_called()


@pytest.mark.parametrize("success", ["false", "true", 1, False, None, True])
def test_totp_activation_requires_boolean_success(monkeypatch, success):
    from core.abais_protocol import mfa
    monkeypatch.setattr(mfa, "enroll_totp", lambda *a: {"secret": "fixture", "session_id": "fixture"})
    monkeypatch.setattr(mfa, "activate_totp_enrollment", lambda *a: {"success": success})
    result = mfa.bind_totp_2fa(Mock(), "fixture", code_provider=lambda _: "123456")
    assert result["activated"] is (success is True)


def test_native_document_updates_dom_and_api_referrer(native_transport, monkeypatch):
    session, base = native_transport
    monkeypatch.setattr(mod, "OPENAI_AUTH", base)
    result = session.get(base + "/create-account/password")
    page, _ = session._page(base)
    assert result.status_code == 200
    assert page.url == base + "/create-account/password"
    assert page.title() == "Fixture auth"
    assert page.evaluate("window.documentLoaded") is True
    result = session.post(base + "/api/echo", json={"fixture": True},
                          headers={"referer": base + "/unvisited-template"})
    assert result.json()["referer"] == page.url
    assert result.json()["fetch_mode"] == "cors"


def test_bootstrap_response_is_consumed_once(transport):
    initial = response(200)
    initial.url = "https://chatgpt.com/"
    transport._bootstrap_response = initial
    assert transport.get(initial.url) is initial
    assert transport._bootstrap_response is None


def test_native_document_redirect_remains_manual_and_keeps_cookie(native_transport, monkeypatch):
    session, base = native_transport
    monkeypatch.setattr(mod, "OPENAI_AUTH", base)
    result = session.get(base + "/redirect", allow_redirects=False)
    page, _ = session._page(base)
    assert result.status_code == 302 and result.headers["location"] == "/echo"
    assert page.url == base + "/redirect"
    assert session.context.cookies()[0]["value"] == "fixture"


def test_native_sdk_readiness_respects_document_csp(native_transport, monkeypatch):
    session, base = native_transport
    monkeypatch.setattr(mod, "OPENAI_AUTH", base)
    session.get(base + "/log-in")
    values = session.build_headers("fixture-device", "fixture-flow")
    token = json.loads(values["openai-sentinel-token"])
    assert token["id"] == "fixture-device" and token["flow"] == "fixture-flow"


def test_native_public_asset_cache_reuses_bytes_across_isolated_contexts(native_transport, monkeypatch):
    from core import browser_traffic

    session, base = native_transport
    original = browser_traffic.is_cacheable_request
    cacheable = lambda url, *a, **kw: original(url.replace(base, "https://chatgpt.com"), *a, **kw)
    monkeypatch.setattr(browser_traffic, "is_cacheable_request", cacheable)
    monkeypatch.setattr(mod, "is_cacheable_request", cacheable)
    page, _ = session._page(base)
    load = """url => new Promise((resolve, reject) => {
      const s = document.createElement('script'); s.src = url;
      s.onload = () => resolve(window.fixtureAsset); s.onerror = reject;
      document.head.append(s);
    })"""
    url = base + "/cdn/assets/fixture-12345678.js"
    assert page.evaluate(load, url) is True
    session.context.add_cookies([{"name": "private", "value": "first-profile", "url": base}])
    other = mod.LocalBrowserSession(proxy="http://fixture.invalid:1", email="second@example.test")
    other.context = session.browser.new_context()
    other.context.on("page", other._observe_page)
    try:
        fresh, _ = other._page(base)
        fresh.goto(base, wait_until="domcontentloaded")
        assert fresh.evaluate(load, url) is True
        assert other.context.cookies() == []
        assert len([r for r in session.fixture_requests if r[1] == "/cdn/assets/fixture-12345678.js"]) == 1
        other.close()
        assert other.traffic["cache_hits"] == 1
        assert other.traffic["cached_downloaded"] > 262144
    finally:
        other.close()
        other.context.close()


def test_native_optional_rum_upload_blocked_but_auth_api_live(native_transport, monkeypatch):
    from core import browser_traffic

    session, base = native_transport
    monkeypatch.setattr(mod, "block_reason", lambda url, resource, **kw: browser_traffic.block_reason(
        url.replace(base, "https://auth.openai.com"), resource, **kw))
    page, _ = session._page(base)
    outcome = page.evaluate("""async base => {
      try { await fetch(base + '/awe/api/v2/rum', {method:'POST', body:'x'.repeat(1024*1024)}); return 'sent'; }
      catch { return 'blocked'; }
    }""", base)
    assert outcome == "blocked"
    assert not any(r[1] == "/awe/api/v2/rum" for r in session.fixture_requests)
    assert session.post(base + "/api/echo", json={"fixture": True}).status_code == 200


def test_native_registration_finalization_keeps_document_cookie_session_and_mfa(native_transport, monkeypatch):
    from core import browser_traffic

    session, base = native_transport
    monkeypatch.setattr(mod, "OPENAI_AUTH", base)
    monkeypatch.setattr(mod, "CHATGPT_APP", base)
    monkeypatch.setattr(mod, "block_reason", lambda url, resource, **kw: browser_traffic.block_reason(
        url.replace(base, "https://chatgpt.com"), resource, **kw))
    created = session.post(base + "/api/accounts/create_account", json={"name": "Fixture"})
    assert created.status_code == 200 and session._registration_created
    session.get(base + created.json()["continue_url"])
    page, _ = session._page(base)
    assert page.title() == "Registered fixture"
    assert session.context.cookies()[0]["httpOnly"] is True
    assert not any(r[1] == "/cdn/assets/fixture-12345678.js" for r in session.fixture_requests)
    assert session.get(base + "/api/auth/session").status_code == 200
    result = session.post(base + "/backend-api/accounts/mfa/enroll", json={"fixture": True},
                          headers={"Authorization": "Bearer fixture-web-token"})
    assert result.status_code == 200
    assert result.json()["cookie"] == "account=fixture"


@pytest.mark.parametrize("url,resource,headers", [
    ("https://auth.openai.com/api/accounts/user/register", "Fetch", {}),
    ("https://chatgpt.com/api/auth/session", "Fetch", {}),
    ("https://chatgpt.com/backend-api/accounts/mfa/enroll", "Fetch", {}),
    ("https://sentinel.openai.com/sentinel/12345678/sdk.js", "Script", {}),
    ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/fixture.js", "Script", {}),
    ("https://chatgpt.com/cdn/assets/app-12345678.js", "Script", {"Authorization": "Bearer PRIVATE"}),
    ("https://chatgpt.com/cdn/assets/app-12345678.js", "Script", {"Cache-Control": "no-store"}),
])
def test_protocol_cache_never_replays_auth_or_private_requests(transport, url, resource, headers):
    cdp = Mock()
    transport._cache = Mock()
    transport._on_request_paused(cdp, {
        "requestId": "fixture", "resourceType": resource,
        "request": {"url": url, "method": "GET", "headers": headers},
    })
    transport._cache.read.assert_not_called()
    cdp.send.assert_called_once_with("Fetch.continueRequest", {"requestId": "fixture", "interceptResponse": False})


@pytest.mark.parametrize("private_header", [
    {"name": "Set-Cookie", "value": "PRIVATE"},
    {"name": "Vary", "value": "Cookie"},
    {"name": "Access-Control-Allow-Credentials", "value": "true"},
])
def test_protocol_cache_never_stores_profile_state(transport, private_header):
    cdp = Mock()
    transport._cache = Mock()
    transport._on_request_paused(cdp, {
        "requestId": "fixture", "resourceType": "Script", "responseStatusCode": 200,
        "request": {"url": "https://chatgpt.com/cdn/assets/app-12345678.js", "method": "GET"},
        "responseHeaders": [{"name": "Content-Type", "value": "application/javascript"},
                            {"name": "Cache-Control", "value": "public, max-age=3600"}, private_header],
    })
    transport._cache.write.assert_not_called()
    cdp.send.assert_called_once_with("Fetch.continueResponse", {"requestId": "fixture"})


def test_protocol_cache_failure_keeps_live_request(transport):
    cdp = Mock()
    transport._cache = Mock()
    transport._cache.read.side_effect = OSError("PRIVATE")
    transport._on_request_paused(cdp, {
        "requestId": "fixture", "resourceType": "Script",
        "request": {"url": "https://chatgpt.com/cdn/assets/app-12345678.js", "method": "GET"},
    })
    cdp.send.assert_called_once_with("Fetch.continueRequest", {"requestId": "fixture"})
    assert transport._cache_stats["cache_errors"] == 1


def test_protocol_traffic_keeps_compressed_download_separate_from_cache(transport):
    url = "https://chatgpt.com/cdn/assets/app-12345678.js"
    transport._traffic_events = [
        {"method": "Network.requestWillBeSent", "params": {"requestId": rid, "request": {"url": url}}}
        for rid in ("network", "cache")
    ] + [
        {"method": "Network.loadingFinished", "params": {"requestId": "network", "encodedDataLength": 1024}},
        {"method": "Network.loadingFinished", "params": {"requestId": "cache", "encodedDataLength": 4096}},
    ]
    transport._cached_request_ids.add("cache")
    transport._cache_stats.update(cache_hits=1, cached_bytes=4096)
    transport.close()
    assert transport.traffic["downloaded"] == 1024
    assert transport.traffic["cached_downloaded"] == 4096
    assert transport.traffic["observed_transport_bytes"] == 1024


def test_reentrant_page_event_does_not_install_duplicate_cdp_observers(transport):
    target = Mock()
    transport.context = Mock()
    def create():
        transport._observe_page(target)
        return target
    transport.context.new_page.side_effect = create
    page, cdp = transport._page("https://chatgpt.com/")
    assert page is target
    assert transport._page("https://chatgpt.com/")[1] is cdp
    transport.context.new_cdp_session.assert_called_once_with(target)
    assert not transport._creating_page


def test_failed_managed_page_creation_restores_page_observation(transport):
    transport.context = Mock()
    transport.context.new_page.side_effect = RuntimeError("fixture")
    with pytest.raises(RuntimeError, match="fixture"):
        transport._page("https://chatgpt.com/")
    assert not transport._creating_page
    assert not transport._pages


@pytest.mark.parametrize("status,payload,created", [
    (200, {"continue_url": "https://chatgpt.com/api/auth/callback/openai"}, True),
    (403, {"error": {"code": "fixture"}}, False),
    (200, {"error": "fixture", "continue_url": "/completed"}, False),
    (200, {}, False),
])
def test_only_successful_profile_creation_restricts_chat_shell(transport, status, payload, created):
    transport._fetch = Mock(return_value=response(status, payload))
    transport.post("https://auth.openai.com/api/accounts/create_account", json={"name": "Fixture"})
    assert transport._registration_created is created
    assert transport._identity_verified is False
    cdp = Mock()
    transport._cache = None
    transport._on_request_paused(cdp, {
        "requestId": "chat-shell", "resourceType": "Script",
        "request": {"url": "https://chatgpt.com/cdn/assets/conversation-12345678.js", "method": "GET"},
    })
    assert cdp.send.call_args.args[0] == ("Fetch.failRequest" if created else "Fetch.continueRequest")
    for url in [
        "https://chatgpt.com/api/auth/callback/openai", "https://chatgpt.com/api/auth/session",
        "https://chatgpt.com/backend-api/accounts/mfa/enroll",
        "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment",
        "https://sentinel.openai.com/sentinel/12345678/sdk.js",
    ]:
        cdp.reset_mock()
        transport._on_request_paused(cdp, {
            "requestId": "required", "resourceType": "Fetch",
            "request": {"url": url, "method": "GET"},
        })
        assert cdp.send.call_args.args[0] == "Fetch.continueRequest"


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 504])
def test_cdn_headers_alone_do_not_make_http_errors_challenges(status):
    from core.abais_protocol.protocol_register import _is_cloudflare_challenge_response
    result = response(status, {"error": {"code": "account_deactivated"}})
    result.headers.update({"server": "cloudflare", "cf-ray": "fixture", "content-type": "application/json"})
    assert _is_cloudflare_challenge_response(result) is False
    result.headers["cf-mitigated"] = "challenge"
    assert _is_cloudflare_challenge_response(result) is True


def test_totp_account_deactivation_is_terminal_even_behind_cdn(monkeypatch):
    from core.abais_protocol.protocol_register import ChatGPTProtocolRegister
    from core.abais_protocol.credential_checks import ChatGPTAccountBannedDuringRelogin
    from core.abais_protocol import mfa
    monkeypatch.setattr(mfa, "totp_code", lambda _: "123456")
    rejected = response(403, {"error": {"code": "account_deactivated", "message": "PRIVATE"}})
    rejected.headers.update({"server": "cloudflare", "cf-ray": "fixture"})
    session = Mock()
    session.post.return_value = rejected
    page = response(200)
    page.url = "https://auth.openai.com/mfa-challenge/fixture"
    page._content = b'<form action="/api/accounts/mfa/verify"><input name="code"></form>'
    worker = ChatGPTProtocolRegister(session=session)
    with pytest.raises(ChatGPTAccountBannedDuringRelogin) as error:
        worker._login_totp("fixture-secret", page)
    assert error.value.code == "account_deactivated"
    assert "PRIVATE" not in str(error.value)
    session.post.assert_called_once()


@pytest.fixture
def registration_worker():
    from core.abais_protocol.protocol_register import ChatGPTProtocolRegister
    worker = ChatGPTProtocolRegister(session=Mock(), otp_callback=Mock(return_value="123456"))
    worker._visit_auth_step = Mock()
    worker._register_password = Mock(return_value={
        "page": {"type": "email_otp_verification"}, "continue_url": "/email-verification",
    })
    worker._validate_otp = Mock(return_value={
        "page": {"type": "about_you"}, "continue_url": "/about-you",
    })
    worker._send_otp = Mock()
    worker._create_account = Mock(return_value={"continue_url": "/completed"})
    return worker


@pytest.mark.parametrize("authorization", [
    {"page": {"type": "add_phone"}},
    {"page_type": "add_phone", "continue_url": "/about-you"},
    {"page": {"type": "password"}, "continue_url": "/add-phone?state=PRIVATE"},
    {"continue_url": "https://auth.openai.com/add-phone/?state=PRIVATE"},
])
def test_required_phone_step_stops_before_any_mutation(registration_worker, authorization):
    worker = registration_worker
    with pytest.raises(RuntimeError, match="^stage=protocol_phone_verification_required$"):
        worker._create_account_from_authorization(
            email="u@example.test",
            password="fixture",
            name="User Example",
            birthdate="1990-01-01",
            authorization=authorization,
        )
    worker._register_password.assert_not_called()
    worker._visit_auth_step.assert_not_called()
    worker.otp_callback.assert_not_called()
    worker._create_account.assert_not_called()


def test_phone_step_after_email_verification_never_creates_account(registration_worker):
    worker = registration_worker
    worker._validate_otp.return_value = {"page": {"type": "add_phone"}}
    with pytest.raises(RuntimeError, match="protocol_phone_verification_required"):
        worker._create_account_from_authorization(
            email="u@example.test", password="fixture", name="User Example",
            birthdate="1990-01-01", authorization={"page": {"type": "password"}},
        )
    worker._register_password.assert_called_once()
    worker._validate_otp.assert_called_once_with("123456")
    worker._create_account.assert_not_called()


@pytest.mark.parametrize("authorization", [
    {"page": {"type": "login_password"}},
    {"page": {"type": "password"}, "continue_url": "/log-in/password?state=PRIVATE"},
    {"continue_url": "https://auth.openai.com/log-in/password/"},
])
def test_existing_account_login_never_sets_registration_password(registration_worker, authorization):
    worker = registration_worker
    with pytest.raises(RuntimeError, match="^stage=protocol_existing_account_login_required$"):
        worker._create_account_from_authorization(
            email="u@example.test",
            password="fixture",
            name="User Example",
            birthdate="1990-01-01",
            authorization=authorization,
        )
    worker._register_password.assert_not_called()
    worker._visit_auth_step.assert_not_called()
    worker.otp_callback.assert_not_called()
    worker._create_account.assert_not_called()


@pytest.mark.parametrize("next_step", [
    {},
    {"continue_url": "/error?next=/about-you"},
    {"continue_url": "/about-you-required-other-step"},
    {"page": {"type": "unsupported_step"}, "continue_url": "/about-you"},
])
def test_unconfirmed_profile_step_never_creates_account(registration_worker, next_step):
    worker = registration_worker
    worker._validate_otp.return_value = next_step
    with pytest.raises(RuntimeError, match="^stage=protocol_registration_step_unconfirmed$"):
        worker._create_account_from_authorization(
            email="u@example.test", password="fixture", name="User Example",
            birthdate="1990-01-01", authorization={"page": {"type": "password"}},
        )
    worker._create_account.assert_not_called()


def test_repeated_password_state_does_not_repeat_password_write(registration_worker):
    worker = registration_worker
    worker._register_password.return_value = {"page": {"type": "password"}}
    with pytest.raises(RuntimeError, match="^stage=protocol_password_step_not_advanced$"):
        worker._create_account_from_authorization(
            email="u@example.test", password="fixture", name="User Example",
            birthdate="1990-01-01", authorization={"page": {"type": "password"}},
        )
    worker._register_password.assert_called_once()
    worker.otp_callback.assert_not_called()
    worker._create_account.assert_not_called()


def test_confirmed_steps_create_once_without_duplicate_otp(registration_worker):
    worker = registration_worker
    result = worker._create_account_from_authorization(
        email="u@example.test", password="fixture", name="User Example",
        birthdate="1990-01-01", authorization={"page": {"type": "password"}},
    )
    assert result == {"continue_url": "/completed"}
    worker._register_password.assert_called_once()
    worker._validate_otp.assert_called_once_with("123456")
    worker._send_otp.assert_not_called()
    worker._create_account.assert_called_once_with("User Example", "1990-01-01")


def test_session_result_skips_codex_oauth_when_not_requested(monkeypatch):
    from core.abais_protocol import credential_checks
    from core.abais_protocol.protocol_register import ChatGPTProtocolRegister

    session = Mock()
    session.get.return_value = response(200, {
        "accessToken": "fixture-web-access-token",
        "account": {"id": "acct-fixture"},
        "sessionToken": "session-fixture",
    })
    session.cookies.get_dict.return_value = {"session": "fixture-cookie"}
    mint = Mock()
    monkeypatch.setattr(credential_checks, "mint_chatgpt_refresh_token_from_session", mint)

    result = ChatGPTProtocolRegister(session=session)._session_result(
        "u@example.test", "fixture-password", recover_oauth_tokens=False,
    )

    assert result["access_token"] == "fixture-web-access-token"
    assert result["refresh_token"] == ""
    mint.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_standalone_session_keeps_oauth_recovery_independent_of_registration_switch(monkeypatch, enabled):
    from config import codex as codex_cfg
    from core.abais_protocol import credential_checks
    from core.abais_protocol.protocol_register import ChatGPTProtocolRegister

    session = Mock()
    session.get.return_value = response(200, {
        "accessToken": "fixture-web-access-token",
        "account": {"id": "acct-fixture"},
    })
    session.cookies.get_dict.return_value = {"session": "fixture-cookie"}
    mint = Mock(return_value={
        "state": "valid",
        "tokens": {
            "access_token": "fixture-oauth-access-token",
            "refresh_token": "fixture-refresh-token",
        },
    })
    monkeypatch.setattr(codex_cfg, "ENABLE_CODEX_AUTO", enabled)
    monkeypatch.setattr(credential_checks, "mint_chatgpt_refresh_token_from_session", mint)

    result = ChatGPTProtocolRegister(session=session)._session_result(
        "u@example.test", "fixture-password"
    )

    assert result["access_token"] == "fixture-oauth-access-token"
    assert result["refresh_token"] == "fixture-refresh-token"
    mint.assert_called_once()


@pytest.mark.parametrize("enabled", [False, True])
def test_registration_preserves_task_profile_and_existing_oauth_switch(registration_worker, monkeypatch, enabled):
    from config import codex as codex_cfg
    worker = registration_worker
    monkeypatch.setattr(codex_cfg, "ENABLE_CODEX_AUTO", enabled)
    worker._initialize_signup = Mock()
    worker._submit_signup_email = Mock(return_value={"page": {"type": "password"}})
    worker._session_result = Mock(return_value={"access_token": "fixture"})
    worker._finalize_registration_result = Mock(side_effect=lambda result: result)
    result = worker.run(email="u@example.test", password="fixture", name="Task Name", birthdate="1990-01-02")
    assert result["access_token"] == "fixture"
    worker._create_account.assert_called_once_with("Task Name", "1990-01-02")
    worker._session_result.assert_called_once_with("u@example.test", "fixture", recover_oauth_tokens=enabled)
