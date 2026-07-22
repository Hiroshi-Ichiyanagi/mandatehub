"""Ops tools (deploy/local): backup, verify_state, monitor — offline, tamper-aware."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mandatehub import (
    AuditLog,
    Currency,
    IntentSettlementEngine,
    Ledger,
    Money,
    OwnerType,
    SQLiteLedgerStorage,
    TransactionBuilder,
)

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy" / "local"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
USDC = Currency.USDC


def _make_operator_state(data_dir: Path) -> None:
    """Build a realistic operator data dir: funded escrow, mandate, one settlement, config."""
    ledger = Ledger(SQLiteLedgerStorage(str(data_dir / "ledger.db")))
    audit = AuditLog(str(data_dir / "audit.db"))
    plat = ledger.open_account(OwnerType.PLATFORM, USDC, "platform")
    escrow = ledger.open_account(OwnerType.PLATFORM, USDC, "escrow")
    b = TransactionBuilder("DEPOSIT", "ops", initiated_at=T0)
    b.transfer(plat.account_id, escrow.account_id, Money(1000000, USDC))
    ledger.post(b.build()); ledger.settle(b.transaction_id, settled_at=T0)
    merchant = ledger.open_account(OwnerType.USER, USDC, "merchant")
    eng = IntentSettlementEngine(ledger, audit_log=audit)
    eng.create_mandate(
        mandate_id="operator-m1", principal_id="operator", escrow_account_id=escrow.account_id,
        budget_cap=Money(1000000, USDC), allowed_purposes=frozenset(["API_CALL"]),
        valid_from=T0, valid_until=T0 + timedelta(days=365), created_at=T0)
    eng.settle_intent(mandate_id="operator-m1", intent_id="i1",
                      payee_account_id=merchant.account_id, amount=Money(10000, USDC),
                      purpose="API_CALL", at=T0 + timedelta(minutes=1))
    (data_dir / "mandate.json").write_text(json.dumps({
        "mandate_id": "operator-m1", "principal_id": "operator",
        "escrow_account_id": escrow.account_id, "budget_cap_cents": 1000000,
        "allowed_purposes": ["API_CALL"], "valid_from": T0.isoformat(),
        "valid_until": (T0 + timedelta(days=365)).isoformat(), "created_at": T0.isoformat(),
        "merchant_account_id": merchant.account_id}))


def _run(script: str, data_dir: Path, extra_env=None):
    import os
    env = {**os.environ, "MANDATEHUB_DATA_DIR": str(data_dir), "PYTHONPATH": str(REPO)}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(DEPLOY / script)],
                          capture_output=True, text=True, env=env)


def test_backup_creates_verified_snapshot(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    _make_operator_state(data)
    r = _run("backup.py", data, {"MANDATEHUB_BACKUP_DIR": str(tmp_path / "bak")})
    assert r.returncode == 0, r.stderr
    snaps = list((tmp_path / "bak").iterdir())
    assert len(snaps) == 1
    for f in ("ledger.db", "audit.db", "mandate.json"):
        assert (snaps[0] / f).exists()
    ok, _ = AuditLog(str(snaps[0] / "audit.db")).verify_chain()
    assert ok


def test_verify_state_consistent(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    _make_operator_state(data)
    r = _run("verify_state.py", data)
    assert r.returncode == 0, r.stderr
    assert "STATE CONSISTENT" in r.stdout
    assert "audit chain: OK" in r.stdout


def test_verify_state_detects_semantic_tamper(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    _make_operator_state(data)
    conn = sqlite3.connect(str(data / "audit.db"))
    seq, payload = conn.execute(
        "SELECT sequence, payload FROM audit_events WHERE event_type='intent_settled' "
        "ORDER BY sequence LIMIT 1").fetchone()
    d = json.loads(payload); d["amount_cents"] = d["amount_cents"] + 1
    conn.execute("UPDATE audit_events SET payload=? WHERE sequence=?",
                 (json.dumps(d, sort_keys=True), seq))
    conn.commit(); conn.close()
    r = _run("verify_state.py", data)
    assert r.returncode != 0
    assert "INVALID" in r.stdout


def test_verify_state_ignores_whitespace_representation(tmp_path):
    """A JSON-representation change (not a value change) is NOT tampering."""
    data = tmp_path / "data"; data.mkdir()
    _make_operator_state(data)
    conn = sqlite3.connect(str(data / "audit.db"))
    conn.execute("UPDATE audit_events SET payload = payload || ' ' "
                 "WHERE sequence=(SELECT MIN(sequence) FROM audit_events)")
    conn.commit(); conn.close()
    r = _run("verify_state.py", data)
    assert r.returncode == 0, r.stdout + r.stderr


def test_stats_reports_revenue(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    _make_operator_state(data)   # one 10000-cent settlement
    r = _run("stats.py", data, {"MANDATEHUB_ARGS": ""})
    # stats.py takes --json via argv; call directly for determinism
    import os
    import subprocess
    env = {**os.environ, "MANDATEHUB_DATA_DIR": str(data), "PYTHONPATH": str(REPO)}
    out = subprocess.run([sys.executable, str(DEPLOY / "stats.py"), "--json"],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    m = json.loads(out.stdout)
    assert m["settlements"] == 1
    assert m["revenue_cents"] == 10000
    assert m["unique_payees"] == 1
    assert list(m["per_day"].values())[0]["count"] == 1


def test_dashboard_html_renders_and_json_negotiation(tmp_path, monkeypatch):
    """Server-rendered HTML for browsers; JSON for API clients; both from live metrics."""
    import importlib.util
    data = tmp_path / "data"; data.mkdir()
    _make_operator_state(data)
    spec = importlib.util.spec_from_file_location("operator", DEPLOY / "operator.py")
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(DEPLOY))
    spec.loader.exec_module(mod)

    # Build an Operator over the prepared data dir (rehydrates; no network in ctor).
    class _NoAdapter:  # never called by the dashboard path
        pass
    from mandatehub.x402 import X402PaymentRequirements, BASE_SEPOLIA_USDC
    reqs = X402PaymentRequirements(
        scheme="exact", network="base-sepolia", max_amount_required="10000",
        asset=BASE_SEPOLIA_USDC, pay_to="0xEDd58c7C43Cd63059fBeC3E43527c45f8efb42B4",
        resource="http://x/quote", max_timeout_seconds=60,
        description="d", mime_type="application/json", extra={"name": "USDC", "version": "2"})
    op = mod.Operator(data, adapter=_NoAdapter(), requirements=reqs, budget_cents=1000000)
    html = mod._dashboard_html(op, "https://mandatehub.example")
    assert html.startswith("<!doctype html>")
    assert "USDC revenue" in html and "settlements" in html
    assert "0.010000" in html                 # revenue from the one settlement
    assert "mandatehub.example/quote" in html
    # well-formed
    from html.parser import HTMLParser
    class V(HTMLParser):
        void = {"meta", "link", "img", "br", "hr", "input", "area", "base", "col",
                "embed", "source", "track", "wbr"}
        def __init__(self): super().__init__(); self.st = []
        def handle_starttag(self, t, a):
            if t not in self.void: self.st.append(t)
        def handle_endtag(self, t):
            if t in self.void: return
            if self.st and self.st[-1] == t: self.st.pop()
            elif t in self.st:
                while self.st and self.st.pop() != t: pass
    p = V(); p.feed(html); assert not p.st, p.st


def test_rate_limit_enforced_and_survives_restart(tmp_path, monkeypatch):
    """MANDATEHUB_RATE_PER_MIN → rolling-window velocity cap, persisted across restart."""
    import importlib.util
    from datetime import datetime, timedelta, timezone
    monkeypatch.syspath_prepend(str(DEPLOY))
    spec = importlib.util.spec_from_file_location("operator", DEPLOY / "operator.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    from mandatehub import Currency, Money
    from mandatehub.x402 import X402PaymentRequirements, BASE_SEPOLIA_USDC
    USDC = Currency.USDC
    reqs = X402PaymentRequirements(
        scheme="exact", network="base-sepolia", max_amount_required="10000",
        asset=BASE_SEPOLIA_USDC, pay_to="0xEDd58c7C43Cd63059fBeC3E43527c45f8efb42B4",
        resource="http://x/quote", max_timeout_seconds=60, description="d",
        mime_type="application/json", extra={"name": "USDC", "version": "2"})

    data = tmp_path / "data"; data.mkdir()
    op = mod.Operator(data, adapter=object(), requirements=reqs,
                      budget_cents=1000000, rate_per_min=2)
    now = datetime.now(timezone.utc)
    pay = op.merchant_account_id
    # two settlements in the window are fine
    for i in range(2):
        r = op.engine.settle_intent(mandate_id=op.mandate_id, intent_id=f"i{i}",
                                    payee_account_id=pay, amount=Money(10000, USDC),
                                    purpose="API_CALL", at=now + timedelta(seconds=i))
        assert r.decision == "SETTLED"
    # the third within 60s is rate-limited
    ok, reason, _ = op.engine.preauthorize(mandate_id=op.mandate_id, intent_id="i2",
        payee_account_id=pay, amount=Money(10000, USDC), purpose="API_CALL",
        at=now + timedelta(seconds=3))
    assert (ok, reason) == (False, "WINDOW_VELOCITY_EXCEEDED")

    # RESTART: a fresh Operator over the same dir must re-apply the rate limit from mandate.json
    op2 = mod.Operator(data, adapter=object(), requirements=reqs, budget_cents=1000000)
    ok, reason, _ = op2.engine.preauthorize(mandate_id=op2.mandate_id, intent_id="i9",
        payee_account_id=pay, amount=Money(10000, USDC), purpose="API_CALL",
        at=now + timedelta(seconds=4))
    assert (ok, reason) == (False, "WINDOW_VELOCITY_EXCEEDED")
    # but after the window passes, it recovers
    ok, reason, _ = op2.engine.preauthorize(mandate_id=op2.mandate_id, intent_id="i9",
        payee_account_id=pay, amount=Money(10000, USDC), purpose="API_CALL",
        at=now + timedelta(seconds=120))
    assert ok, reason


def test_products_ecb_parse_cache_and_gate(monkeypatch):
    import importlib
    sys.path.insert(0, str(DEPLOY))
    import products
    importlib.reload(products)
    FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
 <Cube><Cube time="2026-07-21">
  <Cube currency="USD" rate="1.1418"/><Cube currency="JPY" rate="185.82"/>
 </Cube></Cube></gesmes:Envelope>"""
    parsed = products._parse_ecb(FIXTURE)
    assert parsed == {"date": "2026-07-21", "rates": {"JPY": "185.82", "USD": "1.1418"}}

    # inject fetch: first works, later fails -> cache serves, then stale gate closes
    calls = {"n": 0}
    def fake_fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            return parsed
        raise RuntimeError("ecb down")
    monkeypatch.setattr(products, "_fetch_ecb", fake_fetch)
    products._ecb_cache.update({"at": 0.0, "data": None})
    q = products.ecb_quote(now=1000.0)
    assert q["ecb_date"] == "2026-07-21" and "artifact_sha256" in q
    # within TTL: cache, no refetch
    assert products.ecb_quote(now=1100.0)["rates"]["USD"] == "1.1418"
    assert calls["n"] == 1
    # after TTL with ECB down: still serves cached (grace)
    assert products.ecb_quote(now=1000.0 + products.ECB_TTL_SECONDS + 5) is not None
    # beyond MAX_AGE with ECB down: fail-closed -> None (never charge)
    assert products.ecb_quote(now=1000.0 + products.ECB_MAX_AGE_SECONDS + 5) is None


