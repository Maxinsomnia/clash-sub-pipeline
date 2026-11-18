import asyncio
import unittest
from unittest.mock import patch

from core.parser import Node
from core import tester


class FakeWriter:
    async def wait_closed(self):
        return None

    def close(self):
        return None


def make_node(name, server="1.2.3.4", port=443, node_type="ss", private=False):
    node = Node({"name": name, "server": server, "port": port, "type": node_type})
    if private:
        node.data["_is_private"] = True
    return node


class TestTester(unittest.TestCase):
    def test_env_helpers(self):
        with patch.dict("os.environ", {"SPEEDTEST_WORKERS": "0", "SPEEDTEST_ENABLED": "false"}):
            cfg = tester.speedtest_config()
            self.assertEqual(cfg["workers"], 1)
            self.assertFalse(cfg["enabled"])

    def test_is_exempt(self):
        self.assertTrue(tester.is_exempt(make_node("p", private=True)))
        self.assertTrue(tester.is_exempt(make_node("h", node_type="hysteria2")))
        self.assertFalse(tester.is_exempt(make_node("s")))

    def test_probe_marks_alive_and_latency(self):
        async def fake_open(host, port):
            return object(), FakeWriter()

        node = make_node("ok")
        with patch("asyncio.open_connection", side_effect=fake_open):
            ok = asyncio.run(tester._probe(node, 1.0, asyncio.Semaphore(10)))
        self.assertTrue(ok)
        self.assertTrue(node.data["_alive"])
        self.assertGreaterEqual(node.data["_latency_ms"], 0)

    def test_probe_marks_dead_on_failure(self):
        node = make_node("bad")
        with patch("asyncio.open_connection", side_effect=ConnectionRefusedError):
            ok = asyncio.run(tester._probe(node, 1.0, asyncio.Semaphore(10)))
        self.assertFalse(ok)
        self.assertFalse(node.data["_alive"])
        self.assertIsNone(node.data["_latency_ms"])

    def test_probe_nodes_async_drops_dead_and_sorts(self):
        nodes = [make_node("slow", "1.1.1.1"), make_node("fast", "2.2.2.2")]

        async def fake_open(host, port):
            await asyncio.sleep(0.03 if host == "1.1.1.1" else 0.001)
            return object(), FakeWriter()

        with patch("asyncio.open_connection", side_effect=fake_open):
            final, stats = asyncio.run(tester.probe_nodes_async(nodes, drop_dead=True))

        self.assertEqual(stats["alive"], 2)
        self.assertEqual(final[0].name, "fast")
        self.assertLess(final[0].data["_latency_ms"], final[1].data["_latency_ms"])

    def test_probe_nodes_async_keeps_dead_when_disabled(self):
        nodes = [make_node("bad", "1.1.1.1")]
        with patch("asyncio.open_connection", side_effect=ConnectionRefusedError):
            final, stats = asyncio.run(tester.probe_nodes_async(nodes, drop_dead=False))
        self.assertEqual(len(final), 1)
        self.assertEqual(stats["dropped"], 0)
        self.assertEqual(stats["dead"], 1)


if __name__ == "__main__":
    unittest.main()
