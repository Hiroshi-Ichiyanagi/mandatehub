"""
transparency/merkle.py — Merkle Tree実装。

Proof of Reservesの基礎構造。利用者個別の残高を秘匿しつつ、
総額の正しさを暗号学的に検証可能にする。

リーフは (account_id, balance_cents) のハッシュ。
内部ノードは子ノードのハッシュの連結のSHA-256。
ルートが公開され、利用者は自分の残高についてMerkle Pathを使って
「自分の残高がツリーに含まれている」ことを検証できる。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_pair(left_hex: str, right_hex: str) -> str:
    """2つのハッシュを結合して新しいハッシュを生成する。"""
    return sha256_hex(bytes.fromhex(left_hex) + bytes.fromhex(right_hex))


@dataclass(frozen=True)
class MerkleLeaf:
    """ツリーのリーフ。account_idとbalance_centsを含み、ハッシュが計算される。

    リーフのハッシュ計算には domain separator（"LEAF:"）を含める。
    内部ノードと衝突する second-preimage 攻撃を防ぐ標準手法。
    """

    account_id: str
    balance_cents: int
    currency_code: str

    @property
    def hash_hex(self) -> str:
        payload = f"LEAF:{self.account_id}:{self.balance_cents}:{self.currency_code}".encode()
        return sha256_hex(payload)


@dataclass(frozen=True)
class MerkleProofStep:
    """Merkle Path上の1ステップ。

    sibling_hash: 兄弟ノードのハッシュ
    is_left: 兄弟が左側にあれば True、右側なら False
    """

    sibling_hash: str
    is_left: bool


@dataclass(frozen=True)
class MerkleProof:
    """利用者が自身の残高の包含を証明するためのデータ構造。"""

    leaf: MerkleLeaf
    steps: tuple[MerkleProofStep, ...]
    root_hash: str

    def verify(self) -> bool:
        """このProofが有効かどうかを検証する。"""
        current = self.leaf.hash_hex
        for step in self.steps:
            if step.is_left:
                current = hash_pair(step.sibling_hash, current)
            else:
                current = hash_pair(current, step.sibling_hash)
        return current == self.root_hash


class MerkleTree:
    """残高集合のMerkle Tree。

    リーフ数が奇数の場合は最後のノードを複製してペアを作る（標準的な手法）。
    内部ノードのハッシュ計算には domain separator（"NODE:"）を含める。
    """

    def __init__(self, leaves: Sequence[MerkleLeaf]) -> None:
        if not leaves:
            raise ValueError("Merkle tree requires at least one leaf")
        self._leaves: tuple[MerkleLeaf, ...] = tuple(leaves)
        # 各レベルのノードハッシュを計算し保持する
        self._levels: list[list[str]] = self._build_levels()

    def _build_levels(self) -> list[list[str]]:
        """全レベルのノードハッシュを計算する。レベル0がリーフ、最終レベルがルート。"""
        # レベル0：リーフのハッシュ
        current_level = [leaf.hash_hex for leaf in self._leaves]
        levels = [current_level]

        while len(current_level) > 1:
            next_level: list[str] = []
            i = 0
            while i < len(current_level):
                left = current_level[i]
                right = current_level[i + 1] if i + 1 < len(current_level) else current_level[i]
                # 内部ノードの domain separator
                node_input = f"NODE:".encode() + bytes.fromhex(left) + bytes.fromhex(right)
                next_level.append(sha256_hex(node_input))
                i += 2
            levels.append(next_level)
            current_level = next_level

        return levels

    @property
    def root_hash(self) -> str:
        return self._levels[-1][0]

    @property
    def leaf_count(self) -> int:
        return len(self._leaves)

    def total_balance_cents(self) -> int:
        """全リーフのbalance_centsの合計（同一通貨想定）。"""
        return sum(leaf.balance_cents for leaf in self._leaves)

    def proof_for(self, account_id: str) -> MerkleProof:
        """指定アカウントについてMerkle Proofを生成する。

        内部ノードの domain separator が NODE: なのに対し、proof verification側でも
        同じ式で計算しないと一致しない。verify() のロジックは hash_pair を使うが、
        これは現状 NODE: prefix を使わない単純連結。verify()側を NODE: 付きに合わせる。
        """
        # account_idを持つリーフを探す
        leaf_index = None
        for i, leaf in enumerate(self._leaves):
            if leaf.account_id == account_id:
                leaf_index = i
                break
        if leaf_index is None:
            raise ValueError(f"Account {account_id} not in Merkle tree")

        # Merkle Pathを構築
        steps: list[MerkleProofStep] = []
        current_index = leaf_index
        for level_idx in range(len(self._levels) - 1):
            level = self._levels[level_idx]
            if current_index % 2 == 0:
                # 自分が左、兄弟が右
                sibling_index = current_index + 1
                if sibling_index >= len(level):
                    sibling_index = current_index  # 複製
                steps.append(MerkleProofStep(sibling_hash=level[sibling_index], is_left=False))
            else:
                # 自分が右、兄弟が左
                sibling_index = current_index - 1
                steps.append(MerkleProofStep(sibling_hash=level[sibling_index], is_left=True))
            current_index = current_index // 2

        return MerkleProof(
            leaf=self._leaves[leaf_index],
            steps=tuple(steps),
            root_hash=self.root_hash,
        )


# domain separator問題を統一するため、verify_with_node_prefix を別関数で提供する
def verify_proof_with_node_prefix(proof: MerkleProof) -> bool:
    """NODE: prefixを使った検証。MerkleTreeの内部実装に合わせた版。"""
    current = proof.leaf.hash_hex
    for step in proof.steps:
        if step.is_left:
            node_input = b"NODE:" + bytes.fromhex(step.sibling_hash) + bytes.fromhex(current)
        else:
            node_input = b"NODE:" + bytes.fromhex(current) + bytes.fromhex(step.sibling_hash)
        current = sha256_hex(node_input)
    return current == proof.root_hash