def test_products_verify_tx_offline_paths():
    sys.path.insert(0, str(DEPLOY))
    from products import verify_usdc_tx
    assert verify_usdc_tx("nope")["verdict"] == "INVALID_HASH"
    assert verify_usdc_tx("0x" + "a" * 63)["verdict"] == "INVALID_HASH"
    # RPC unreachable -> stated, not raised
    v = verify_usdc_tx("0x" + "a" * 64, rpc_url="https://127.0.0.1:1")
    assert v["verdict"] == "RPC_UNAVAILABLE"


def test_product_catalog_all_build_and_gate():
    import base64
    import json as _json
    sys.path.insert(0, str(DEPLOY))
    sys.path.insert(0, str(DEPLOY / "assets" / "keystone"))
    import products as P
    # availability all True locally (assets vendored; ECB may fetch — tolerate offline)
    cs = P.catalog_summary()
    assert {"fx", "qswap", "audit-verify", "verify-tx", "govern-verify"} <= set(cs)
    # qswap: static, deterministic, hashed
    q = P.qswap_matrix({"matrix": "both"})
    assert len(q["fidelity"]) == 16 and len(q["swap"]) == 9 and "artifact_sha256" in q
    # verify-tx: offline verdicts
    assert P.CATALOG["verify-tx"].build({"tx": "bad"})["verdict"] == "INVALID_HASH"
    # audit-verify: build a valid + a tampered submission
    from audit import AuditLog
    from anchor import make_signed_anchor
    from dataclasses import asdict
    log = AuditLog()
    for i in range(3):
        log.append(f"e{i}", "intent", x=i)
    a = make_signed_anchor(log, b"secret")
    recs = [asdict(r) for r in log.records()]
    good = {"records": recs, "anchor": {"head": a.head, "length": a.length,
            "signature": a.signature, "key_id": a.key_id}, "key": b"secret".hex()}
    enc = lambda d: base64.b64encode(_json.dumps(d).encode()).decode()
    assert P.keystone_verify({"data": enc(good)})["is_valid"] is True
    bad = dict(good); bad["records"] = [dict(r) for r in recs]; bad["records"][0]["data"] = {"x": 9}
    assert P.keystone_verify({"data": enc(bad)})["is_valid"] is False
    assert "error" in P.keystone_verify({})  # missing data


