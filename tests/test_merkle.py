"""transparency/merkle.py に対するテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from mandatehub.transparency.merkle import (
    MerkleLeaf,
    MerkleTree,
    verify_proof_with_node_prefix,
)


class TestMerkleTree:
    def test_single_leaf_tree(self):
        leaves = [MerkleLeaf(account_id="a1", balance_cents=100, currency_code="JPY")]
        tree = MerkleTree(leaves)
        assert tree.leaf_count == 1
        # ルートはリーフのハッシュそのもの
        assert tree.root_hash == leaves[0].hash_hex

    def test_two_leaves_tree(self):
        leaves = [
            MerkleLeaf(account_id="a1", balance_cents=100, currency_code="JPY"),
            MerkleLeaf(account_id="a2", balance_cents=200, currency_code="JPY"),
        ]
        tree = MerkleTree(leaves)
        assert tree.leaf_count == 2
        assert tree.total_balance_cents() == 300
        # ルートハッシュは決定論的
        tree2 = MerkleTree(leaves)
        assert tree.root_hash == tree2.root_hash

    def test_deterministic_construction(self):
        """同じリーフ集合からは常に同じルートハッシュが得られる。"""
        leaves = [
            MerkleLeaf(account_id=f"a{i}", balance_cents=i * 100, currency_code="JPY")
            for i in range(10)
        ]
        tree1 = MerkleTree(leaves)
        tree2 = MerkleTree(leaves)
        assert tree1.root_hash == tree2.root_hash

    def test_different_leaves_yield_different_root(self):
        leaves_a = [MerkleLeaf(account_id="a1", balance_cents=100, currency_code="JPY")]
        leaves_b = [MerkleLeaf(account_id="a1", balance_cents=101, currency_code="JPY")]
        assert MerkleTree(leaves_a).root_hash != MerkleTree(leaves_b).root_hash

    def test_proof_verification_two_leaves(self):
        leaves = [
            MerkleLeaf(account_id="a1", balance_cents=100, currency_code="JPY"),
            MerkleLeaf(account_id="a2", balance_cents=200, currency_code="JPY"),
        ]
        tree = MerkleTree(leaves)
        proof = tree.proof_for("a1")
        assert verify_proof_with_node_prefix(proof)

    def test_proof_verification_many_leaves(self):
        leaves = [
            MerkleLeaf(account_id=f"acc_{i}", balance_cents=i * 1000, currency_code="JPY")
            for i in range(100)
        ]
        tree = MerkleTree(leaves)
        # 全てのリーフについて Proof が成立する
        for leaf in leaves:
            proof = tree.proof_for(leaf.account_id)
            assert verify_proof_with_node_prefix(proof), f"Proof failed for {leaf.account_id}"

    def test_proof_verification_odd_leaf_count(self):
        """リーフ数が奇数（複製ペア発生）でも検証が通る。"""
        leaves = [
            MerkleLeaf(account_id=f"acc_{i}", balance_cents=i, currency_code="JPY")
            for i in range(7)
        ]
        tree = MerkleTree(leaves)
        for leaf in leaves:
            proof = tree.proof_for(leaf.account_id)
            assert verify_proof_with_node_prefix(proof)

    def test_tampered_balance_breaks_proof(self):
        leaves = [
            MerkleLeaf(account_id="a1", balance_cents=100, currency_code="JPY"),
            MerkleLeaf(account_id="a2", balance_cents=200, currency_code="JPY"),
        ]
        tree = MerkleTree(leaves)
        proof = tree.proof_for("a1")

        # リーフの残高を改竄するとProofが失敗する
        from dataclasses import replace
        tampered_leaf = replace(proof.leaf, balance_cents=999)
        tampered_proof = replace(proof, leaf=tampered_leaf)
        assert not verify_proof_with_node_prefix(tampered_proof)

    def test_unknown_account_raises(self):
        leaves = [MerkleLeaf(account_id="a1", balance_cents=100, currency_code="JPY")]
        tree = MerkleTree(leaves)
        with pytest.raises(ValueError):
            tree.proof_for("nonexistent")

    def test_total_balance(self):
        leaves = [
            MerkleLeaf(account_id=f"a{i}", balance_cents=100, currency_code="JPY")
            for i in range(50)
        ]
        tree = MerkleTree(leaves)
        assert tree.total_balance_cents() == 5000

    def test_empty_leaves_rejected(self):
        with pytest.raises(ValueError):
            MerkleTree([])
