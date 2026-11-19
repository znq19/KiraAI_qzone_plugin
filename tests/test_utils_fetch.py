"""下载层：4xx 不重试、大小上限、失败日志去重、SSRF 校验、本地读盘下沉线程。"""
import asyncio
import logging
import os
import tempfile
import unittest

import _bootstrap as B

utils = B.qzone_utils

JPEG = b"\xff\xd8\xff" + b"\x00" * 64
HTML = b"<html><body>expired</body></html>"


class TestFetchPolicy(unittest.TestCase):
    def setUp(self):
        utils.reset_fail_log()

    def test_http_400_no_retry(self):
        session = B.patch_shared_session([B.FakeResponse(400, HTML)] * 5)
        res = B.run(utils.fetch_bytes("https://x/a.jpg"))
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 400)
        self.assertEqual(len(session.calls), 1, "400 不应重试")

    def test_http_404_no_retry(self):
        session = B.patch_shared_session([B.FakeResponse(404)] * 5)
        B.run(utils.fetch_bytes("https://x/a.jpg"))
        self.assertEqual(len(session.calls), 1)

    def test_5xx_retries_then_fails(self):
        session = B.patch_shared_session([B.FakeResponse(500)] * 5)
        res = B.run(utils.fetch_bytes("https://x/a.jpg", max_retries=2))
        self.assertFalse(res.ok)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(res.retryable)

    def test_200_ok(self):
        B.patch_shared_session([B.FakeResponse(200, JPEG)])
        res = B.run(utils.fetch_bytes("https://x/a.jpg"))
        self.assertTrue(res.ok)
        self.assertEqual(res.data, JPEG)

    def test_error_page_rejected(self):
        B.patch_shared_session([B.FakeResponse(200, HTML)])
        res = B.run(utils.fetch_bytes("https://x/a.jpg"))
        self.assertFalse(res.ok)
        self.assertIn("不是图片", res.reason)

    def test_size_cap(self):
        big = JPEG + b"\x00" * (2 * 1024 * 1024)
        B.patch_shared_session([B.FakeResponse(200, big)])
        res = B.run(utils.fetch_bytes("https://x/a.jpg", max_bytes=1024))
        self.assertFalse(res.ok)
        self.assertIn("大小上限", res.reason)

    def test_failure_log_deduped(self):
        """同一 URL 连续失败：只打一条可见日志，其余降级 debug。"""
        B.patch_shared_session([B.FakeResponse(400)] * 4)
        with self.assertLogs(utils.logger, level='DEBUG') as ctx:
            B.run(utils.fetch_bytes("https://x/same.jpg"))
            B.run(utils.fetch_bytes("https://x/same.jpg"))
            B.run(utils.fetch_bytes("https://x/same.jpg"))
        visible = [r for r in ctx.records if r.levelno >= logging.INFO]
        self.assertEqual(len(visible), 1, f"应只打一条可见日志，实际 {len(visible)}")
        self.assertIn("静默", visible[0].getMessage())

    def test_different_urls_logged_separately(self):
        B.patch_shared_session([B.FakeResponse(400)] * 4)
        with self.assertLogs(utils.logger, level='DEBUG') as ctx:
            B.run(utils.fetch_bytes("https://x/a.jpg"))
            B.run(utils.fetch_bytes("https://x/b.jpg"))
        visible = [r for r in ctx.records if r.levelno >= logging.INFO]
        self.assertEqual(len(visible), 2)


class TestUrlSafety(unittest.TestCase):
    def test_internal_blocked(self):
        for url in ("http://127.0.0.1/a.jpg", "http://localhost/a.jpg",
                    "http://10.0.0.5/a.jpg", "http://192.168.1.9/a.jpg",
                    "http://172.16.0.3/a.jpg", "http://169.254.1.1/a.jpg"):
            self.assertFalse(utils.is_safe_public_url(url), url)

    def test_public_allowed(self):
        for url in ("https://multimedia.nt.qq.com.cn/download?x=1",
                    "http://8.8.8.8/a.jpg", "https://example.com/a.jpg"):
            self.assertTrue(utils.is_safe_public_url(url), url)

    def test_local_path_allowed(self):
        self.assertTrue(utils.is_safe_public_url("data/temp/x.jpg"))
        self.assertTrue(utils.is_safe_public_url(""))


class TestNormalizeImages(unittest.TestCase):
    def setUp(self):
        utils.reset_fail_log()

    def test_pairs_and_local_path(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(JPEG)
            path = f.name
        try:
            pairs = []
            errors = []
            out = B.run(utils.normalize_images([path], errors=errors, pairs=pairs))
            self.assertEqual(len(out), 1)
            self.assertEqual(pairs[0][0], path)
            self.assertEqual(pairs[0][1], JPEG)
            self.assertEqual(errors, [])
        finally:
            os.unlink(path)

    def test_reports_reason_on_failure(self):
        B.patch_shared_session([B.FakeResponse(400)])
        errors = []
        out = B.run(utils.normalize_images(["https://x/a.jpg"], errors=errors))
        self.assertEqual(out, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("图片下载失败", errors[0])

    def test_missing_local_file(self):
        errors = []
        out = B.run(utils.normalize_images(["/no/such/file.jpg"], errors=errors))
        self.assertEqual(out, [])
        self.assertIn("不存在", errors[0])


if __name__ == '__main__':
    unittest.main()
