#!/usr/bin/env python3
"""
Validate a generated Clash config with the REAL Mihomo kernel.

The YAML self-check in the workflow catches structural problems, but only the
kernel itself catches semantic ones such as an SS2022 node with an illegal
base64 key -- a single such node makes Mihomo reject the ENTIRE subscription,
which is exactly what caused ClashX Meta to report "输入不合法".

Usage:
    python scripts/validate_with_kernel.py list.meta.yml

Downloads the official mihomo binary (Linux amd64) into a cache dir on first
run. Set MIHOMO_BIN to use a local binary instead of downloading.
"""

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

MIHOMO_VERSION = os.getenv("MIHOMO_VERSION", "v1.19.34")
DOWNLOAD_URL = (
    "https://github.com/MetaCubeX/mihomo/releases/download/"
    f"{MIHOMO_VERSION}/mihomo-linux-amd64-compatible-{MIHOMO_VERSION}.gz"
)


def resolve_binary() -> Path:
    env_bin = os.getenv("MIHOMO_BIN")
    if env_bin:
        path = Path(env_bin)
        if not path.exists():
            raise SystemExit(f"MIHOMO_BIN not found: {env_bin}")
        return path

    cache_dir = Path(os.getenv("MIHOMO_CACHE_DIR", tempfile.gettempdir()))
    cached = cache_dir / "mihomo-validate"
    if cached.exists() and os.access(cached, os.X_OK):
        return cached

    print(f"Downloading mihomo kernel {MIHOMO_VERSION} ...")
    req = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "clash-sub-pipeline"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if not data.startswith(b"\x1f\x8b"):
        raise SystemExit("Unexpected mihomo download (not gzip)")
    import gzip
    decoded = gzip.decompress(data)
    cached.write_bytes(decoded)
    cached.chmod(0o755)
    print(f"Kernel ready: {cached} ({len(decoded)} bytes)")
    return cached


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python scripts/validate_with_kernel.py <config.yaml>")
    config = Path(sys.argv[1]).resolve()
    if not config.exists():
        raise SystemExit(f"Config not found: {config}")

    binary = resolve_binary()
    workdir = Path(tempfile.mkdtemp(prefix="mihomo-validate-"))
    try:
        proc = subprocess.run(
            [str(binary), "-t", "-d", str(workdir), "-f", str(config)],
            capture_output=True, text=True, timeout=120,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        print(output.strip())
        if "test failed" in output.lower() or proc.returncode != 0:
            print("KERNEL VALIDATION FAILED", file=sys.stderr)
            return 1
        print("Kernel validation OK")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
