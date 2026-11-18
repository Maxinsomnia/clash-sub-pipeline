import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from core.kernel_tester import (
    kernel_probe_config,
    _build_config,
    _free_port,
    resolve_binary,
)
from core.parser import Node


class TestKernelProbeConfig(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = kernel_probe_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["timeout"], 6000)
        self.assertEqual(cfg["workers"], 40)
        self.assertEqual(cfg["max_nodes"], 400)
        self.assertTrue(cfg["drop_dead"])
        self.assertIn("generate_204", cfg["url"])

    def test_env_overrides(self):
        env = {
            "KERNEL_PROBE_ENABLED": "false",
            "KERNEL_PROBE_TIMEOUT": "3000",
            "KERNEL_PROBE_WORKERS": "12",
            "KERNEL_PROBE_MAX_NODES": "50",
            "KERNEL_PROBE_DROP_DEAD": "0",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = kernel_probe_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["timeout"], 3000)
        self.assertEqual(cfg["workers"], 12)
        self.assertEqual(cfg["max_nodes"], 50)
        self.assertFalse(cfg["drop_dead"])


class TestKernelProbeConfigBuilder(unittest.TestCase):
    def test_minimal_config_has_no_routing_rules(self):
        node = Node({"type": "vmess", "name": "test", "server": "1.2.3.4", "port": 443, "uuid": "x"})
        cfg = _build_config([node], 17890, 19090)
        self.assertEqual(cfg["mixed-port"], 17890)
        self.assertEqual(cfg["external-controller"], "127.0.0.1:19090")
        self.assertEqual(cfg["mode"], "direct")
        self.assertEqual(len(cfg["proxies"]), 1)
        # Direct routing ensures the delay API tests proxies themselves,
        # not a rule-selected chain.
        self.assertEqual(cfg["rules"], ["MATCH,DIRECT"])

    def test_yaml_serializable(self):
        node = Node({"type": "ss", "name": "🇭🇰 test", "server": "1.2.3.4", "port": 8388,
                     "cipher": "aes-128-gcm", "password": "pwd"})
        cfg = _build_config([node], 17890, 19090)
        text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
        self.assertIn("🇭🇰 test", text)
        self.assertIn("external-controller", text)


class TestKernelBinaryResolution(unittest.TestCase):
    def test_explicit_binary(self):
        with tempfile.NamedTemporaryFile() as tmp:
            with patch.dict(os.environ, {"MIHOMO_BIN": tmp.name}, clear=True):
                self.assertEqual(str(resolve_binary()), tmp.name)

    def test_missing_explicit_binary_raises(self):
        with patch.dict(os.environ, {"MIHOMO_BIN": "/nonexistent/kernel"}, clear=True):
            with self.assertRaises(RuntimeError):
                resolve_binary()

    def test_free_port_is_usable(self):
        import socket
        port = _free_port()
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
        finally:
            sock.close()
        self.assertGreater(port, 1024)


if __name__ == "__main__":
    unittest.main()
