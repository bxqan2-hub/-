import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import MagicMock, call, patch

from config import proxy as proxy_config
from core.roxybrowser_client import RoxyBrowserClient


class RoxyProxyEnforcementTests(unittest.TestCase):
    def setUp(self):
        proxy_config.reset_registration_exit_ip_reservations(clear_history=True)

    def tearDown(self):
        proxy_config.reset_registration_exit_ip_reservations(clear_history=True)

    def test_profile_is_reported_immediately_after_creation(self):
        client = RoxyBrowserClient()
        reported = []
        with patch.object(client, "create_profile", return_value="created-profile"), \
             patch.object(client, "request", side_effect=RuntimeError("open failed")), \
             patch.object(client, "close_profile", return_value=True) as close_profile, \
             patch.object(client, "delete_profile", return_value=True) as delete_profile:
            with self.assertRaisesRegex(RuntimeError, "open failed"):
                client.open_profile(on_profile_ready=reported.append)
        self.assertEqual(reported, ["created-profile"])
        close_profile.assert_called_once_with("created-profile")
        delete_profile.assert_called_once_with("created-profile")

    def test_registration_entry_refuses_profile_reuse_mode(self):
        # The public registration function is covered in the registration
        # module; this client-level test documents that generic lifecycle
        # helpers may still be used for maintenance only.
        from core import roxy_registration

        with patch.object(roxy_registration._cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False):
            result = roxy_registration.run_roxy_registration(
                "mail@example.test", "Test User", "1990-01-01"
            )
        self.assertFalse(result["success"])
        self.assertIn("ROXY_ONE_PROFILE_PER_ACCOUNT=True", result["error"])

    def test_profile_binding_failure_reclaims_new_profile_before_open(self):
        client = RoxyBrowserClient()
        with patch.object(client, "create_profile", return_value="created-profile"), \
             patch.object(client, "request") as request, \
             patch.object(client, "close_profile", return_value=True) as close_profile, \
             patch.object(client, "delete_profile", return_value=True) as delete_profile:
            with self.assertRaisesRegex(RuntimeError, "bind failed"):
                client.open_profile(on_profile_ready=MagicMock(side_effect=RuntimeError("bind failed")))

        request.assert_not_called()
        close_profile.assert_called_once_with("created-profile")
        delete_profile.assert_called_once_with("created-profile")

    def test_visible_window_opens_only_after_proxy_exit_ip_is_known(self):
        client = RoxyBrowserClient(profile_proxy="socks5h://proxy.example:1080")
        events = []
        with patch.object(client, "create_profile", side_effect=lambda: events.append("create") or "created-profile"), \
             patch("core.browser_exit_geo.probe_proxy_exit_geo", side_effect=lambda *_a, **_k: events.append("probe") or {"ip": "203.0.113.8", "country": "JP"}), \
             patch.object(client, "request", side_effect=lambda *_a, **_k: events.append("open") or {"data": {"dirId": "created-profile", "http": "127.0.0.1:9222"}}):
            opened = client.open_profile(require_proxy_exit_ip=True)
        self.assertEqual(events, ["probe", "create", "open"])
        self.assertEqual(opened.preflight_exit_geo["ip"], "203.0.113.8")

    def test_local_mode_creates_and_opens_without_proxy_preflight(self):
        client = RoxyBrowserClient(profile_proxy="http://127.0.0.1:10808")
        events = []
        with patch.object(client, "create_profile", side_effect=lambda: events.append("create") or "created-profile"), \
             patch("core.browser_exit_geo.probe_proxy_exit_geo") as probe_proxy_exit_geo, \
             patch.object(client, "request", side_effect=lambda *_a, **_k: events.append("open") or {"data": {"dirId": "created-profile", "http": "127.0.0.1:9222"}}):
            opened = client.open_profile(require_proxy_exit_ip=False)
        self.assertEqual(events, ["create", "open"])
        self.assertEqual(opened.preflight_exit_geo, {})
        probe_proxy_exit_geo.assert_not_called()

    def test_failed_proxy_exit_ip_probe_never_creates_or_opens_profile(self):
        client = RoxyBrowserClient(profile_proxy="socks5h://proxy.example:1080")
        with patch.object(client, "create_profile") as create_profile, \
             patch("core.browser_exit_geo.probe_proxy_exit_geo", return_value={}), \
             patch.object(client, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "快速检测失败"):
                client.open_profile(require_proxy_exit_ip=True)
        create_profile.assert_not_called()
        request.assert_not_called()

    def test_pool_proxy_is_resolved_and_probed_before_profile_creation(self):
        client = RoxyBrowserClient()
        events = []
        with patch("core.roxybrowser_client._cfg.ROXY_CREATE_USE_PROXY_POOL", True), \
             patch("config.proxy.pick_proxy", side_effect=lambda: events.append("pick") or "socks5h://proxy.example:1080"), \
             patch("core.browser_exit_geo.probe_proxy_exit_geo", side_effect=lambda *_a, **_k: events.append("probe") or {"ip": "203.0.113.9"}), \
             patch.object(client, "create_profile", side_effect=lambda: events.append("create") or "created-profile"), \
             patch.object(client, "request", side_effect=lambda *_a, **_k: events.append("open") or {"data": {"dirId": "created-profile", "http": "127.0.0.1:9222"}}):
            client.open_profile(require_proxy_exit_ip=True)
        self.assertEqual(events, ["pick", "probe", "create", "open"])

    def test_failed_pool_proxy_rotates_before_profile_creation(self):
        client = RoxyBrowserClient()
        first = "socks5h://first.example:1080"
        second = "socks5h://second.example:1080"
        with patch("core.roxybrowser_client._cfg.ROXY_CREATE_USE_PROXY_POOL", True), \
             patch("core.roxybrowser_client._cfg.ROXY_PROXY_PREFLIGHT_PROXY_ATTEMPTS", 3), \
             patch("config.proxy.pick_proxy", side_effect=[first, second]) as pick_proxy, \
             patch("core.browser_exit_geo.probe_proxy_exit_geo", side_effect=[{}, {"ip": "203.0.113.10"}]) as probe, \
             patch.object(client, "create_profile", return_value="created-profile"), \
             patch.object(client, "request", return_value={"data": {"dirId": "created-profile", "http": "127.0.0.1:9222"}}):
            opened = client.open_profile(require_proxy_exit_ip=True)

        self.assertEqual(opened.preflight_exit_geo["ip"], "203.0.113.10")
        self.assertEqual(probe.call_args_list[0].args[0], first)
        self.assertEqual(probe.call_args_list[1].args[0], second)
        self.assertEqual(pick_proxy.call_args_list[1].kwargs["excluded"], {first})

    def test_duplicate_preflight_exit_ip_rotates_to_a_new_pool_node(self):
        client = RoxyBrowserClient()
        first = "socks5h://first.example:1080"
        second = "socks5h://second.example:1080"
        with patch("core.roxybrowser_client._cfg.ROXY_CREATE_USE_PROXY_POOL", True), \
             patch("core.roxybrowser_client._cfg.ROXY_PROXY_PREFLIGHT_PROXY_ATTEMPTS", 3), \
             patch("config.proxy.pick_proxy", side_effect=[first, second]) as pick_proxy, \
             patch("core.browser_exit_geo.probe_proxy_exit_geo", side_effect=[
                 {"ip": "203.0.113.20", "country": "JP"},
                 {"ip": "203.0.113.21", "country": "JP"},
             ]) as probe, \
             patch.object(client, "create_profile", return_value="created-profile"), \
             patch.object(client, "request", return_value={"data": {"dirId": "created-profile", "http": "127.0.0.1:9222"}}):
            # Occupy the first observed address as if another registration
            # worker won the race just before this probe.
            self.assertTrue(proxy_config.reserve_registration_exit_ip("203.0.113.20", "other-worker"))
            opened = client.open_profile(require_proxy_exit_ip=True)

        self.assertEqual(opened.preflight_exit_geo["ip"], "203.0.113.21")
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(pick_proxy.call_args_list[1].kwargs["excluded"], {first})
        client.cleanup_profile(opened)

    def test_duplicate_explicit_exit_ip_fails_closed_before_create(self):
        client = RoxyBrowserClient(profile_proxy="socks5h://fixed.example:1080")
        self.assertTrue(proxy_config.reserve_registration_exit_ip("203.0.113.22", "other-worker"))
        with patch("core.browser_exit_geo.probe_proxy_exit_geo", return_value={"ip": "203.0.113.22", "country": "JP"}), \
             patch.object(client, "create_profile") as create_profile:
            with self.assertRaisesRegex(RuntimeError, "并发注册任务重复"):
                client.open_profile(require_proxy_exit_ip=True)
        create_profile.assert_not_called()

    def test_invalid_preflight_ip_is_not_misreported_as_duplicate(self):
        client = RoxyBrowserClient(profile_proxy="socks5h://fixed.example:1080")
        with patch("core.browser_exit_geo.probe_proxy_exit_geo", return_value={"ip": "not-an-ip"}), \
             patch.object(client, "create_profile") as create_profile:
            with self.assertRaisesRegex(RuntimeError, "快速检测失败") as raised:
                client.open_profile(require_proxy_exit_ip=True)
        self.assertNotIn("并发注册任务重复", str(raised.exception))
        create_profile.assert_not_called()

    def test_released_exit_ip_is_not_immediately_reused(self):
        self.assertTrue(proxy_config.reserve_registration_exit_ip("2001:0db8::1", "owner-a"))
        self.assertTrue(proxy_config.release_registration_exit_ip("2001:db8:0:0:0:0:0:1", "owner-a"))
        self.assertFalse(proxy_config.reserve_registration_exit_ip("2001:db8::1", "owner-b"))
        with patch.object(proxy_config, "_REGISTRATION_EXIT_IP_REUSE_COOLDOWN_SECONDS", 0):
            self.assertTrue(proxy_config.reserve_registration_exit_ip("2001:db8::1", "owner-b"))

    def test_keep_open_profile_retains_exit_ip_reservation(self):
        client = RoxyBrowserClient(profile_proxy="socks5h://keep.example:1080")
        with patch("core.roxybrowser_client._cfg.ROXY_KEEP_BROWSER_OPEN", True), \
             patch("core.browser_exit_geo.probe_proxy_exit_geo", return_value={"ip": "203.0.113.23", "country": "JP"}), \
             patch.object(client, "create_profile", return_value="keep-profile"), \
             patch.object(client, "request", return_value={"data": {"dirId": "keep-profile", "http": "127.0.0.1:9222"}}):
            opened = client.open_profile(require_proxy_exit_ip=True)

        self.assertTrue(opened.keep_open)
        client.cleanup_profile(opened)
        self.assertFalse(proxy_config.reserve_registration_exit_ip("203.0.113.23", "other-worker"))
        # Explicitly close the retained reservation in the test, mirroring a
        # later manual profile close/restart in the service.
        client._release_exit_ip_reservation()

    def _config_patches(self):
        return (
            patch("core.roxybrowser_client._cfg.ROXY_CREATE_USE_PROXY_POOL", True),
            patch("core.roxybrowser_client._cfg.ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False),
            patch("core.roxybrowser_client._cfg.ROXY_RANDOM_OS_ON_CREATE", False),
            patch("core.roxybrowser_client._cfg.ROXY_WORKSPACE_ID", "123"),
            patch("core.roxybrowser_client._cfg.ROXY_PROJECT_ID", "456"),
        )

    def test_pool_proxy_overrides_template_and_call_payload(self):
        client = RoxyBrowserClient()
        stale = {
            "proxyMethod": "custom",
            "protocol": "HTTP",
            "host": "non-jp.example",
            "port": "8080",
        }
        with ExitStack() as stack:
            for config_patch in self._config_patches():
                stack.enter_context(config_patch)
            stack.enter_context(patch("config.proxy.PROXY_API_ENABLED", True))
            stack.enter_context(patch("config.proxy.pick_proxy", return_value="socks5h://jp-gateway.example:16601"))
            request = stack.enter_context(
                patch.object(client, "request", return_value={"data": {"dirId": 789}})
            )
            profile_id = client.create_profile({"proxyInfo": stale})

        self.assertEqual(profile_id, "789")
        body = request.call_args.kwargs["json_body"]
        self.assertEqual(body["proxyInfo"]["host"], "jp-gateway.example")
        self.assertEqual(body["proxyInfo"]["port"], "16601")
        self.assertEqual(body["proxyInfo"]["protocol"], "SOCKS5")

    def test_missing_pool_proxy_aborts_before_roxy_create(self):
        client = RoxyBrowserClient()
        with ExitStack() as stack:
            for config_patch in self._config_patches():
                stack.enter_context(config_patch)
            stack.enter_context(patch("config.proxy.PROXY_API_ENABLED", False))
            stack.enter_context(patch("config.proxy.pick_proxy", return_value=""))
            request = stack.enter_context(patch.object(client, "request"))
            with self.assertRaisesRegex(RuntimeError, "未取得可用代理"):
                client.create_profile()

        request.assert_not_called()

    def test_explicit_local_proxy_bypasses_proxy_api(self):
        client = RoxyBrowserClient(profile_proxy="http://127.0.0.1:10808")
        with ExitStack() as stack:
            for config_patch in self._config_patches():
                stack.enter_context(config_patch)
            stack.enter_context(patch("config.proxy.PROXY_API_ENABLED", True))
            pick_proxy = stack.enter_context(patch("config.proxy.pick_proxy"))
            request = stack.enter_context(
                patch.object(client, "request", return_value={"data": {"dirId": 790}})
            )
            profile_id = client.create_profile()

        self.assertEqual(profile_id, "790")
        pick_proxy.assert_not_called()
        body = request.call_args.kwargs["json_body"]
        self.assertEqual(body["proxyInfo"]["host"], "127.0.0.1")
        self.assertEqual(body["proxyInfo"]["port"], "10808")

    def test_fixed_windows_overrides_template_and_call_payload(self):
        client = RoxyBrowserClient(profile_proxy="http://127.0.0.1:10808")
        with ExitStack() as stack:
            for config_patch in self._config_patches():
                stack.enter_context(config_patch)
            stack.enter_context(patch("core.roxybrowser_client._cfg.ROXY_DEFAULT_OS", "Windows"))
            stack.enter_context(patch("core.roxybrowser_client._cfg.ROXY_DEFAULT_OS_VERSION", ""))
            request = stack.enter_context(
                patch.object(client, "request", return_value={"data": {"dirId": 791}})
            )
            profile_id = client.create_profile({"os": "macOS", "osVersion": "15.3.2"})

        self.assertEqual(profile_id, "791")
        body = request.call_args.kwargs["json_body"]
        self.assertEqual(body["os"], "Windows")
        self.assertNotIn("osVersion", body)

    def test_profile_create_always_requests_fresh_random_fingerprint(self):
        client = RoxyBrowserClient(profile_proxy="http://127.0.0.1:10808")
        with ExitStack() as stack:
            for config_patch in self._config_patches():
                stack.enter_context(config_patch)
            stack.enter_context(
                patch(
                    "core.roxybrowser_client._cfg.ROXY_PROFILE_CREATE_PAYLOAD",
                    {"randomFingerprint": False},
                )
            )
            request = stack.enter_context(
                patch.object(client, "request", return_value={"data": {"dirId": "792"}})
            )
            profile_id = client.create_profile({"randomFingerprint": False})

        self.assertEqual(profile_id, "792")
        body = request.call_args.kwargs["json_body"]
        self.assertIs(body["randomFingerprint"], True)
        self.assertEqual(client._last_profile_create_summary["fingerprint_requested"], True)

    def test_create_retries_only_explicit_roxy_busy_response(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50100")
        busy = MagicMock(status_code=200, text='{"code":1,"msg":"正在创建中，请稍等！"}')
        busy.json.return_value = {"code": 1, "msg": "正在创建中，请稍等！"}
        success = MagicMock(status_code=200, text='{"code":0}')
        success.json.return_value = {"code": 0, "data": {"dirId": "ok"}}
        with patch.object(client.http, "request", side_effect=[busy, success]) as request, \
             patch("core.roxybrowser_client.time.sleep"):
            result = client.request("POST", "/browser/create", json_body={})
        self.assertEqual(result["data"]["dirId"], "ok")
        self.assertEqual(request.call_count, 2)

    def test_create_does_not_repeat_fixed_fingerprint_15000ms_failure(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50100")
        fingerprint_timeout = MagicMock(
            status_code=200,
            text='{"code":1,"msg":"timeout of 15000ms exceeded"}',
        )
        fingerprint_timeout.json.return_value = {
            "code": 1,
            "msg": "timeout of 15000ms exceeded",
        }
        with (
            patch("core.roxybrowser_client._cfg.ROXY_CREATE_API_ATTEMPTS", 3),
            patch.object(client.http, "request", return_value=fingerprint_timeout) as request,
            patch("core.roxybrowser_client.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "timeout of 15000ms exceeded"):
                client.request("POST", "/browser/create", json_body={})

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(request.call_args.kwargs["timeout"], 45)

    def test_create_attempts_are_bounded_separately_from_lifecycle_retries(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50100")
        busy = MagicMock(
            status_code=200,
            text='{"code":1,"msg":"正在创建中，请稍等！"}',
        )
        busy.json.return_value = {
            "code": 1,
            "msg": "正在创建中，请稍等！",
        }

        with (
            patch("core.roxybrowser_client._cfg.ROXY_CREATE_API_ATTEMPTS", 2),
            patch("core.roxybrowser_client._cfg.ROXY_API_RETRIES", 5),
            patch.object(client.http, "request", return_value=busy) as request,
            patch("core.roxybrowser_client.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "正在创建中"):
                client.request("POST", "/browser/create", json_body={})

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_create_does_not_retry_other_roxy_business_timeout(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50100")
        other_timeout = MagicMock(
            status_code=200,
            text='{"code":1,"msg":"timeout of 10000ms exceeded"}',
        )
        other_timeout.json.return_value = {
            "code": 1,
            "msg": "timeout of 10000ms exceeded",
        }

        with (
            patch.object(client.http, "request", return_value=other_timeout) as request,
            patch("core.roxybrowser_client.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "timeout of 10000ms exceeded"):
                client.request("POST", "/browser/create", json_body={})

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_create_timeout_is_not_retried(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50100")
        with patch.object(client.http, "request", side_effect=TimeoutError("create timed out")) as request, \
             patch("core.roxybrowser_client.time.sleep"):
            with self.assertRaises(TimeoutError):
                client.request("POST", "/browser/create", json_body={})
        self.assertEqual(request.call_count, 1)

    def test_create_uses_dedicated_long_timeout_but_other_lifecycle_keeps_short_budget(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50100")
        response = MagicMock(status_code=200, text='{"code":0}')
        response.json.return_value = {"code": 0}
        with patch.object(client.http, "request", return_value=response) as request:
            client.request("POST", "/browser/open", json_body={})
        self.assertEqual(request.call_args.kwargs["timeout"], 15)

    def test_profile_create_calls_are_serialized(self):
        guard = threading.Lock()
        active = 0
        max_active = 0
        sequence = 0

        def fake_request(*_args, **_kwargs):
            nonlocal active, max_active, sequence
            with guard:
                active += 1
                max_active = max(max_active, active)
                sequence += 1
                profile_id = str(sequence)
            time.sleep(0.03)
            with guard:
                active -= 1
            return {"data": {"dirId": profile_id}}

        clients = [RoxyBrowserClient(), RoxyBrowserClient()]
        with ExitStack() as stack:
            for config_patch in self._config_patches():
                stack.enter_context(config_patch)
            stack.enter_context(patch("config.proxy.PROXY_API_ENABLED", True))
            stack.enter_context(patch("config.proxy.pick_proxy", return_value="socks5h://jp-gateway.example:16601"))
            for client in clients:
                stack.enter_context(patch.object(client, "request", side_effect=fake_request))
            with ThreadPoolExecutor(max_workers=2) as pool:
                profile_ids = list(pool.map(lambda client: client.create_profile(), clients))

        self.assertEqual(sorted(profile_ids), ["1", "2"])
        self.assertEqual(max_active, 1)

    def test_open_and_close_lifecycle_calls_share_one_local_api_lane(self):
        guard = threading.Lock()
        active = 0
        max_active = 0

        def fake_request(*_args, **_kwargs):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            response = MagicMock(status_code=200, text='{"code":0}')
            response.json.return_value = {"code": 0, "data": {}}
            return response

        clients = [RoxyBrowserClient(), RoxyBrowserClient()]
        with patch.object(clients[0].http, "request", side_effect=fake_request), \
             patch.object(clients[1].http, "request", side_effect=fake_request), \
             ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(clients[0].request, "POST", "/browser/open", json_body={}),
                pool.submit(clients[1].request, "POST", "/browser/close", json_body={}),
            ]
            [future.result() for future in futures]

        self.assertEqual(max_active, 1)

    def test_delete_missing_profile_is_idempotent_success(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50100")
        error = RuntimeError("Roxy API 返回失败 POST /browser/delete: 窗口/数据不存在，请刷新页面后重试")
        with patch.object(client, "request", side_effect=error) as request:
            deleted = client.delete_profile("missing-profile")

        self.assertTrue(deleted)
        request.assert_called_once()

    def test_delete_other_error_remains_failure(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50100")
        with patch.object(client, "request", side_effect=RuntimeError("permission denied")):
            deleted = client.delete_profile("protected-profile")

        self.assertFalse(deleted)


if __name__ == "__main__":
    unittest.main()
