import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_probe", ROOT / "scripts" / "local_probe.py")
lp = importlib.util.module_from_spec(spec)
sys.modules["local_probe"] = lp
spec.loader.exec_module(lp)


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestGeminiDetection(unittest.TestCase):
    """Gemini usable = real page with enablement flag, NOT HTTP 200 alone.

    Google returns HTTP 403 for datacenter IPs and HTTP 200 for region/risk
    pages; Mihomo's delay API even counts 403 as 'success'. The body flag is
    the same method used by clash-verge-rev and subs-check.
    """

    def _run_with_body(self, body, status=200):
        with patch.object(lp.urllib.request, "build_opener") as build:
            opener = build.return_value
            opener.open.return_value = FakeResponse(body, status)
            return lp.test_gemini(7890, 5000)

    def test_accepts_new_escaped_flag(self):
        body = r'AF_initDataCallback ... \"45772048\":false,\"45631641\":true,\"45841483\":true ...'
        usable, latency = self._run_with_body(body)
        self.assertTrue(usable)
        self.assertIsInstance(latency, (int, float))

    def test_accepts_legacy_flag(self):
        usable, _ = self._run_with_body("xx 45631641,null,true yy")
        self.assertTrue(usable)

    def test_rejects_403_page(self):
        body = "<html>403. That's an error. Your client does not have permission to get URL /</html>"
        import urllib.error
        with patch.object(lp.urllib.request, "build_opener") as build:
            opener = build.return_value
            opener.open.side_effect = urllib.error.HTTPError(
                "https://gemini.google.com/", 403, "Forbidden", {}, None
            )
            usable, latency = lp.test_gemini(7890, 5000)
        self.assertFalse(usable)
        self.assertIsNone(latency)

    def test_rejects_200_without_flag(self):
        # Google can return 200 with a risk-control / unsupported region page.
        body = "<html><title>Google Gemini</title> ... no feature flag ... </html>"
        usable, _ = self._run_with_body(body, status=200)
        self.assertFalse(usable)

    def test_rejects_false_flag(self):
        body = r'\"45631641\":false'
        usable, _ = self._run_with_body(body)
        self.assertFalse(usable)

    def test_browser_ua_is_used(self):
        with patch.object(lp.urllib.request, "build_opener") as build:
            opener = build.return_value
            opener.open.return_value = FakeResponse(r'\"45631641\":true')
            lp.test_gemini(7890, 5000)
            request = opener.open.call_args[0][0]
            ua = request.headers.get("User-agent")
            self.assertIn("Mozilla", ua)

    def test_patterns_cover_both_formats(self):
        self.assertIn('45631641,null,true', lp.GEMINI_OK_PATTERNS)
        self.assertTrue(any('45631641' in p for p in lp.GEMINI_OK_PATTERNS))


if __name__ == "__main__":
    unittest.main()
