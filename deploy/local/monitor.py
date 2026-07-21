"""Uptime / health monitor for the live operator (observation never blocks the money path).

Checks, in order, and prints a single status line (plus non-zero exit if anything is down):
  1. public /healthz reachable + ok=true
  2. the agent wallet's on-chain USDC balance (so you notice it running dry)
  3. (best-effort) the launchd services are loaded

Designed for a launchd/cron cadence (e.g. every 5 min). Read-only; it never touches operator
state. All network reads carry a real User-Agent (public zones 403 the default urllib UA).

Env:
  MANDATEHUB_PUBLIC_URL   default https://mandatehub.obolpay.xyz
  MANDATEHUB_AGENT_ADDR   agent wallet address to balance-check (optional)
  MANDATEHUB_RPC_URL      default https://mainnet.base.org
  MANDATEHUB_USDC         default Base mainnet USDC
  MANDATEHUB_MIN_USDC     warn below this many whole USDC (default 0.1)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from urllib.error import HTTPError, URLError

UA = {"User-Agent": "mandatehub-monitor/1"}


def _get_json(url: str, data: bytes | None = None) -> dict:
    headers = dict(UA)
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers),
                                timeout=20) as r:
        return json.load(r)


def _usdc_balance(rpc: str, usdc: str, addr: str) -> float:
    data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [
        {"to": usdc, "data": "0x70a08231" + "0" * 24 + addr[2:]}, "latest"]}).encode()
    return int(_get_json(rpc, data)["result"], 16) / 1e6


def main() -> int:
    public = os.environ.get("MANDATEHUB_PUBLIC_URL", "https://mandatehub.obolpay.xyz")
    rpc = os.environ.get("MANDATEHUB_RPC_URL", "https://mainnet.base.org")
    usdc = os.environ.get("MANDATEHUB_USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    addr = os.environ.get("MANDATEHUB_AGENT_ADDR")
    min_usdc = float(os.environ.get("MANDATEHUB_MIN_USDC", "0.1"))

    problems: list[str] = []
    parts: list[str] = []

    # 1) public health
    try:
        h = _get_json(public.rstrip("/") + "/healthz")
        if not h.get("ok"):
            problems.append("healthz ok=false")
        parts.append(f"health=up remaining={h.get('remaining_cents')} "
                     f"settled={h.get('settled_this_process')} denied={h.get('denied_this_process')}")
        try:
            m = _get_json(public.rstrip("/") + "/metrics")
            parts.append(f"revenue={m.get('revenue_cents', 0) / 1e6:.4f}USDC "
                         f"total_settled={m.get('settlements')}")
        except Exception:
            pass
    except (HTTPError, URLError, TimeoutError, ValueError) as e:
        problems.append(f"healthz unreachable: {e!r}")
        parts.append("health=DOWN")

    # 2) agent balance (optional)
    if addr:
        try:
            bal = _usdc_balance(rpc, usdc, addr)
            parts.append(f"agent_usdc={bal:.4f}")
            if bal < min_usdc:
                problems.append(f"agent balance low: {bal:.4f} < {min_usdc}")
        except Exception as e:  # RPC hiccups shouldn't crash the monitor
            parts.append("agent_usdc=?")
            problems.append(f"balance check failed: {e!r}")

    # 3) service supervision (best-effort, platform-aware, local only)
    import shutil
    try:
        if shutil.which("systemctl"):
            for svc in ("mandatehub-operator", "mandatehub-tunnel"):
                state = subprocess.run(["systemctl", "is-active", svc], capture_output=True,
                                       text=True, timeout=10).stdout.strip()
                if state != "active":
                    problems.append(f"{svc} {state or 'inactive'}")
            parts.append("services=ok" if not any("mandatehub-" in p for p in problems)
                         else "services=CHECK")
        elif shutil.which("launchctl"):
            listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                                     timeout=10).stdout
            for svc in ("com.mandatehub.operator", "com.mandatehub.tunnel"):
                if svc not in listing:
                    problems.append(f"{svc} not loaded")
            parts.append("services=ok" if not any("not loaded" in p for p in problems)
                         else "services=CHECK")
        else:
            parts.append("services=skipped")
    except Exception:
        parts.append("services=?")

    status = "OK" if not problems else "PROBLEM"
    print(f"[{status}] " + " | ".join(parts) + ("" if not problems else "  << " + "; ".join(problems)))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