def test_wave2_products_pyverify_openunit_kairos():
    import base64
    import io
    import zipfile
    sys.path.insert(0, str(DEPLOY))
    import products as P
    cs = P.catalog_summary()
    assert set(cs) == {"fx", "qswap", "audit-verify", "verify-tx",
                       "govern-verify", "openunit", "kairos"}
    # govern-verify (pure Python): genuine passes, tampered fails with the Ed25519 code
    assert P.govern_verify({"bundle": "genuine"})["exit_code"] == 0
    assert P.govern_verify({"bundle": "tampered"})["exit_code"] == 21
    # caller-submitted zip of the genuine bundle verifies
    buf = io.BytesIO()
    root = DEPLOY / "assets" / "pyverify" / "genuine.bundle"
    with zipfile.ZipFile(buf, "w") as z:
        for f in root.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(root))
    up = P.govern_verify({"data": base64.b64encode(buf.getvalue()).decode()})
    assert up["exit_code"] == 0 and up["bundle"] == "caller-submitted"
    # zip-slip and size guards
    assert "error" in P.govern_verify({"data": base64.b64encode(b"nope").decode()})
    # openunit: live re-verification on every sale
    o = P.openunit_value({})
    assert o["reverified_now"] is True and o["numeraire"] == "USD"
    # kairos: honest as-of + bounded top
    k = P.kairos_scores({"top": "5"})
    assert k["as_of"] == "2026-04-16" and len(k["top"]) == 5
    assert "NOT live" in k["staleness_note"]
    k2 = P.kairos_scores({"top": "99999"})
    assert len(k2["top"]) <= 300


