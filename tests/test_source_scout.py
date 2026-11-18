import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location("source_scout", ROOT / "scripts" / "source_scout.py")
scout = importlib.util.module_from_spec(spec)
sys.modules["source_scout"] = scout
spec.loader.exec_module(scout)


class TestUrlNormalization(unittest.TestCase):
    def test_raw_4_segment(self):
        self.assertEqual(
            scout._normalize_repo_url(
                "https://raw.githubusercontent.com/Pawdroid/Free-servers/main/sub", ""),
            "https://raw.githubusercontent.com/Pawdroid/Free-servers/main/sub",
        )

    def test_raw_with_extra_segments_truncated(self):
        norm = scout._normalize_repo_url(
            "https://raw.githubusercontent.com/u/r/main/output/clash.yaml?x=1", "")
        self.assertEqual(norm, "https://raw.githubusercontent.com/u/r/main/output/clash.yaml")

    def test_blob_link(self):
        norm = scout._normalize_repo_url(
            "https://github.com/u/r/blob/main/sub", "")
        self.assertEqual(norm, "https://raw.githubusercontent.com/u/r/main/sub")

    def test_html_tail_stripped(self):
        norm = scout._normalize_repo_url(
            "https://raw.githubusercontent.com/Pawdroid/Free-servers/main/sub</a", "")
        self.assertEqual(norm, "https://raw.githubusercontent.com/Pawdroid/Free-servers/main/sub")

    def test_anchor_stripped(self):
        norm = scout._normalize_repo_url(
            "https://raw.githubusercontent.com/u/r/main/sub#readme", "")
        self.assertEqual(norm, "https://raw.githubusercontent.com/u/r/main/sub")


class TestSubscriptionUrlDetection(unittest.TestCase):
    def test_accepts_real_subscription_shapes(self):
        for url in (
            "https://raw.githubusercontent.com/Pawdroid/Free-servers/main/sub",
            "https://raw.githubusercontent.com/foo/bar/main/output/clash.yaml",
            "https://raw.githubusercontent.com/foo/bar/main/subs.txt",
            "https://raw.githubusercontent.com/Surfboardv2ray/TGParse/main/python/trojan",
        ):
            self.assertTrue(scout._is_subscription_url(url), url)

    def test_rejects_source_code_and_assets(self):
        for url in (
            "https://raw.githubusercontent.com/clash-verge/clash-verge/dev/tsconfig.node.json",
            "https://raw.githubusercontent.com/foo/bar/main/.editorconfig",
            "https://raw.githubusercontent.com/foo/bar/main/README.md",
            "https://raw.githubusercontent.com/foo/bar/main/config.go",
            "https://raw.githubusercontent.com/foo/bar/main/sub.png",
        ):
            self.assertFalse(scout._is_subscription_url(url), url)

    def test_repo_relevance_filter(self):
        self.assertTrue(scout.TOPIC_RE.search("free clash subscription nodes"))
        self.assertTrue(scout.TOPIC_RE.search("翻墙 节点 订阅"))
        self.assertFalse(scout.TOPIC_RE.search("reactjs interview questions"))
        self.assertFalse(scout.TOPIC_RE.search("public apis list"))


class TestCli(unittest.TestCase):
    def test_parser_has_all_commands(self):
        parser = scout.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])  # command is required
        for cmd in ("discover", "evaluate", "promote"):
            args = parser.parse_args([cmd])
            self.assertEqual(args.command, cmd)

    def test_evaluate_has_threshold_args(self):
        parser = scout.build_parser()
        args = parser.parse_args([
            "evaluate", "--min-nodes", "8", "--min-alive-rate", "0.5",
            "--max-candidates", "10",
        ])
        self.assertEqual(args.min_nodes, 8)
        self.assertAlmostEqual(args.min_alive_rate, 0.5)
        self.assertEqual(args.max_candidates, 10)


if __name__ == "__main__":
    unittest.main()
