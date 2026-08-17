# -*- coding: utf-8 -*-
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core import codex_retry_service


class CodexRetryLoggingTests(unittest.TestCase):
    def test_same_named_overlapping_workers_write_each_record_once(self):
        barrier = threading.Barrier(2)
        emitted_logger = logging.getLogger("tests.codex_retry.concurrent")
        previous_level = emitted_logger.level
        emitted_logger.setLevel(logging.INFO)

        def run_codex_oauth(email: str, *, force: bool):
            self.assertTrue(force)
            barrier.wait(timeout=5)
            emitted_logger.info("并发接码日志 %s", email)
            barrier.wait(timeout=5)
            return {"status": "success", "ok": True}

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "codex-retry.log"
            errors = []

            def worker(email: str) -> None:
                try:
                    codex_retry_service.run_worker(
                        email,
                        clear_log=True,
                        target_log_path=log_path,
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            try:
                with patch("config.reload_all"), \
                     patch("core.codex_oauth.run_codex_oauth", side_effect=run_codex_oauth), \
                     patch.object(codex_retry_service.db, "update_account_codex_status"):
                    threads = [
                        threading.Thread(target=worker, args=("one@example.test",), name="codex-retry-same"),
                        threading.Thread(target=worker, args=("two@example.test",), name="codex-retry-same"),
                    ]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=10)

                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                text = log_path.read_text(encoding="utf-8")
                self.assertEqual(text.count("并发接码日志 one@example.test"), 1)
                self.assertEqual(text.count("并发接码日志 two@example.test"), 1)
            finally:
                emitted_logger.setLevel(previous_level)


if __name__ == "__main__":
    unittest.main()
