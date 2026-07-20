"""transparency/audit_log.py に対するテスト。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.transparency.audit_log import GENESIS_HASH, AuditLog


class TestAuditLog:
    def test_first_event_uses_genesis_hash(self):
        log = AuditLog(":memory:")
        ev = log.append("test", {"foo": "bar"})
        assert ev.sequence == 0
        assert ev.prev_hash == GENESIS_HASH

    def test_chain_links_correctly(self):
        log = AuditLog(":memory:")
        ev1 = log.append("event1", {"data": 1})
        ev2 = log.append("event2", {"data": 2})
        ev3 = log.append("event3", {"data": 3})

        assert ev2.prev_hash == ev1.event_hash
        assert ev3.prev_hash == ev2.event_hash
        assert ev1.sequence == 0
        assert ev2.sequence == 1
        assert ev3.sequence == 2

    def test_chain_verification_passes_for_valid_chain(self):
        log = AuditLog(":memory:")
        for i in range(20):
            log.append(f"event_{i}", {"index": i, "payload": "data" * 10})
        is_valid, err = log.verify_chain()
        assert is_valid, err
        assert err is None

    def test_chain_verification_detects_payload_tampering(self):
        log = AuditLog(":memory:")
        log.append("ev1", {"value": 100})
        log.append("ev2", {"value": 200})
        log.append("ev3", {"value": 300})

        # 直接DB操作で payload を改竄
        log._conn.execute(
            "UPDATE audit_events SET payload = ? WHERE sequence = ?",
            ('{"value": 999}', 1),  # ev2 の payload 改竄
        )

        is_valid, err = log.verify_chain()
        assert not is_valid
        assert err is not None
        assert "Event hash mismatch" in err

    def test_chain_verification_detects_hash_replacement(self):
        log = AuditLog(":memory:")
        log.append("ev1", {})
        log.append("ev2", {})
        log.append("ev3", {})

        # event_hash を直接書き換え
        log._conn.execute(
            "UPDATE audit_events SET event_hash = ? WHERE sequence = ?",
            ("0" * 64, 1),
        )

        is_valid, err = log.verify_chain()
        assert not is_valid
        assert err is not None

    def test_event_count(self):
        log = AuditLog(":memory:")
        assert log.event_count() == 0
        log.append("e", {})
        log.append("e", {})
        assert log.event_count() == 2

    def test_latest_hash_starts_as_genesis(self):
        log = AuditLog(":memory:")
        assert log.latest_hash() == GENESIS_HASH

    def test_latest_hash_updates(self):
        log = AuditLog(":memory:")
        ev = log.append("e", {})
        assert log.latest_hash() == ev.event_hash

    def test_iter_events_in_order(self):
        log = AuditLog(":memory:")
        for i in range(5):
            log.append(f"e_{i}", {"i": i})
        events = list(log.iter_events())
        assert len(events) == 5
        assert [e.payload["i"] for e in events] == [0, 1, 2, 3, 4]

    def test_get_event_by_sequence(self):
        log = AuditLog(":memory:")
        log.append("a", {"x": 1})
        log.append("b", {"x": 2})
        log.append("c", {"x": 3})

        ev = log.get_event(1)
        assert ev.event_type == "b"
        assert ev.payload == {"x": 2}

    def test_get_nonexistent_event_raises(self):
        log = AuditLog(":memory:")
        log.append("a", {})
        with pytest.raises(ValueError):
            log.get_event(99)

    def test_canonical_serialization_consistency(self):
        """同じpayloadは順序に関係なく同じハッシュを生成する。"""
        log1 = AuditLog(":memory:")
        log2 = AuditLog(":memory:")

        ts = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
        # 異なる順序でdictを構築
        ev1 = log1.append("e", {"a": 1, "b": 2, "c": 3}, timestamp=ts)
        ev2 = log2.append("e", {"c": 3, "a": 1, "b": 2}, timestamp=ts)

        assert ev1.event_hash == ev2.event_hash
