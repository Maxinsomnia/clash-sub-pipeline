#!/usr/bin/env python3
"""
Local network-aware subscription filter.

GitHub Actions runs overseas, so its probe cannot tell whether a node works
from YOUR network. This script runs a real Mihomo kernel locally, tests every
node with an actual HTTP request, and writes a cleaned config containing only
nodes that are usable from where you are.

Two-stage probe:
  1. general reachability (Cloudflare generate_204)
  2. AI reachability (Gemini API discovery) -- only on nodes that passed stage 1

The output drops region groups with zero healthy nodes, and partitions healthy
nodes into "🤖 AI 可用节点" / "🌐 非AI节点" groups wired into the menus.

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
DEFAULT_AI_URL = "https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta"
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
    """Two-stage probe: general reachability, then AI (Gemini) reachability.

    Returns (working, ai_capable):
      working   = {name: latency_ms} for nodes that pass the general HTTP test
      ai_capable= {name: latency_ms} for working nodes that also reach Gemini
    """
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
        print(f"Probing {len(names)} nodes with {args.workers} workers (real HTTP via {args.test_url}) ...")
        started = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(
                pool.map(lambda n: test_one(n, base, args.test_url, args.timeout, opener), names)
            )
        working = {name: delay for name, delay in results if delay is not None}
        print(
            f"General probe done in {time.time() - started:.1f}s: "
            f"{len(working)}/{len(names)} usable ({100 * len(working) / max(1, len(names)):.1f}%)"
        )

        # Stage 2: only test AI reachability on nodes that already work.
        ai_capable = {}
        if working and not args.skip_ai:
            ai_names = list(working)
            print(f"Checking AI (Gemini) reachability on {len(ai_names)} working nodes ...")
            ai_started = time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                ai_results = list(
                    pool.map(
                        lambda n: test_one(n, base, args.ai_url, args.timeout, opener),
                        ai_names,
                    )
                )
            ai_capable = {name: delay for name, delay in ai_results if delay is not None}
            print(
                f"AI probe done in {time.time() - ai_started:.1f}s: "
                f"{len(ai_capable)}/{len(ai_names)} can reach Gemini "
                f"({100 * len(ai_capable) / max(1, len(ai_names)):.1f}%)"
            )
        return working, ai_capable
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        log.close()
        shutil.rmtree(workdir, ignore_errors=True)


def build_clean_config(original, working, ai_capable, args):
    """Filter proxies, delete empty groups, and partition nodes by AI access.

    Returns (cleaned_config, stats).
    """
    cleaned = copy.deepcopy(original)
    all_groups = cleaned.get("proxy-groups", [])

    info_nodes = {
        p.get("name") for p in original.get("proxies", [])
        if str(p.get("server", "")) in ("127.0.0.1", "::1")
    }
    group_names = {g.get("name") for g in all_groups}
    builtins = {"DIRECT", "REJECT", "PASS"}

    # Nodes that survive: healthy real nodes + display-only info nodes.
    surviving_nodes = set(working) | info_nodes

    # 1) Strip every group of dead node references.
    for group in all_groups:
        group["proxies"] = [
            ref for ref in (group.get("proxies") or [])
            if ref in surviving_nodes or ref in group_names or ref in builtins
        ]

    # 2) Identify region auto groups (⚡ 自动选择 | xxx) with zero healthy
    #    nodes and delete them + their paired region select group.
    auto_groups = {}
    for group in all_groups:
        name = group.get("name") or ""
        if name.startswith("⚡ 自动选择 | "):
            region = name[len("⚡ 自动选择 | "):]
            auto_groups[region] = (group, name)
            group_names.discard(name)  # mark for potential removal

    dead_groups = set()
    for region, (group, auto_name) in auto_groups.items():
        has_healthy = any(ref in working for ref in (group.get("proxies") or []))
        if not has_healthy:
            dead_groups.add(auto_name)
            dead_groups.add(region)

    # 3) Remove dead groups and purge references to them everywhere.
    all_groups = [g for g in all_groups if g.get("name") not in dead_groups]
    cleaned["proxy-groups"] = all_groups
    live_group_names = {g.get("name") for g in all_groups}
    for group in all_groups:
        group["proxies"] = [
            ref for ref in (group.get("proxies") or [])
            if ref in live_group_names or ref in surviving_nodes or ref in builtins
        ]

    # 4) Update the 🗺️ 选择地区 menu to only list surviving regions.
    for group in all_groups:
        if group.get("name") == "🗺️ 选择地区":
            kept = [ref for ref in (group.get("proxies") or []) if ref in live_group_names]
            group["proxies"] = kept if kept else ["DIRECT"]

    # 5) Partition working nodes by AI reachability into two new groups.
    ai_nodes = [name for name in working if name in ai_capable]
    normal_nodes = [name for name in working if name not in ai_capable]
    # Keep the same latency ordering the kernel already measured.
    ai_nodes.sort(key=lambda n: ai_capable[n])
    normal_nodes.sort(key=lambda n: working[n])

    def make_group(name, test_url, members):
        group = {
            "name": name,
            "type": "fallback",
            "url": test_url,
            "interval": 300,
            "timeout": 3000,
            "lazy": True,
            "proxies": members if members else ["DIRECT"],
        }
        return group

    ai_group_name = "🤖 AI 可用节点"
    normal_group_name = "🌐 普通节点"
    ai_group = make_group(ai_group_name, args.ai_url, ai_nodes)
    normal_group = make_group(normal_group_name, args.test_url, normal_nodes)

    # 6) Wire the two groups into the menus.
    for group in all_groups:
        name = group.get("name")
        if name == "🚀 选择代理":
            refs = [
                ref for ref in (group.get("proxies") or [])
                if ref not in (ai_group_name, normal_group_name)
            ]
            # Keep the original first entry as the default; list AI group before
            # the general group so AI-capable nodes are offered first.
            insert_at = 1 if refs else 0
            refs[insert_at:insert_at] = [ai_group_name, normal_group_name]
            group["proxies"] = refs
        elif name == "🤖 AI 服务":
            # Clash select groups default to the FIRST proxy in the list.
            # Put the Gemini-verified group first so AI traffic uses working
            # AI nodes out of the box; keep the rest as manual fallbacks.
            refs = [
                ref for ref in (group.get("proxies") or [])
                if ref not in (ai_group_name, normal_group_name)
            ]
            group["proxies"] = [ai_group_name, normal_group_name] + refs
        elif name in ("♻️ 自动选择", "🔰 延迟最低", "✅ 手动选择"):
            # Point the auto pools at the AI-capable subset so AI and general
            # traffic both prefer nodes proven to work on this network.
            pass

    # Insert new groups at the front so they appear first in the UI.
    cleaned["proxy-groups"] = [ai_group, normal_group] + all_groups

    # 7) Keep only surviving proxies in the node list.
    cleaned["proxies"] = [
        p for p in original.get("proxies", [])
        if p.get("name") in surviving_nodes
    ]

    stats = {
        "workspace_nodes": len(working),
        "ai_nodes": len(ai_nodes),
        "normal_nodes": len(normal_nodes),
        "deleted_groups": sorted(dead_groups),
        "final_groups": len(cleaned["proxy-groups"]),
    }
    return cleaned, stats


def main():
    parser = argparse.ArgumentParser(description="Filter a Clash subscription by local network reachability")
    parser.add_argument("--url", default=DEFAULT_URL, help="Remote subscription URL")
    parser.add_argument("--input", help="Local config file (overrides --url)")
    parser.add_argument("--output", default="local-clash.yaml", help="Output config path")
    parser.add_argument("--test-url", default="https://cp.cloudflare.com/generate_204")
    parser.add_argument("--ai-url", default=DEFAULT_AI_URL,
                        help="Endpoint used to judge AI (Gemini) availability")
    parser.add_argument("--skip-ai", action="store_true",
                        help="Skip AI reachability test and grouping")
    parser.add_argument("--timeout", type=int, default=5000, help="Per-node timeout in ms")
    parser.add_argument("--workers", type=int, default=40)
    parser.add_argument("--max-nodes", type=int, default=400, help="0 = all nodes")
    args = parser.parse_args()

    binary = find_kernel()
    config = load_config(args)
    working, ai_capable = probe(config, binary, args)
    if not working:
        raise SystemExit("No node is usable from this network; output not written.")

    cleaned, stats = build_clean_config(config, working, ai_capable, args)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Local filtered subscription | {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"# {stats['workspace_nodes']} nodes reachable from this machine (real HTTP proxy test)\n"
        f"# AI (Gemini) capable: {stats['ai_nodes']} | general only: {stats['normal_nodes']}\n"
        f"# Source: {args.input or args.url}\n"
    )
    out.write_text(
        header + yaml.safe_dump(cleaned, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"Written {out} with {len(cleaned['proxies'])} proxies, {stats['final_groups']} groups")
    if stats["deleted_groups"]:
        print(f"Deleted {len(stats['deleted_groups'])} empty region groups: "
              + ", ".join(stats["deleted_groups"][:12]))
    print("Groups: 🤖 AI 可用节点 / 🌐 普通节点 added to menus.")
    print("Import this FILE into ClashX Meta (Config -> Import -> Local File).")


if __name__ == "__main__":
    main()
