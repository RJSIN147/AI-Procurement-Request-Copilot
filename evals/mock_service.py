"""Start the mock vendor-risk API for an eval run if it isn't already running."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]


def _healthy(base: str) -> bool:
    try:
        return requests.get(f"{base}/health", timeout=0.5).ok
    except requests.RequestException:
        return False


@contextmanager
def mock_api_running():
    base = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    if _healthy(base):
        yield base
        return
    port = str(urlparse(base).port or 8001)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_api.app:app", "--host", "127.0.0.1",
                             "--port", port, "--log-level", "warning"], cwd=ROOT)
    try:
        deadline = time.monotonic() + 15
        while not _healthy(base):
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"Could not start the mock vendor-risk API on port {port}")
            time.sleep(0.25)
        print(f"(started mock vendor-risk API on {base})")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
