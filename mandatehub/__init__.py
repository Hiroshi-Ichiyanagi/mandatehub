"""mandatehub — provable autonomous machine-to-machine payment.

A self-contained **verification core** for two directions of 24/7 value transfer:

- **intent / account-abstraction (4)** — a `Mandate` is a pre-funded, budget-bounded
  authorization; an autonomous agent settles M2M intents within it, and a `ProofOfMandate`
  lets anyone verify offline that the budget was never exceeded. With spend policy, session
  keys, batch, lifecycle, and replay/monotonic-time protection.
- **best execution / MEV-arbitrage recapture (3)** — a solver auction fills an intent at the
  best disclosed cost and the price-improvement surplus is split (user rebate / operator
  margin / gas) integer-exactly, with `ProofOfBestExecution` + `ProofOfSurplusRecapture`.

Deterministic, offline-verifiable, append-only double-entry, **standard library only** — no
on-chain execution, no third-party runtime dependencies. See README.md.
"""

from mandatehub.core import (
    Account,
    Currency,
    Entry,
    Ledger,
    LedgerStorage,
    Money,
    OwnerType,
    SQLiteLedgerStorage,
    Transaction,
    TransactionBuilder,
    TransactionStatus,
)
from mandatehub.execution import (
    AuctionOutcome,
    CyclicArbOpportunity,
    ExecutionAccounts,
    PoolEdge,
    PoolGraph,
    ProofOfBestExecution,
    ProofOfBestExecutionGenerator,
    ProofOfSurplusRecapture,
    ProofOfSurplusRecaptureGenerator,
    RouteQuote,
    SolverBid,
    SplitAllocation,
    SurplusEvent,
    SurplusSplitPolicy,
    compute_split,
    find_best_arbitrage_cycle,
    run_auction,
    select_best_route,
)
from mandatehub.intent import (
    AuctionSettlementResult,
    BatchSettlementResult,
    DENIAL_ORDER,
    EpochSpec,
    IntentRequest,
    IntentSettlementEngine,
    IntentSettlementResult,
    Mandate,
    MandateError,
    MandateLifecycleView,
    MandatePortfolioProof,
    MandatePortfolioProofGenerator,
    MandateState,
    ProofOfMandate,
    ProofOfMandateGenerator,
    SettlementRecord,
    SpendPolicy,
    audit_root_as_of,
)
from mandatehub.intent.bridge import settle_batch_via_auction, settle_via_auction
from mandatehub.transparency import (
    AuditEvent,
    AuditLog,
    GENESIS_HASH,
    MerkleLeaf,
    MerkleProof,
    MerkleTree,
    verify_proof_with_node_prefix,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # ledger primitives (vendored)
    "Account",
    "Currency",
    "Entry",
    "Ledger",
    "LedgerStorage",
    "Money",
    "OwnerType",
    "SQLiteLedgerStorage",
    "Transaction",
    "TransactionBuilder",
    "TransactionStatus",
    # transparency primitives (vendored)
    "AuditEvent",
    "AuditLog",
    "GENESIS_HASH",
    "MerkleLeaf",
    "MerkleProof",
    "MerkleTree",
    "verify_proof_with_node_prefix",
    "audit_root_as_of",
    # intent (4)
    "IntentSettlementEngine",
    "IntentSettlementResult",
    "IntentRequest",
    "BatchSettlementResult",
    "AuctionSettlementResult",
    "SettlementRecord",
    "Mandate",
    "MandateError",
    "MandateState",
    "MandateLifecycleView",
    "SpendPolicy",
    "EpochSpec",
    "DENIAL_ORDER",
    "ProofOfMandate",
    "ProofOfMandateGenerator",
    "MandatePortfolioProof",
    "MandatePortfolioProofGenerator",
    "settle_via_auction",
    "settle_batch_via_auction",
    # execution (3)
    "ExecutionAccounts",
    "RouteQuote",
    "select_best_route",
    "SolverBid",
    "AuctionOutcome",
    "run_auction",
    "SurplusSplitPolicy",
    "SplitAllocation",
    "SurplusEvent",
    "compute_split",
    "PoolGraph",
    "PoolEdge",
    "CyclicArbOpportunity",
    "find_best_arbitrage_cycle",
    "ProofOfBestExecution",
    "ProofOfBestExecutionGenerator",
    "ProofOfSurplusRecapture",
    "ProofOfSurplusRecaptureGenerator",
]
