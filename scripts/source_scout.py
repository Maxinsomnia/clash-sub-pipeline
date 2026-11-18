#!/usr/bin/env python3
"""
scripts/source_scout.py - Semi-automatic discovery of new public proxy sources.

This tool does NOT auto-enable anything. It implements a review pipeline:

  discover  -> find candidate subscription URLs (GitHub search + seed repos)
  evaluate  -> fetch + real local probe each candidate, score its quality
  promote   -> after human review, append APPROVED candidates to sources.yaml
               as disabled entries, ready for a one-line enable.

Why semi-automatic: newly-discovered public sources have a high ratio of dead
nodes, phishing subscriptions and compliance risk. Auto-enabling them would
undo the reliability work done by the kernel probe. A human reviews the
quality report and decides what to add.

Requires: httpx (already a project dependency), and for evaluate, a local
Mihomo kernel (auto-detected from ClashX Meta.app, or MIHOMO_BIN).

Examples:
    python scripts/source_scout.py discover
    python scripts/source_scout.py discover --max-repos 30 --out scout_candidates.json
    python scripts/source_scout.py evaluate --candidates scout_candidates.json --out scout_report.json
    python scripts/source_scout.py promote --report scout_report.json --approve "repo/name:path"
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

try:
    import httpx
except ImportError:
    httpx = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.logger import logger  # noqa: E402

# GitHub search queries aimed at repos that publish auto-refreshing free subs.
# Name/topic-scoped queries avoid the noise (public-apis, interview repos, etc.)
# that broad `in:readme` queries produce.
SEARCH_QUERIES = [
    "clash in:name,description stars:>150 pushed:>2025-01-01",
    "free proxy nodes in:name,description stars:>150 pushed:>2025-01-01",
    "订阅 in:name,description stars:>100 pushed:>2025-01-01",
    "topic:clash stars:>150",
    "topic:v2ray stars:>150",
]

# Repos whose README is itself a curated list of live subscription URLs.
SEED_READMES = [
    "VPN-Subcription-Links/ClashX-V2Ray-TopFreeProxy",
    "Pawdroid/Free-servers",
]

# A repository is relevant only if its name/description mentions proxies.
TOPIC_RE = re.compile(
    r"clash|v2ray|vless|trojan|proxy|proxies|节点|订阅|subscri|free.?node|机场|翻墙|fanqiang|ssr|xray",
    re.IGNORECASE,
)

GITHUB_API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "clash-sub-pipeline-source-scout",
    "X-GitHub-Api-Version": "2022-11-28",
}
# Last path segment looks like a subscription file (extension optional).
SUB_PATH_RE = re.compile(
    r"(?:^|/)[^/]*(?:sub|clash|node|proxy|free|list|payload|all|config|订阅)[^/]*"
    r"(?:\.(?:yaml|yml|txt|b64|json))?$",
    re.IGNORECASE,
)
BAD_PATH_RE = re.compile(
    r"\.(?:png|jpe?g|gif|svg|ico|md|pdf|zip|exe|dmg|go|ts|tsx|js|jsx|mts|"
    r"pb|toml|lock|html|css|py|rs|java|kt|swift|gradle|properties|xml)$|"
    r"readme|license|\.github|/assets?/|node_modules|/src/|/test|"
    r"tsconfig|jsconfig|package(-lock)?\.json|config\.json",
    re.IGNORECASE,
)
# Directories that typically hold generated subscription data.
SUB_DIR_RE = re.compile(r"/(?:output|sub|subs|docs|nodes?|proxies|list|clash|configs?)/", re.IGNORECASE)
SUB_NAME_RE = re.compile(
    r"sub|node|proxy|free|clash|list|all|payload|机场|订阅|subscribe|v2ray|trojan|vless",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s)>\]\"'<>]+")


def load_existing_urls() -> set:
    """URLs already present (enabled or disabled) in sources.yaml."""
    try:
        import yaml
        data = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8")) or {}
        urls = set()
        for s in data.get("sources") or []:
            if isinstance(s, dict) and s.get("url"):
                urls.add(str(s["url"]).strip())
        return urls
    except Exception as exc:
        logger.warning(f"Could not read sources.yaml: {exc}")
        return set()


def _gh_get(client, path: str, params: Optional[dict] = None):
    resp = client.get(GITHUB_API + path, params=params or {}, timeout=30)
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        raise RuntimeError("GitHub API rate limit reached; set GITHUB_TOKEN and retry.")
    resp.raise_for_status()
    return resp.json()


def _normalize_repo_url(raw: str, default_repo: str) -> Optional[str]:
    """Turn a README URL/blob link into a raw.githubusercontent.com URL.

    Accepts both `.../user/repo/branch/file` and `.../user/repo/branch/dir/file`.
    Returns None when the final path component does not look like a subscription.
    """
    raw = raw.rstrip(".,;)")
    # Strip HTML/CSS fragments that regex may have swallowed (e.g. `sub</a`).
    raw = re.split(r"[<>\s\\]", raw)[0]
    raw = raw.split("#")[0]

    if "raw.githubusercontent.com/" in raw:
        url = raw.split("raw.githubusercontent.com/", 1)[1]
        url = url.split("?", 1)[0]
        parts = url.split("/")
        if len(parts) >= 4:
            candidate = "/".join(parts[:5]) if len(parts) > 5 else url
            norm = "https://raw.githubusercontent.com/" + candidate
            from urllib.parse import urlparse as _up
            return norm if not BAD_PATH_RE.search(_up(norm).path) else None
        return None
    if "/blob/" in raw:
        m = re.search(r"github\.com/([\w.\-]+)/([\w.\-]+)/blob/([^/]+)/(.+)", raw)
        if m:
            user, repo, branch, path = m.groups()
            path = path.split("?", 1)[0]
            if BAD_PATH_RE.search(path):
                return None
            return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"
    return None


def _is_subscription_url(url: str) -> bool:
    """True only for paths that look like published proxy subscription data."""
    path = urlparse(url).path
    if BAD_PATH_RE.search(path):
        return False
    last = path.rstrip("/").split("/")[-1]
    low = last.lower()
    # Data formats: require an explicit subscription keyword in the filename,
    # or a known subscription data directory in the path.
    if re.search(r"\.(?:yaml|yml|txt|b64|json)$", low):
        return bool(SUB_NAME_RE.search(low) or SUB_DIR_RE.search(path))
    # Extensionless subscriptions (e.g. Pawdroid's /sub, TGParse's /trojan).
    return low in ("sub", "subs", "subscribe", "nodes", "proxies", "clash", "all", "list",
                   "trojan", "vless", "vmess", "ss") \
        or bool(SUB_DIR_RE.search(path) and SUB_NAME_RE.search(low))


def discover(args) -> int:
    if httpx is None:
        raise SystemExit("httpx is required: pip install -r requirements-dev.txt")

    existing = load_existing_urls()
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    candidates: Dict[str, dict] = {}

    with httpx.Client(headers=headers, follow_redirects=True) as client:
        repos = []
        seen_repos = set()
        for query in SEARCH_QUERIES:
            try:
                data = _gh_get(client, "/search/repositories", {
                    "q": query,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": min(args.max_repos, 100),
                })
                for item in data.get("items", []):
                    full = item.get("full_name")
                    if full and full not in seen_repos:
                        seen_repos.add(full)
                        repos.append(item)
            except Exception as exc:
                logger.warning(f"Search failed for '{query}': {exc}")
        # Always include seed repos even when search is rate-limited.
        for seed in SEED_READMES:
            if seed in seen_repos:
                continue
            try:
                repos.append(_gh_get(client, f"/repos/{seed}"))
                seen_repos.add(seed)
            except Exception as exc:
                logger.warning(f"Seed repo fetch failed ({seed}): {exc}")

        repos.sort(key=lambda r: r.get("stargazers_count", 0), reverse=True)
        # Drop irrelevant repos that slipped through search (readme keyword hits).
        repos = [
            r for r in repos
            if r.get("full_name") in SEED_READMES
            or TOPIC_RE.search(
                f"{r.get('name','')} {r.get('description','') or ''} "
                f"{' '.join(r.get('topics') or [])}"
            )
        ][: args.max_repos]
        print(f"Scanning {len(repos)} relevant repositories for subscription URLs ...")

        for repo in repos:
            full = repo.get("full_name")
            if not full:
                continue
            branch = repo.get("default_branch", "HEAD")
            # Try README (common branch names), then a shallow tree listing.
            readme_texts = []
            for ref in (branch, "master", "main"):
                try:
                    resp = client.get(
                        f"https://raw.githubusercontent.com/{full}/{ref}/README.md",
                        timeout=20,
                    )
                    if resp.status_code == 200 and resp.text:
                        readme_texts.append(resp.text)
                        break
                except Exception:
                    pass

            urls = []
            for text in readme_texts:
                for found in URL_RE.findall(text):
                    norm = _normalize_repo_url(found, full)
                    if norm and _is_subscription_url(norm):
                        urls.append(norm)
            # Also look at repo tree for obvious subscription files.
            try:
                tree = _gh_get(client, f"/repos/{full}/git/trees/{branch}?recursive=1")
                for node in tree.get("tree", []):
                    path = node.get("path", "")
                    if node.get("type") != "blob" or not _is_subscription_url(path):
                        continue
                    if BAD_PATH_RE.search(path):
                        continue
                    urls.append(f"https://raw.githubusercontent.com/{full}/{branch}/{path}")
            except Exception:
                pass

            for url in dict.fromkeys(urls):
                if url in existing:
                    continue
                host = urlparse(url).netloc.lower()
                if host not in ("raw.githubusercontent.com", "gist.githubusercontent.com"):
                    continue  # keep discover conservative and auditable
                key = url
                if key in candidates:
                    continue
                candidates[key] = {
                    "url": url,
                    "repo": full,
                    "stars": repo.get("stargazers_count", 0),
                    "found_at": datetime.now(timezone.utc).isoformat(),
                }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(candidates),
                "candidates": list(candidates.values()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Discovered {len(candidates)} candidate URLs (excluded {len(existing)} existing).")
    print(f"Written {out}")
    return 0


def evaluate(args) -> int:
    from source_scout_eval import evaluate_candidates  # lazy import
    return evaluate_candidates(args)


def promote(args) -> int:
    from source_scout_eval import promote_candidates  # lazy import
    return promote_candidates(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Semi-automatic public proxy source discovery and review"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dis = sub.add_parser("discover", help="Find candidate subscription URLs")
    p_dis.add_argument("--max-repos", type=int, default=25)
    p_dis.add_argument("--out", default="scout_candidates.json")
    p_dis.set_defaults(func=discover)

    p_ev = sub.add_parser("evaluate", help="Fetch + real-probe each candidate")
    p_ev.add_argument("--candidates", default="scout_candidates.json")
    p_ev.add_argument("--out", default="scout_report.json")
    p_ev.add_argument("--timeout", type=int, default=5000, help="Per-node probe timeout in ms")
    p_ev.add_argument("--workers", type=int, default=40)
    p_ev.add_argument("--max-nodes", type=int, default=150, help="Max nodes probed per source")
    p_ev.add_argument("--max-candidates", type=int, default=30, help="Max candidates to evaluate")
    p_ev.add_argument("--min-nodes", type=int, default=5, help="Min parsed nodes to qualify")
    p_ev.add_argument("--min-alive-rate", type=float, default=0.30,
                      help="Min alive ratio (0-1) to qualify")
    p_ev.add_argument("--test-url", default="https://cp.cloudflare.com/generate_204")
    p_ev.set_defaults(func=evaluate)

    p_pr = sub.add_parser("promote", help="Append approved candidates to sources.yaml (disabled)")
    p_pr.add_argument("--report", default="scout_report.json")
    p_pr.add_argument("--approve", action="append", default=[],
                      help="Key to approve, e.g. 'repo/name:https://raw...' (repeatable)")
    p_pr.add_argument("--approve-all-qualified", action="store_true",
                      help="Approve all qualified candidates (still added disabled)")
    p_pr.set_defaults(func=promote)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        logger.error(f"source_scout failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
