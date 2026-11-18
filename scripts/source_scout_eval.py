#!/usr/bin/env python3
"""
Evaluation and promotion half of scripts/source_scout.py.

Split into its own file to keep each script readable. Imported lazily by
source_scout.py so `discover` does not pay the import cost of the probe stack.
"""

import asyncio
import gzip
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timezone

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.fetcher import Fetcher  # noqa: E402
from core.parser import Node  # noqa: E402
from utils.logger import logger  # noqa: E402

CLASHX_KERNEL_GZ = (
    "/Applications/ClashX Meta.app/Contents/Resources/"
    "com.metacubex.ClashX.ProxyConfigHelper.meta.gz"
)


def _find_kernel() -> Path:
    env = __import__("os").getenv("MIHOMO_BIN")
    if env:
        path = Path(env)
        if not path.exists():
            raise SystemExit(f"MIHOMO_BIN not found: {env}")
        return path
    cached = Path(tempfile.gettempdir()) / "local-probe-mihomo"
    if cached.exists() and shutil.which and __import__("os").access(cached, __import__("os").X_OK):
        return cached
    if Path(CLASHX_KERNEL_GZ).exists():
        data = gzip.decompress(Path(CLASHX_KERNEL_GZ).read_bytes())
        cached.write_bytes(data)
        cached.chmod(0o755)
        return cached
    for candidate in ("mihomo", "clash-meta"):
        found = shutil.which(candidate)
        if found:
            return Path(found)
    raise RuntimeError("No local Mihomo kernel; set MIHOMO_BIN or install ClashX Meta.")


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _candidate_key(item: dict) -> str:
    """Stable key used by --approve: 'repo:url'."""
    return f"{item.get('repo', 'unknown')}:{item.get('url', '')}"


def evaluate_candidates(args) -> int:
    candidates_path = Path(args.candidates)
    if not candidates_path.exists():
        raise SystemExit(f"Candidates file not found: {candidates_path}")
    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") or []
    if not candidates:
        raise SystemExit("No candidates to evaluate; run `discover` first.")
    if len(candidates) > args.max_candidates:
        print(f"Limiting to first {args.max_candidates} candidates (of {len(candidates)}).")
        candidates = candidates[: args.max_candidates]

    kernel = _find_kernel()
    print(f"Kernel: {kernel}")

    reports = []
    with asyncio.Runner() as runner:
        for index, item in enumerate(candidates, 1):
            url = item.get("url")
            print(f"[{index}/{len(candidates)}] {item.get('repo')} -> {url}")
            try:
                async def _fetch():
                    async with Fetcher(timeout=12, connect_timeout=6) as f:
                        return await f.fetch_nodes(url)
                nodes = runner.run(_fetch())
            except Exception as exc:
                reports.append({**item, "error": f"{type(exc).__name__}: {exc}",
                                "parsed": 0, "tested": 0, "alive": 0,
                                "alive_rate": 0.0, "qualified": False})
                print(f"    fetch failed: {exc}")
                continue

            parsed = len(nodes)
            if not nodes:
                reports.append({**item, "parsed": 0, "tested": 0, "alive": 0,
                                "alive_rate": 0.0, "qualified": False,
                                "error": "no parsable nodes"})
                print("    no parsable nodes")
                continue

            tested = nodes[: args.max_nodes]
            try:
                alive, fastest = _probe_nodes(tested, kernel, args)
            except Exception as exc:
                reports.append({**item, "parsed": parsed, "tested": len(tested),
                                "alive": 0, "alive_rate": 0.0, "qualified": False,
                                "error": f"probe failed: {exc}"})
                print(f"    probe failed: {exc}")
                continue

            alive_count = len(alive)
            rate = alive_count / max(1, len(tested))
            qualified = (
                parsed >= args.min_nodes
                and alive_count >= 3
                and rate >= args.min_alive_rate
            )
            reports.append({
                **item,
                "parsed": parsed,
                "tested": len(tested),
                "alive": alive_count,
                "alive_rate": round(rate, 3),
                "fastest_ms": fastest,
                "qualified": qualified,
                "key": _candidate_key(item),
                "error": None,
            })
            print(f"    parsed={parsed} tested={len(tested)} alive={alive_count} "
                  f"rate={rate:.0%} {'QUALIFIED' if qualified else 'rejected'}")

    qualified_count = sum(1 for r in reports if r.get("qualified"))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "thresholds": {
                    "min_nodes": args.min_nodes,
                    "min_alive_rate": args.min_alive_rate,
                },
                "summary": {
                    "evaluated": len(reports),
                    "qualified": qualified_count,
                },
                "reports": reports,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nEvaluated {len(reports)} sources; {qualified_count} qualified.")
    print(f"Report: {out}")
    print("Review the report, then: python scripts/source_scout.py promote "
          "--approve '<repo>:<url>'")
    return 0


