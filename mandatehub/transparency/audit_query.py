"""
transparency/audit_query.py — 監査ログに対する純粋な as-of クエリ。

`AuditLog.latest_hash()` はチェーンの現在の先頭（グローバル）を返すため、
「snapshot_at 時点のコミットメント」には使えない。証明アーティファクトが
決定論的であるためには、コミットメントが (ログ内容, snapshot_at) の純関数で
あり、snapshot_at より後に発生したイベントを含まないことが必要。

この関数はそれを提供する（intent / execution の両方が依存する共有ヘルパー。
どちらのパッケージにも属さないため transparency に置く）。
"""

from __future__ import annotations

from datetime import datetime

from mandatehub.transparency.audit_log import GENESIS_HASH, AuditLog


def audit_root_as_of(audit_log: AuditLog | None, snapshot_at: datetime) -> str:
    """timestamp <= snapshot_at である最後のイベントの event_hash を返す。

    該当イベントが無ければ GENESIS_HASH。純関数：同一のログ内容 + 同一の
    snapshot_at は同一のコミットメントを返し、snapshot_at より後のイベントは
    除外する。ウォールクロック（datetime.now()）は一切参照しない。
    """
    if audit_log is None:
        return GENESIS_HASH
    chosen = GENESIS_HASH
    for ev in audit_log.iter_events():  # sequence 順
        if ev.timestamp <= snapshot_at:
            chosen = ev.event_hash
        # break しない：append が僅かに時刻前後しても決定論的に選べるよう全走査する
    return chosen
