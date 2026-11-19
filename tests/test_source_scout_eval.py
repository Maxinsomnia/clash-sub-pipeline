import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location(
    "scout_eval", ROOT / "scripts" / "source_scout_eval.py"
)
mod = importlib.util.module_from_spec(spec)
sys.modules["scout_eval"] = mod
spec.loader.exec_module(mod)


class TestMarkdownSummary(unittest.TestCase):
    def test_render_basic_table(self):
        data = {
            "generated_at": "2026-11-19T00:00:00+00:00",
            "environment": "github-actions-overseas",
            "environment_note": "Overseas runner; NOT mainland China.",
            "thresholds": {"min_nodes": 5, "min_alive_rate": 0.3},
            "summary": {"evaluated": 2, "qualified": 1},
            "reports": [
                {
                    "repo": "foo/bar",
                    "url": "https://raw.githubusercontent.com/foo/bar/main/sub",
                    "parsed": 20, "tested": 20, "alive": 10,
                    "alive_rate": 0.5, "fastest_ms": 120, "qualified": True,
                },
                {
                    "repo": "baz/qux", "url": "https://raw.githubusercontent.com/baz/qux/main/nope",
                    "parsed": 0, "tested": 0, "alive": 0, "alive_rate": 0.0,
                    "qualified": False, "error": "fetch failed",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "report.md"
            mod._write_markdown_summary(out, data)
            text = out.read_text(encoding="utf-8")
        self.assertIn("# 候选订阅源质量报告", text)
        self.assertIn("github-actions-overseas", text)
        self.assertIn("NOT mainland China", text)
        self.assertIn("✅ 达标", text)
        self.assertIn("⚠️ fetch failed", text)
        self.assertIn("| 20 | 10 | 50% | 120 |", text)
        self.assertIn("promote --approve", text)

    def test_pipes_in_repo_are_escaped(self):
        data = {
            "generated_at": "x", "environment": "local", "environment_note": "n",
            "thresholds": {"min_nodes": 1, "min_alive_rate": 0.0},
            "summary": {"evaluated": 1, "qualified": 0},
            "reports": [{
                "repo": "a|b", "url": "http://x", "parsed": 0, "tested": 0,
                "alive": 0, "alive_rate": 0.0, "qualified": False,
            }],
        }
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "report.md"
            mod._write_markdown_summary(out, data)
            text = out.read_text(encoding="utf-8")
        self.assertIn("a\\|b", text)


class TestKernelFallback(unittest.TestCase):
    def test_explicit_mihomo_bin_respected(self):
        with tempfile.NamedTemporaryFile() as tmp:
            import os
            old = os.environ.get("MIHOMO_BIN")
            os.environ["MIHOMO_BIN"] = tmp.name
            try:
                self.assertEqual(str(mod._find_kernel()), tmp.name)
            finally:
                if old is None:
                    os.environ.pop("MIHOMO_BIN", None)
                else:
                    os.environ["MIHOMO_BIN"] = old


if __name__ == "__main__":
    unittest.main()
