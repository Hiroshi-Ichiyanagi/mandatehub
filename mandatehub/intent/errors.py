"""
intent/errors.py — intent パッケージ共通の例外（依存を持たない葉モジュール）。
"""

from __future__ import annotations


class MandateError(Exception):
    """委任枠・ポリシーの構成が不正なとき（呼び出し側のバグ）に送出される。"""


class SettlementIntegrityError(Exception):
    """元帳から決済を再構成する際、構造とメタデータが矛盾したとき（fail-closed）。"""
