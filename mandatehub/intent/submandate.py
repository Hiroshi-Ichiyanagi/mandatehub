"""
intent/submandate.py — セッション鍵 / サブ委任枠のツリー操作（純粋）。

集約は **子孫 ID 集合のメンバーシップ** で行い、部分文字列マッチは一切使わない
（"root/a" と "root/ab" が衝突しない）。委任枠レジストリ（{id: Mandate}）を受け取り、
.mandate_id / .parent_mandate_id のみを参照する（Mandate 型は import しない）。
"""

from __future__ import annotations

MAX_DELEGATION_DEPTH = 8


def children_map(mandates: dict) -> dict[str, list[str]]:
    """parent_id -> [child_id...] を構築する。"""
    kids: dict[str, list[str]] = {}
    for mid, m in mandates.items():
        parent = getattr(m, "parent_mandate_id", None)
        if parent is not None:
            kids.setdefault(parent, []).append(mid)
    return kids


def descendant_ids(mandates: dict, root_id: str) -> set[str]:
    """root_id とその全子孫の ID 集合（root 自身を含む）。"""
    kids = children_map(mandates)
    out: set[str] = set()
    stack = [root_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(kids.get(cur, ()))
    return out


def ancestor_ids(mandates: dict, node_id: str) -> list[str]:
    """node_id の厳密な祖先（親→…→根）のリスト。"""
    out: list[str] = []
    seen: set[str] = {node_id}
    cur = getattr(mandates.get(node_id), "parent_mandate_id", None)
    while cur is not None and cur in mandates and cur not in seen:
        out.append(cur)
        seen.add(cur)
        cur = getattr(mandates.get(cur), "parent_mandate_id", None)
    return out


def depth_of(mandates: dict, node_id: str) -> int:
    """根からの深さ（根は 0）。"""
    return len(ancestor_ids(mandates, node_id))


def root_id_of(mandates: dict, node_id: str) -> str:
    anc = ancestor_ids(mandates, node_id)
    return anc[-1] if anc else node_id


def mandate_path(mandates: dict, node_id: str) -> str:
    """根から node までの ID を "/" で連結したパス。"""
    anc = ancestor_ids(mandates, node_id)
    return "/".join([*reversed(anc), node_id])
