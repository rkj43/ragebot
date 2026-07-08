"""Deserialize and sign swap transactions returned by Jupiter.

Jupiter's ``/swap`` endpoint returns a fully built, base64-encoded
``VersionedTransaction``. This module's only jobs are to decode it, verify it
pays out of the expected wallet, and have the wallet sign it. It never
constructs instructions by hand, which keeps the attack surface small.
"""

from __future__ import annotations

import base64


class TransactionBuildError(Exception):
    pass


class TransactionBuilder:
    def __init__(self, wallet) -> None:
        self._wallet = wallet

    def build_signed_swap(self, swap_transaction_b64: str) -> bytes:
        """Decode Jupiter's transaction, verify the fee payer, sign, return bytes."""
        from solders.transaction import VersionedTransaction

        try:
            raw = base64.b64decode(swap_transaction_b64)
            tx = VersionedTransaction.from_bytes(raw)
        except Exception as exc:  # noqa: BLE001
            raise TransactionBuildError("Could not decode swap transaction") from exc

        fee_payer = str(tx.message.account_keys[0])
        if fee_payer != self._wallet.address:
            raise TransactionBuildError(
                f"Swap transaction fee payer {fee_payer} does not match "
                f"our wallet {self._wallet.address}; refusing to sign."
            )
        return self._wallet.sign_versioned_transaction(tx)
