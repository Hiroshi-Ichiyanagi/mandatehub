"""
intent/audit_asof.py — audit_root_as_of の intent 名前空間への再輸出。

実体は transparency.audit_query にある（intent と execution の双方が依存できるよう
共有の場所に置く）。証明のコミットメントは latest_hash() ではなくこの as-of 版を使う。
"""

from __future__ import annotations

from mandatehub.transparency.audit_query import audit_root_as_of

__all__ = ["audit_root_as_of"]
