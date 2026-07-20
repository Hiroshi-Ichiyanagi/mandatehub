"""
transparency/audit_log.py — ハッシュチェーンによる改竄検出可能な監査ログ。

各イベントは前のイベントのハッシュを含むため、過去のイベントを書き換えると
それ以降のすべてのハッシュが変わり、改竄が検出される。

このログは元帳の全イベントを記録し、規制当局への提出資料として、
そして利用者への透明性ダッシュボードのデータソースとして使われる。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator


GENESIS_HASH = "0" * 64  # 創世ハッシュ：チェーンの最初のエントリーの prev_hash


@dataclass(frozen=True)
class AuditEvent:
    """監査ログ上の1イベント。"""

    sequence: int
    event_type: str
    payload: dict[str, Any]
    timestamp: datetime
    prev_hash: str
    event_hash: str

    @staticmethod
    def compute_hash(
        sequence: int,
        event_type: str,
        payload: dict[str, Any],
        timestamp: datetime,
        prev_hash: str,
    ) -> str:
        """イベントハッシュを計算する。

        payloadはJSONで決定論的にシリアライズ（ソート付き）して入力にする。
        timestampはISO8601形式で固定。
        """
        canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        material = (
            f"AUDIT_v1\n"
            f"seq={sequence}\n"
            f"type={event_type}\n"
            f"payload={canonical_payload}\n"
            f"ts={timestamp.isoformat()}\n"
            f"prev={prev_hash}\n"
        )
        return hashlib.sha256(material.encode()).hexdigest()


_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_audit_events_type ON audit_events(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_events_timestamp ON audit_events(timestamp);
"""


class AuditLog:
    """SQLiteベースのハッシュチェーン監査ログ。

    append() でイベントを追加すると、前のイベントのハッシュを含めて新しいハッシュを計算し記録する。
    verify_chain() でチェーン全体の整合性を検証できる。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_AUDIT_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        timestamp: datetime | None = None,
    ) -> AuditEvent:
        """新しいイベントをログに追加する。"""
        timestamp = timestamp or datetime.now(timezone.utc)

        # 既存の最後のシーケンスとハッシュを取得
        last_row = self._conn.execute(
            "SELECT sequence, event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if last_row is None:
            sequence = 0
            prev_hash = GENESIS_HASH
        else:
            sequence = last_row[0] + 1
            prev_hash = last_row[1]

        event_hash = AuditEvent.compute_hash(
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            timestamp=timestamp,
            prev_hash=prev_hash,
        )

        self._conn.execute(
            "INSERT INTO audit_events (sequence, event_type, payload, timestamp, prev_hash, event_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                sequence,
                event_type,
                json.dumps(payload, sort_keys=True),
                timestamp.isoformat(),
                prev_hash,
                event_hash,
            ),
        )

        return AuditEvent(
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            timestamp=timestamp,
            prev_hash=prev_hash,
            event_hash=event_hash,
        )

    def get_event(self, sequence: int) -> AuditEvent:
        row = self._conn.execute(
            "SELECT sequence, event_type, payload, timestamp, prev_hash, event_hash "
            "FROM audit_events WHERE sequence = ?",
            (sequence,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No audit event with sequence {sequence}")
        return AuditEvent(
            sequence=row[0],
            event_type=row[1],
            payload=json.loads(row[2]),
            timestamp=datetime.fromisoformat(row[3]),
            prev_hash=row[4],
            event_hash=row[5],
        )

    def latest_hash(self) -> str:
        """最新のチェーンの先頭ハッシュ。公開コミットメントとして使う。"""
        row = self._conn.execute(
            "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else GENESIS_HASH

    def event_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()
        return row[0]

    def iter_events(self) -> Iterator[AuditEvent]:
        rows = self._conn.execute(
            "SELECT sequence, event_type, payload, timestamp, prev_hash, event_hash "
            "FROM audit_events ORDER BY sequence"
        ).fetchall()
        for row in rows:
            yield AuditEvent(
                sequence=row[0],
                event_type=row[1],
                payload=json.loads(row[2]),
                timestamp=datetime.fromisoformat(row[3]),
                prev_hash=row[4],
                event_hash=row[5],
            )

    def verify_chain(self) -> tuple[bool, str | None]:
        """チェーン全体の整合性を検証する。

        Returns:
            (is_valid, error_message). 改竄があれば error_message に内容が入る。
        """
        expected_prev_hash = GENESIS_HASH
        expected_sequence = 0

        for event in self.iter_events():
            if event.sequence != expected_sequence:
                return False, f"Sequence gap: expected {expected_sequence}, got {event.sequence}"
            if event.prev_hash != expected_prev_hash:
                return (
                    False,
                    f"Hash chain broken at sequence {event.sequence}: "
                    f"prev_hash {event.prev_hash} != expected {expected_prev_hash}",
                )

            recomputed_hash = AuditEvent.compute_hash(
                sequence=event.sequence,
                event_type=event.event_type,
                payload=event.payload,
                timestamp=event.timestamp,
                prev_hash=event.prev_hash,
            )
            if recomputed_hash != event.event_hash:
                return (
                    False,
                    f"Event hash mismatch at sequence {event.sequence}: "
                    f"stored {event.event_hash} != recomputed {recomputed_hash}. "
                    f"This indicates tampering with the event payload.",
                )

            expected_prev_hash = event.event_hash
            expected_sequence += 1

        return True, None
