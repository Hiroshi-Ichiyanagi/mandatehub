"""
genesis-keystone :: tamper-evident audit log (Tier P, disclosure-safe)

The composition layer's accountability backbone. Every intent's journey through
the seam (policy -> kernel verdict -> reserve -> confirmation -> settlement) is
appended as a hash-chained record. Any later edit to a past record breaks the
chain, so `verify()` gives cryptographic evidence that the trail is intact.

stdlib only. Backed by a JSONL file (inspectable, append-only) or in-memory.
Carries no node mechanism -- only the decisions made at the seam and opaque refs.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

_GENESIS_HASH = "0" * 64


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    ts: str
    event: str
    intent_id: str
    request_id: Optional[str]
    data: dict
    prev_hash: str
    hash: str


class AuditLog:
    """Append-only, hash-chained audit log. Thread-safe.

    path=None -> in-memory only (tests/demos). path=<file> -> durable JSONL that
    is reloaded (and its chain continued) on construction.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._records: list[AuditRecord] = []
        self._last_hash = _GENESIS_HASH
        self._seq = 0
        if path and os.path.exists(path):
            self._load()

    def _load(self) -> None:
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = AuditRecord(**json.loads(line))
                self._records.append(rec)
                self._last_hash = rec.hash
                self._seq = rec.seq

    @staticmethod
    def _digest(prev_hash: str, seq: int, ts: str, event: str,
                intent_id: str, request_id: Optional[str], data: dict) -> str:
        payload = json.dumps(
            {"prev": prev_hash, "seq": seq, "ts": ts, "event": event,
             "intent_id": intent_id, "request_id": request_id, "data": data},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def append(self, event: str, intent_id: str,
               request_id: Optional[str] = None, **data) -> AuditRecord:
        with self._lock:
            self._seq += 1
            ts = _utc_now_iso()
            h = self._digest(self._last_hash, self._seq, ts, event, intent_id, request_id, data)
            rec = AuditRecord(seq=self._seq, ts=ts, event=event, intent_id=intent_id,
                              request_id=request_id, data=data,
                              prev_hash=self._last_hash, hash=h)
            self._records.append(rec)
            self._last_hash = h
            if self._path:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(rec), separators=(",", ":")) + "\n")
            return rec

    def verify(self, *, expected_head: Optional[str] = None,
               expected_len: Optional[int] = None) -> bool:
        """Recompute the chain; True iff intact.

        Default (no args) is the original chain-internal check: it proves no
        record was edited in place and no link was broken or reordered. It
        CANNOT detect truncation of trailing records, because a shorter prefix
        of a valid chain is itself a valid chain (the "tamper-evident" gap for
        the drop-records vector).

        To close that gap, pass a previously-captured anchor (decision D-3):
          - ``expected_head``: the head hash captured earlier. The head of a
            hash chain is a commitment to the ENTIRE history, so a mismatch
            detects truncation, deletion, reorder, or any edit.
          - ``expected_len``: the record count captured earlier — a cheaper,
            precise truncation diagnostic.
        Both are keyword-only and default ``None`` → existing callers are
        unaffected (byte-identical behavior).
        """
        prev = _GENESIS_HASH
        for rec in self._records:
            if rec.prev_hash != prev:
                return False
            if self._digest(prev, rec.seq, rec.ts, rec.event,
                            rec.intent_id, rec.request_id, rec.data) != rec.hash:
                return False
            prev = rec.hash
        # D-3: anchor checks (only when an expected value is supplied).
        if expected_len is not None and len(self._records) != expected_len:
            return False
        if expected_head is not None and prev != expected_head:
            return False
        return True

    def records(self) -> list[AuditRecord]:
        return list(self._records)

    def trace(self, intent_id: str) -> list[AuditRecord]:
        return [r for r in self._records if r.intent_id == intent_id]

    @property
    def head(self) -> str:
        return self._last_hash