def _probe_nodes(nodes: List[Node], kernel: Path, args) -> tuple:
    """Probe nodes with a throwaway Mihomo config; return (alive_names, fastest_ms)."""
    workdir = Path(tempfile.mkdtemp(prefix="scout-probe-"))
    ctrl_port = _free_port()
    cfg = {
        "mixed-port": _free_port(),
        "external-controller": f"127.0.0.1:{ctrl_port}",
        "mode": "direct",
        "log-level": "error",
        "proxies": [n.to_clash() for n in nodes],
        "rules": ["MATCH,DIRECT"],
    }
    cfg_path = workdir / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    log = (workdir / "kernel.log").open("w")
    proc = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        proc = subprocess.Popen(
            [str(kernel), "-d", str(workdir), "-f", str(cfg_path)],
            stdout=log, stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{ctrl_port}"
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                opener.open(base + "/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        else:
            detail = (workdir / "kernel.log").read_text(encoding="utf-8", errors="replace")[:400]
            raise RuntimeError(f"kernel not ready: {detail}")

        from concurrent.futures import ThreadPoolExecutor

        def check(name):
            endpoint = (
                base + "/proxies/" + urllib.parse.quote(str(name), safe="")
                + "/delay?url=" + urllib.parse.quote(args.test_url, safe="")
                + f"&timeout={args.timeout}"
            )
            try:
                with opener.open(endpoint, timeout=args.timeout / 1000.0 + 6) as resp:
                    data = json.loads(resp.read())
                d = data.get("delay")
                if isinstance(d, int) and d > 0:
                    return name, d
            except Exception:
                pass
            return name, None

        names = [n.data.get("name") for n in nodes]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(check, names))
        alive = [(name, d) for name, d in results if d is not None]
        fastest = min((d for _, d in alive), default=None)
        return alive, fastest
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()
        shutil.rmtree(workdir, ignore_errors=True)


def promote_candidates(args) -> int:
    """Append approved candidates to sources.yaml as disabled entries."""
    report_path = Path(args.report)
    if not report_path.exists():
        raise SystemExit(f"Report not found: {report_path}")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    reports = payload.get("reports") or []

    by_key = {r.get("key") or _candidate_key(r): r for r in reports}
    approved_keys = []
    if args.approve_all_qualified:
        approved_keys = [k for k, r in by_key.items() if r.get("qualified")]
    else:
        approved_keys = list(args.approve)

    if not approved_keys:
        raise SystemExit(
            "Nothing approved. Pass --approve '<key>' (see report 'key' field) "
            "or --approve-all-qualified."
        )

    sources_path = ROOT / "sources.yaml"
    text = sources_path.read_text(encoding="utf-8")
    existing = set()
    try:
        data = yaml.safe_load(text) or {}
        for s in data.get("sources") or []:
            if isinstance(s, dict) and s.get("url"):
                existing.add(str(s["url"]).strip())
    except Exception:
        pass

    lines = [
        "",
        "  # ==========================================",
        "  # 🔎 半自动发现候选（source_scout promote 追加，默认禁用，人工确认后再启用）",
        f"  # 生成于 {datetime.now(timezone.utc).isoformat()}",
        "  # ==========================================",
    ]
    added = 0
    for key in approved_keys:
        report = by_key.get(key)
        if not report:
            print(f"  ! unknown key, skipped: {key}")
            continue
        url = report.get("url")
        if not url or url in existing:
            print(f"  ! duplicate/invalid, skipped: {url}")
            continue
        repo = report.get("repo", "candidate")
        note = (
            f"半自动发现 | 解析{report.get('parsed', 0)} 存活{report.get('alive', 0)} "
            f"存活率{report.get('alive_rate', 0):.0%} | 待人工复核"
        )
        lines.extend([
            f'  - name: "🔎 {repo} (候选)"',
            f'    url: "{url}"',
            "    enabled: false",
            f'    note: "{note}"',
        ])
        existing.add(url)
        added += 1

    if not added:
        print("No new sources to append.")
        return 0

    sources_path.write_text(text.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"Appended {added} disabled candidate(s) to {sources_path}.")
    print("Enable individually by setting enabled: true after manual review.")
    return 0
