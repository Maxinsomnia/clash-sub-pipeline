import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

# Load scripts/local_probe.py as a module (scripts/ is not a package).
_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_probe.py"
spec = importlib.util.spec_from_file_location("local_probe", _MODULE_PATH)
local_probe = importlib.util.module_from_spec(spec)
sys.modules["local_probe"] = local_probe
spec.loader.exec_module(local_probe)


def args():
    return SimpleNamespace(
        test_url="https://cp.cloudflare.com/generate_204",
        ai_url=local_probe.DEFAULT_AI_URL,
        skip_ai=False,
    )


def sample_config():
    """Minimal config mirroring the generator's group structure."""
    return {
        "proxies": [
            {"name": "node-alive-ai", "type": "ss", "server": "1.1.1.1", "port": 443,
             "cipher": "aes-128-gcm", "password": "p"},
            {"name": "node-alive-noai", "type": "ss", "server": "2.2.2.2", "port": 443,
             "cipher": "aes-128-gcm", "password": "p"},
            {"name": "node-dead", "type": "ss", "server": "3.3.3.3", "port": 443,
             "cipher": "aes-128-gcm", "password": "p"},
            {"name": "info", "type": "ss", "server": "127.0.0.1", "port": 10000,
             "cipher": "aes-128-gcm", "password": "p"},
        ],
        "proxy-groups": [
            {"name": "🚀 选择代理", "type": "select",
             "proxies": ["♻️ 自动选择", "✅ 手动选择", "🗺️ 选择地区", "DIRECT"]},
            {"name": "🗺️ 选择地区", "type": "select",
             "proxies": ["🇯🇵 日本", "🇺🇸 美国"]},
            {"name": "♻️ 自动选择", "type": "fallback",
             "url": "https://cp.cloudflare.com/generate_204", "lazy": True,
             "proxies": ["node-alive-ai", "node-dead"]},
            {"name": "✅ 手动选择", "type": "select",
             "proxies": ["node-alive-ai", "node-alive-noai", "node-dead"]},
            {"name": "🤖 AI 服务", "type": "select",
             "proxies": ["♻️ 自动选择", "✅ 手动选择"]},
            {"name": "⚡ 自动选择 | 🇯🇵 日本", "type": "fallback",
             "url": "https://cp.cloudflare.com/generate_204",
             "proxies": ["node-alive-ai"]},
            {"name": "🇯🇵 日本", "type": "select",
             "proxies": ["⚡ 自动选择 | 🇯🇵 日本", "node-alive-ai"]},
            {"name": "⚡ 自动选择 | 🇺🇸 美国", "type": "fallback",
             "url": "https://cp.cloudflare.com/generate_204",
             "proxies": ["node-dead"]},
            {"name": "🇺🇸 美国", "type": "select",
             "proxies": ["⚡ 自动选择 | 🇺🇸 美国", "node-dead"]},
        ],
        "rules": ["MATCH,🚀 选择代理"],
    }


class TestBuildCleanConfig(unittest.TestCase):
    def setUp(self):
        self.working = {"node-alive-ai": 100, "node-alive-noai": 200}
        self.ai = {"node-alive-ai": 120}

    def test_dead_nodes_removed_from_proxies(self):
        cleaned, stats = local_probe.build_clean_config(
            sample_config(), self.working, self.ai, args()
        )
        names = {p["name"] for p in cleaned["proxies"]}
        self.assertNotIn("node-dead", names)
        self.assertIn("node-alive-ai", names)
        self.assertIn("info", names)  # display-only node retained

    def test_empty_region_group_deleted(self):
        cleaned, stats = local_probe.build_clean_config(
            sample_config(), self.working, self.ai, args()
        )
        groups = {g["name"] for g in cleaned["proxy-groups"]}
        self.assertIn("🇯🇵 日本", groups)       # has a live node
        self.assertNotIn("🇺🇸 美国", groups)     # only had dead node
        self.assertIn("⚡ 自动选择 | 🇺🇸 美国", stats["deleted_groups"])

    def test_deleted_group_purged_from_menu(self):
        cleaned, _ = local_probe.build_clean_config(
            sample_config(), self.working, self.ai, args()
        )
        groups = {g["name"]: g for g in cleaned["proxy-groups"]}
        self.assertEqual(groups["🗺️ 选择地区"]["proxies"], ["🇯🇵 日本"])
        self.assertNotIn("🇺🇸 美国", groups["🚀 选择代理"]["proxies"] or [])

    def test_ai_and_normal_groups_created_and_wired(self):
        cleaned, stats = local_probe.build_clean_config(
            sample_config(), self.working, self.ai, args()
        )
        groups = {g["name"]: g for g in cleaned["proxy-groups"]}
        ai = groups["🤖 AI 可用节点"]
        normal = groups["🌐 普通节点"]
        self.assertEqual(ai["proxies"], ["node-alive-ai"])
        self.assertEqual(normal["proxies"], ["node-alive-noai"])
        self.assertEqual(ai["type"], "fallback")
        self.assertTrue(ai["lazy"])
        self.assertIn("🤖 AI 可用节点", groups["🚀 选择代理"]["proxies"])
        self.assertIn("🤖 AI 可用节点", groups["🤖 AI 服务"]["proxies"])
        self.assertEqual(stats["ai_nodes"], 1)
        self.assertEqual(stats["normal_nodes"], 1)

    def test_no_dangling_references(self):
        cleaned, _ = local_probe.build_clean_config(
            sample_config(), self.working, self.ai, args()
        )
        names = {p["name"] for p in cleaned["proxies"]}
        groups = {g["name"] for g in cleaned["proxy-groups"]}
        builtin = {"DIRECT", "REJECT", "PASS"}
        for g in cleaned["proxy-groups"]:
            for ref in (g.get("proxies") or []):
                self.assertTrue(
                    ref in names or ref in groups or ref in builtin,
                    f"dangling: {g['name']} -> {ref}",
                )

    def test_skip_ai_keeps_all_in_normal_group(self):
        a = args()
        a.skip_ai = True
        cleaned, stats = local_probe.build_clean_config(
            sample_config(), self.working, {}, a
        )
        groups = {g["name"]: g for g in cleaned["proxy-groups"]}
        self.assertEqual(set(groups["🌐 普通节点"]["proxies"]),
                         {"node-alive-ai", "node-alive-noai"})
        self.assertEqual(groups["🤖 AI 可用节点"]["proxies"], ["DIRECT"])

    def test_yaml_serializable(self):
        cleaned, _ = local_probe.build_clean_config(
            sample_config(), self.working, self.ai, args()
        )
        text = yaml.safe_dump(cleaned, allow_unicode=True, sort_keys=False)
        self.assertIn("🤖 AI 可用节点", text)


if __name__ == "__main__":
    unittest.main()
