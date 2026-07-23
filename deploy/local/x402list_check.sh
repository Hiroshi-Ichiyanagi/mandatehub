#!/usr/bin/env bash
# Daily cron: get mandatehub listed on x402-list.com, then watch for the listing to go live.
# States in $STATE: (absent)=not submitted; "submitted <id>"=pending review; "listed <slug>"=done.
# The directory allows one submission per email per 7 days, so the submit step is a best-effort
# retry (429 until the window opens); a manual form submission also satisfies it.
#
# Env (all optional):
#   MANDATEHUB_DATA_DIR      state dir (default /root/.mandatehub-operator)
#   MANDATEHUB_ALERT_WEBHOOK ntfy/webhook URL for notifications (no notify if unset)
#   MANDATEHUB_LIST_EMAIL    submitter email (no auto-submit if unset — listing watch still runs)
set -eu
DATA_DIR="${MANDATEHUB_DATA_DIR:-/root/.mandatehub-operator}"
# Optional secrets (webhook, email) live in a 600 env file, never in the repo or in cron.d.
[ -f "$DATA_DIR/alert.env" ] && . "$DATA_DIR/alert.env"
STATE="$DATA_DIR/x402list.state"

grep -q "^listed" "$STATE" 2>/dev/null && exit 0

MANDATEHUB_STATE="$STATE" \
MANDATEHUB_ALERT_WEBHOOK="${MANDATEHUB_ALERT_WEBHOOK:-}" \
MANDATEHUB_LIST_EMAIL="${MANDATEHUB_LIST_EMAIL:-}" \
python3 - <<'PY'
import json, os, urllib.request, urllib.error

STATE = os.environ["MANDATEHUB_STATE"]
WEBHOOK = os.environ.get("MANDATEHUB_ALERT_WEBHOOK", "")
EMAIL = os.environ.get("MANDATEHUB_LIST_EMAIL", "")
UA = {"User-Agent": "mandatehub-x402list/1", "Content-Type": "application/json"}


def notify(msg: str) -> None:
    if not WEBHOOK:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            WEBHOOK, data=msg.encode(), headers={"Title": "mandatehub"}), timeout=15)
    except Exception:
        pass


# 1. already listed? (this is the path the earlier VPS-only copy got wrong: bare `slug`)
try:
    d = json.load(urllib.request.urlopen(urllib.request.Request(
        "https://x402-list.com/api/v1/services", headers={"User-Agent": UA["User-Agent"]}), timeout=30))
    svcs = d if isinstance(d, list) else d.get("services") or d.get("data") or []
    for s in svcs:
        if "mandatehub" in (s.get("base_url", "") + s.get("slug", "")):
            slug = s.get("slug", "?")
            grade = (s.get("assessment") or {}).get("compliance_grade", "?")
            notify(f"mandatehub is now listed on x402-list.com (grade {grade}, slug {slug})")
            with open(STATE, "w") as fh:
                fh.write(f"listed {slug}\n")
            raise SystemExit(0)
except SystemExit:
    raise
except Exception as e:
    print("listing check failed:", e)

# 2. submission already pending? then just wait for review
try:
    with open(STATE) as fh:
        if fh.read().startswith("submitted"):
            raise SystemExit(0)
except FileNotFoundError:
    pass

# 3. best-effort submit (needs an email; 429 until the per-email window opens)
if not EMAIL:
    print("no MANDATEHUB_LIST_EMAIL set; skipping auto-submit (listing watch still active)")
    raise SystemExit(0)

payload = {
    "url": "https://mandatehub.obolpay.xyz",
    "email": EMAIL,
    "service_name": "mandatehub",
    "description": ("Machine-payable data & verification over x402 on Base mainnet, 0.01 USDC per "
                    "call. Hash-pinned data + verification products; every paid response is "
                    "canonically hashed and carries the on-chain settlement tx, an independent "
                    "chain verification, and a ProofOfMandate from a budget-capped, replay-proof "
                    "mandate gate. Stale/unavailable data returns 503 before any charge."),
    "website_url": "https://mandatehub.obolpay.xyz",
    "category": "Data",
    "endpoints": ["/quote", "/product/fx", "/product/qswap", "/product/audit-verify",
                  "/product/verify-tx", "/product/govern-verify", "/product/openunit",
                  "/product/kairos", "/product/cve-snapshot", "/product/gas-oracle",
                  "/product/url-liveness", "/product/content-attestation"],
    "notes": ("Same operator as the listed Obolpay x402 Gateway. Machine discovery: "
              "/.well-known/agents.json, /.well-known/ai-plugin.json, /openapi.json."),
}
try:
    r = urllib.request.urlopen(urllib.request.Request(
        "https://x402-list.com/api/v1/submit", data=json.dumps(payload).encode(), headers=UA),
        timeout=90)
    sid = (json.load(r).get("data") or {}).get("submission_id", "?")
    notify(f"submitted mandatehub to x402-list.com (id {sid}) — under review")
    with open(STATE, "w") as fh:
        fh.write(f"submitted {sid}\n")
    print("submitted", sid)
except urllib.error.HTTPError as e:
    if e.code == 429:
        print("429: submission window not open yet (7 days per email)")
    else:
        print("submit failed:", e.code, e.read()[:300])
PY
