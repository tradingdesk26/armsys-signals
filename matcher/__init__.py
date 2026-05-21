"""
Inter-Agent Clearinghouse matching engine.

- quote_engine: 3-mode quote computation + EIP-712 signing
- intent_book: in-memory + SQLite order book
- matcher: priority-queue matcher

Tied to the InterAgentRepo.sol contract on Base mainnet.
"""

__version__ = "0.1.0"
