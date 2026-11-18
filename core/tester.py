"""
core/tester.py - Concurrent TCP connectivity probe and latency scoring.

Borrowed the probing idea from subs-check / clash_node_update, but operates
directly on MetaFetch Node objects:

- writes back ``_latency_ms`` (milliseconds) and ``_alive`` on each node
- Hysteria2 (UDP-only) and private nodes are exempt from TCP probing
- concurrency is bounded by a semaphore; a hard cap avoids CI timeouts
- dead nodes are dropped by default, but can be kept via SPEEDTEST_DROP_DEAD=false

NOTE: GitHub-hosted runners are mostly overseas. A low TCP latency here does
not guarantee the same result from mainland China. Treat this as a *relative*
signal for ranking, not an absolute availability verdict.
"""

import asyncio
import os
import time
from typing import List, Tuple

from core.parser import Node
from utils.logger import logger


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.2, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def speedtest_config() -> dict:
    """Read all speed-test knobs from environment variables."""
    return {
        "enabled": _env_bool("SPEEDTEST_ENABLED", True),
        "timeout": _env_float("SPEEDTEST_TIMEOUT", 3.0),
        "workers": _env_int("SPEEDTEST_WORKERS", 120),
        "max_nodes": _env_int("SPEEDTEST_MAX_NODES", 2000),
        "drop_dead": _env_bool("SPEEDTEST_DROP_DEAD", True),
    }


def is_exempt(node: Node) -> bool:
    """Nodes that must not be judged by a TCP connect."""
    if node.data.get("_is_private"):
        return True
    if node.type in ("hysteria2", "hy2"):
        return True
    return False


async def _probe(node: Node, timeout: float, sem: asyncio.Semaphore) -> bool:
    """Probe one node with a TCP connect; return True when reachable."""
    server = str(node.data.get("server", "") or "").strip()
    port = node.data.get("port")
    if not server or port is None:
        node.data["_latency_ms"] = None
        node.data["_alive"] = False
        return False

    try:
        port_int = int(port)
    except (TypeError, ValueError):
        node.data["_latency_ms"] = None
        node.data["_alive"] = False
        return False

    async with sem:
        start = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(server, port_int), timeout=timeout
            )
            latency_ms = (time.monotonic() - start) * 1000.0
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            node.data["_latency_ms"] = round(latency_ms, 1)
            node.data["_alive"] = True
            return True
        except Exception:
            node.data["_latency_ms"] = None
            node.data["_alive"] = False
            return False


async def probe_nodes_async(
    nodes: List[Node],
    timeout: float = 3.0,
    workers: int = 120,
    max_nodes: int = 2000,
    drop_dead: bool = True,
) -> Tuple[List[Node], dict]:
    """Probe nodes concurrently, sort survivors by latency, return final list."""
    exempt = [n for n in nodes if is_exempt(n)]
    candidates = [n for n in nodes if not is_exempt(n)]

    untouched: List[Node] = []
    if len(candidates) > max_nodes:
        untouched = candidates[max_nodes:]
        candidates = candidates[:max_nodes]
        for node in untouched:
            node.data["_latency_ms"] = None
            node.data["_alive"] = True

    sem = asyncio.Semaphore(workers)
    outcomes = await asyncio.gather(
        *(_probe(n, timeout, sem) for n in candidates), return_exceptions=True
    )
    for node, outcome in zip(candidates, outcomes):
        if isinstance(outcome, BaseException):
            node.data["_latency_ms"] = None
            node.data["_alive"] = False

    alive = [n for n in candidates if n.data.get("_alive")]
    dead = [n for n in candidates if not n.data.get("_alive")]
    alive.sort(key=lambda n: n.data.get("_latency_ms") or float("inf"))

    final = exempt + alive + untouched + ([] if drop_dead else dead)
    stats = {
        "tested": len(candidates),
        "alive": len(alive),
        "dead": len(dead),
        "exempt": len(exempt),
        "untouched": len(untouched),
        "dropped": len(dead) if drop_dead else 0,
        "fastest_ms": alive[0].data.get("_latency_ms") if alive else None,
    }
    return final, stats


def probe_nodes(nodes: List[Node], config: dict = None) -> Tuple[List[Node], dict]:
    """Synchronous wrapper used by main.py."""
    cfg = config or speedtest_config()
    if not cfg.get("enabled", True):
        for node in nodes:
            node.data.setdefault("_latency_ms", None)
            node.data.setdefault("_alive", True)
        return nodes, {"skipped": True, "tested": 0}

    started = time.time()
    final, stats = asyncio.run(
        probe_nodes_async(
            nodes,
            timeout=cfg["timeout"],
            workers=cfg["workers"],
            max_nodes=cfg["max_nodes"],
            drop_dead=cfg["drop_dead"],
        )
    )
    stats["elapsed"] = round(time.time() - started, 1)
    logger.info(
        "Speedtest: tested=%s alive=%s dead=%s exempt=%s skipped=%s dropped=%s "
        "fastest=%sms elapsed=%ss",
        stats["tested"], stats["alive"], stats["dead"], stats["exempt"],
        stats["untouched"], stats["dropped"], stats["fastest_ms"], stats["elapsed"],
    )
    return final, stats