def test_security_review_fixes():
    import base64
    import io
    import time
    import zipfile
    sys.path.insert(0, str(DEPLOY))
    import products as P
    # fx: oversized amount -> clean error, no OverflowError
    assert "error" in P.fx_convert({"from": "USD", "to": "JPY", "amount": "9" * 400})
    assert P.fx_convert({"from": "USD", "to": "JPY", "amount": "0"}).get("error")
    # fx: Decimal cross-rate still correct for a normal amount
    assert P.fx_convert({"from": "EUR", "to": "EUR", "amount": "1000"})["conversion"]["to_minor_units"] == 1000
    # ECB negative-cache: a failing fetch is not retried within the backoff window
    P._ecb_cache.update({"at": 0.0, "data": {"date": "x", "rates": {"USD": "1.1"}}, "last_attempt": -1e18})
    calls = {"n": 0}
    orig = P._fetch_ecb
    def boom():
        calls["n"] += 1
        raise RuntimeError("down")
    P._fetch_ecb = boom
    try:
        t = time.time() + P.ECB_TTL_SECONDS + 10
        for _ in range(5):
            P._ecb_data(now=t)
        assert calls["n"] == 1  # one attempt, then backoff
    finally:
        P._fetch_ecb = orig
    # zip-bomb: a tiny zip expanding past the decompressed cap is rejected before extraction
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big", b"\0" * (50 * 1024 * 1024))
    assert "too large" in P.govern_verify({"data": base64.b64encode(buf.getvalue()).decode()})["error"]
    # detail path redaction
    assert "/" not in P._last_line_safe("fail at /private/var/T/mh-bundle-x/manifest.json").split("manifest")[0][-3:]
