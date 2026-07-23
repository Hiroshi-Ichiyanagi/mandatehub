#!/usr/bin/env bash
# Daily cron: has mandatehub appeared in the CDP x402 Bazaar yet? Notify once when it does.
#
# Env (all optional; sensible defaults for the production VPS):
#   MANDATEHUB_DATA_DIR      state dir (default /root/.mandatehub-operator)
#   MANDATEHUB_REPO          repo checkout with .venv (default /opt/mandatehub)
#   MANDATEHUB_CDP_KEY_FILE  CDP api key json (default /root/.mandatehub-cdp.json)
#   MANDATEHUB_PAY_TO        merchant address to query
#   MANDATEHUB_ALERT_WEBHOOK ntfy/webhook URL for the notification (no notify if unset)
set -eu
DATA_DIR="${MANDATEHUB_DATA_DIR:-/root/.mandatehub-operator}"
# Optional secrets (webhook, etc.) live in a 600 env file, never in the repo or in cron.d.
[ -f "$DATA_DIR/alert.env" ] && . "$DATA_DIR/alert.env"
REPO="${MANDATEHUB_REPO:-/opt/mandatehub}"
CDP_KEY_FILE="${MANDATEHUB_CDP_KEY_FILE:-/root/.mandatehub-cdp.json}"
PAY_TO="${MANDATEHUB_PAY_TO:-0xEDd58c7C43Cd63059fBeC3E43527c45f8efb42B4}"
WEBHOOK="${MANDATEHUB_ALERT_WEBHOOK:-}"
STATE="$DATA_DIR/bazaar.state"

[ -f "$STATE" ] && exit 0   # already notified

OUT=$(cd "$REPO" && MANDATEHUB_CDP_KEY_FILE="$CDP_KEY_FILE" MANDATEHUB_PAY_TO="$PAY_TO" \
  .venv/bin/python - <<'PY'
import json, os, pathlib, urllib.request
from cdp.auth import generate_jwt, JwtOptions
cfg = json.loads(pathlib.Path(os.environ["MANDATEHUB_CDP_KEY_FILE"]).read_text())
HOST = "api.cdp.coinbase.com"
pay_to = os.environ["MANDATEHUB_PAY_TO"]
path = f"/platform/v2/x402/discovery/merchant?payTo={pay_to}"
jwt = generate_jwt(JwtOptions(api_key_id=cfg["keyId"], api_key_secret=cfg["keySecret"],
                              request_method="GET", request_host=HOST,
                              request_path=path.split("?")[0]))
req = urllib.request.Request(f"https://{HOST}{path}",
                             headers={"Authorization": f"Bearer {jwt}",
                                      "User-Agent": "mandatehub-x402/1"})
d = json.load(urllib.request.urlopen(req, timeout=25))
print(len(d.get("resources", [])))
PY
) || { echo "bazaar check failed"; exit 0; }

if [ "$OUT" != "0" ] && [ -n "$OUT" ]; then
  if [ -n "$WEBHOOK" ]; then
    curl -s -H "Title: mandatehub" \
      -d "mandatehub is now listed in the x402 Bazaar (resources=$OUT)" "$WEBHOOK" >/dev/null 2>&1 || true
  fi
  echo listed > "$STATE"
fi
