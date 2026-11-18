"""
core/kernel_tester.py - Real HTTP proxy probing through the Mihomo kernel.

A plain TCP connect only proves that a port is open. It does NOT prove that
the proxy can complete a protocol handshake, authenticate, and reach the
internet. This module starts a real Mihomo kernel with a minimal config and
uses its external controller to issue an actual HTTP request THROUGH each
proxy, then removes the nodes that fail.

Designed as a second-stage filter after the cheap TCP prefilter, but works
standalone when SPEEDTEST_ENABLED=false.

Environment knobs:
    KERNEL_PROBE_ENABLED   default true
    KERNEL_PROBE_TIMEOUT   default 6000  (milliseconds per node)
    KERNEL_PROBE_WORKERS   default 40
    KERNEL_PROBE_MAX_NODES default 400
    KERNEL_PROBE_DROP_DEAD default true
    KERNEL_PROBE_URL       default https://cp.cloudflare.com/generate_204
    MIHOMO_BIN             explicit kernel path (skips download)
"""

import concurrent.futures
import gzip
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Tuple

import yaml

from core.parser import Node
from core.tester import is_exempt
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


def kernel_probe_config() -> dict:
    return {
        "enabled": _env_bool("KERNEL_PROBE_ENABLED", True),
        "timeout": _env_int("KERNEL_PROBE_TIMEOUT", 6000),
        "workers": _env_int("KERNEL_PROBE_WORKERS", 40),
        "max_nodes": _env_int("KERNEL_PROBE_MAX_NODES", 400),
        "drop_dead": _env_bool("KERNEL_PROBE_DROP_DEAD", True),
        "url": os.getenv("KERNEL_PROBE_URL", "https://cp.cloudflare.com/generate_204"),
    }


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def resolve_binary() -> Path:
    """Use MIHOMO_BIN, the shared validator cache, or download the kernel."""
    explicit = os.getenv("MIHOMO_BIN")
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise RuntimeError(f"MIHOMO_BIN not found: {explicit}")
        return path

    cached = Path(tempfile.gettempdir()) / "mihomo-validate"
    if cached.exists() and os.access(cached, os.X_OK):
        return cached

    version = os.getenv("MIHOMO_VERSION", "v1.19.34")
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        asset = "mihomo-darwin-arm64" if machine in ("arm64", "aarch64") else "mihomo-darwin-amd64"
    else:
        asset = "mihomo-linux-amd64-compatible"
    url = f"https://github.com/MetaCubeX/mihomo/releases/download/{version}/{asset}-{version}.gz"

    logger.info("Downloading Mihomo kernel for real proxy probing ...")
    request = urllib.request.Request(url, headers={"User-Agent": "clash-sub-pipeline"})
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = response.read()
    decoded = gzip.decompress(payload)
    cached.write_bytes(decoded)
    cached.chmod(0o755)
    logger.info(f"Kernel ready: {cached} ({len(decoded)} bytes)")
    return cached


def _build_config(nodes: List[Node], mix_port: int, ctrl_port: int) -> dict:
    """Minimal config: enough for the controller to test each proxy directly."""
    return {
        "mixed-port": mix_port,
        "external-controller": f"127.0.0.1:{ctrl_port}",
        "mode": "direct",
        "log-level": "error",
        "proxies": [node.to_clash() for node in nodes],
        "rules": ["MATCH,DIRECT"],
    }


def _test_one(name: str, base_url: str, test_url: str, timeout_ms: int, opener):
    endpoint = (
        base_url + "/proxies/" + urllib.parse.quote(name, safe="")
        + "/delay?url=" + urllib.parse.quote(test_url, safe="")
        + f"&timeout={timeout_ms}"
    )
    try:
        with opener.open(endpoint, timeout=timeout_ms / 1000.0 + 6) as response:
            payload = json.loads(response.read())
        delay = payload.get("delay")
        if isinstance(delay, int) and delay > 0:
            return name, delay
    except Exception:
        pass
    return name, None


def probe_nodes_kernel(
    nodes: List[Node], config: dict = None
) -> Tuple[List[Node], dict]:
    """Filter nodes by performing a real HTTP request through each proxy."""
    cfg = config or kernel_probe_config()
    if not cfg.get("enabled", True):
        for node in nodes:
            node.data.setdefault("_kernel_alive", True)
        return nodes, {"skipped": True, "tested": 0}

    exempt = [node for node in nodes if is_exempt(node)]
    candidates = [node for node in nodes if not is_exempt(node)]
    untouched: List[Node] = []
    if len(candidates) > cfg["max_nodes"]:
        untouched = candidates[cfg["max_nodes"]:]
        candidates = candidates[:cfg["max_nodes"]]
        for node in untouched:
            node.data["_kernel_alive"] = True

    if not candidates:
        return nodes, {"tested": 0, "alive": 0, "skipped_empty": True}

    started = time.time()
    binary = resolve_binary()
    workdir = Path(tempfile.mkdtemp(prefix="mihomo-probe-"))
    mix_port, ctrl_port = _free_port(), _free_port()
    config_path = workdir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(_build_config(candidates, mix_port, ctrl_port), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    log_file = (workdir / "kernel.log").open("w")
    process = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    try:
        process = subprocess.Popen(
            [str(binary), "-d", str(workdir), "-f", str(config_path)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        base_url = f"http://127.0.0.1:{ctrl_port}"
        ready = False
        for _ in range(60):
            if process.poll() is not None:
                break
            try:
                opener.open(base_url + "/version", timeout=1).read()
                ready = True
                break
            except Exception:
                time.sleep(0.25)
        if not ready:
            detail = (workdir / "kernel.log").read_text(encoding="utf-8", errors="replace")[:600]
            raise RuntimeError(f"Mihomo kernel failed to start: {detail}")

        names = [node.data.get("name") for node in candidates]
        with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            results = list(
                pool.map(
                    lambda n: _test_one(n, base_url, cfg["url"], cfg["timeout"], opener),
                    names,
                )
            )
        delay_by_name = dict(results)

        alive: List[Node] = []
        dead: List[Node] = []
        for node in candidates:
            name = node.data.get("name")
            delay = delay_by_name.get(name)
            if delay is not None:
                node.data["_kernel_alive"] = True
                node.data["_latency_ms"] = delay  # real HTTP latency replaces TCP latency
                alive.append(node)
            else:
                node.data["_kernel_alive"] = False
                dead.append(node)
        alive.sort(key=lambda n: n.data.get("_latency_ms") or float("inf"))

        final = exempt + alive + untouched + ([] if cfg["drop_dead"] else dead)
        stats = {
            "mode": "kernel",
            "tested": len(candidates),
            "alive": len(alive),
            "dead": len(dead),
            "exempt": len(exempt),
            "untouched": len(untouched),
            "dropped": len(dead) if cfg["drop_dead"] else 0,
            "fastest_ms": alive[0].data.get("_latency_ms") if alive else None,
            "elapsed": round(time.time() - started, 1),
        }
        logger.info(
            "Kernel probe: tested=%s alive=%s dead=%s exempt=%s dropped=%s fastest=%sms elapsed=%ss",
            stats["tested"], stats["alive"], stats["dead"], stats["exempt"],
            stats["dropped"], stats["fastest_ms"], stats["elapsed"],
        )
        return final, stats
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        log_file.close()
        shutil.rmtree(workdir, ignore_errors=True)
