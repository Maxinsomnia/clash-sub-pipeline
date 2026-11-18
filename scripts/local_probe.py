#!/usr/bin/env python3
"""
Local network-aware subscription filter.

GitHub Actions runs overseas, so its probe cannot tell whether a node works
from YOUR network. This script runs a real Mihomo kernel locally, tests every
node with an actual HTTP request, and writes a cleaned config containing only
nodes that are usable from where you are.

Usage:
    python scripts/local_probe.py
    python scripts/local_probe.py --url https://maxinsomnia.github.io/clash-sub-pipeline/clash.yaml
    python scripts/local_probe.py --input list.meta.yml --output my-clash.yaml

The kernel is auto-detected from ClashX Meta.app; override with MIHOMO_BIN.
"""

import argparse
import concurrent.futures
import copy
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

DEFAULT_URL = "https://maxinsomnia.github.io/clash-sub-pipeline/clash.yaml"
CLASHX_KERNEL_GZ = (
    "/Applications/ClashX Meta.app/Contents/Resources/"
    "com.metacubex.ClashX.ProxyConfigHelper.meta.gz"
)


def find_kernel() -> Path:
    """Locate a usable local Mihomo kernel."""
    env = os.getenv("MIHOMO_BIN")
    if env:
        path = Path(env)
        if not path.exists():
            raise SystemExit(f"MIHOMO_BIN not found: {env}")
        return path

    cached = Path(tempfile.gettempdir()) / "local-probe-mihomo"
    if cached.exists() and os.access(cached, os.X_OK):
        return cached

    if Path(CLASHX_KERNEL_GZ).exists():
        data = gzip.decompress(Path(CLASHX_KERNEL_GZ).read_bytes())
        cached.write_bytes(data)
        cached.chmod(0o755)
        print(f"Kernel extracted from ClashX Meta: {cached}")
        return cached

    for candidate in ("mihomo", "clash-meta"):
        found = shutil.which(candidate)
        if found:
            return Path(found)

    raise SystemExit(
        "No local Mihomo kernel found. Set MIHOMO_BIN to your mihomo/clash-meta binary."
    )


def load_config(args) -> dict:
    if args.input:
        text = Path(args.input).read_text(encoding="utf-8")
        source = args.input
    else:
        with urllib.request.urlopen(args.url, timeout=60) as response:
            text = response.read().decode("utf-8")
        source = args.url
    config = yaml.safe_load(text)
    if not isinstance(config, dict) or not config.get("proxies"):
        raise SystemExit(f"No proxies found in {source}")
    print(f"Loaded {len(config['proxies'])} proxies from {source}")
    return config


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_one(name, base, test_url, timeout_ms, opener):
    endpoint = (
        base + "/proxies/" + urllib.parse.quote(str(name), safe="")
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


def probe(config, binary, args):
    """Return {node_name: real_http_latency_ms} for working nodes."""
    workdir = Path(tempfile.mkdtemp(prefix="local-probe-"))
    mix_port, ctrl_port = free_port(), free_port()
    minimal = {
        "mixed-port": mix_port,
        "external-controller": f"127.0.0.1:{ctrl_port}",
        "mode": "direct",
        "log-level": "error",
        "proxies": config["proxies"],
        "rules": ["MATCH,DIRECT"],
    }
    cfg_path = workdir / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(minimal, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    log = (workdir / "kernel.log").open("w")
    process = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        process = subprocess.Popen(
            [str(binary), "-d", str(workdir), "-f", str(cfg_path)],
            stdout=log, stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{ctrl_port}"
        for _ in range(60):
            if process.poll() is not None:
                break
            try:
                opener.open(base + "/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise SystemExit("Kernel did not become ready: " + (workdir / "kernel.log").read_text()[:500])

        names = [p.get("name") for p in config["proxies"]]
        if args.max_nodes > 0:
            names = names[: args.max_nodes]
        print(f"Probing {len(names)} nodes with {args.workers} workers (real HTTP via {args.url}) ...")
        started = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(
                pool.map(lambda n: test_one(n, base, args.test_url, args.timeout, opener), names)
            )
        working = {name: delay for name, delay in results if delay is not None}
        print(
            f"Probe done in {time.time() - started:.1f}s: "
            f"{len(working)}/{len(names)} usable ({100 * len(working) / max(1, len(names)):.1f}%)"
        )
        return working
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        log.close()
        shutil.rmtree(workdir, ignore_errors=True)


def build_clean_config(original, working):
    """Keep working proxies and strip dead node references from every group."""
    cleaned = copy.deepcopy(original)
    info_nodes = {
        p.get("name") for p in original.get("proxies", [])
        if str(p.get("server", "")) in ("127.0.0.1", "::1")
    }
    group_names = {g.get("name") for g in original.get("proxy-groups", [])}
    allowed = set(working) | info_nodes | group_names | {"DIRECT", "REJECT", "PASS"}

    cleaned["proxies"] = [
        p for p in original.get("proxies", [])
        if p.get("name") in allowed
    ]
    for group in cleaned.get("proxy-groups", []):
        kept = [name for name in (group.get("proxies") or []) if name in allowed]
        group["proxies"] = kept if kept else ["DIRECT"]
    return cleaned


def main():
    parser = argparse.ArgumentParser(description="Filter a Clash subscription by local network reachability")
    parser.add_argument("--url", default=DEFAULT_URL, help="Remote subscription URL")
    parser.add_argument("--input", help="Local config file (overrides --url)")
    parser.add_argument("--output", default="local-clash.yaml", help="Output config path")
    parser.add_argument("--test-url", default="https://cp.cloudflare.com/generate_204")
    parser.add_argument("--timeout", type=int, default=5000, help="Per-node timeout in ms")
    parser.add_argument("--workers", type=int, default=40)
    parser.add_argument("--max-nodes", type=int, default=400, help="0 = all nodes")
    args = parser.parse_args()

    binary = find_kernel()
    config = load_config(args)
    working = probe(config, binary, args)
    if not working:
        raise SystemExit("No node is usable from this network; output not written.")

    cleaned = build_clean_config(config, working)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Local filtered subscription | {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"# {len(working)} nodes reachable from this machine (real HTTP proxy test)\n"
        f"# Source: {args.input or args.url}\n"
    )
    out.write_text(
        header + yaml.safe_dump(cleaned, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"Written {out} with {len(cleaned['proxies'])} proxies")
    print("Import this FILE into ClashX Meta (Config -> Import -> Local File).")


if __name__ == "__main__":
    main()
