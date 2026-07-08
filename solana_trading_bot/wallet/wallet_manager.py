"""Secure wallet handling.

Rules enforced here:

* the private key is read from the ``PRIVATE_KEY`` environment variable only;
* the key is never logged, never returned, never persisted anywhere;
* ``repr``/``str`` expose only the public address;
* an optional expected address is verified before the wallet is usable —
  a mismatch is a hard error, not a warning.

The trading wallet is assumed to hold limited capital only. Never point this
bot at a wallet you cannot afford to lose.
"""

from __future__ import annotations

import json
import os
from typing import Optional


class WalletError(Exception):
    pass


class WalletManager:
    """Loads a Solana keypair from the environment and signs transactions."""

    def __init__(self, expected_address: Optional[str] = None) -> None:
        raw = os.environ.get("PRIVATE_KEY", "").strip()
        if not raw:
            raise WalletError(
                "PRIVATE_KEY is not set. Put it in the environment (or .env); "
                "never hard-code or commit it."
            )
        self._keypair = self._parse_key(raw)
        del raw
        self.address: str = str(self._keypair.pubkey())

        if expected_address and self.address != expected_address:
            # Deliberately do not include any key material in the error.
            raise WalletError(
                "Wallet mismatch: loaded wallet does not match "
                "EXPECTED_WALLET_ADDRESS. Refusing to trade."
            )

    @staticmethod
    def _parse_key(raw: str):
        """Accept a base58 secret key or a JSON byte array (solana-keygen)."""
        from solders.keypair import Keypair  # local import: keeps non-live code importable

        try:
            if raw.startswith("["):
                secret = bytes(json.loads(raw))
                return Keypair.from_bytes(secret)
            return Keypair.from_base58_string(raw)
        except Exception as exc:  # noqa: BLE001 — normalize, never echo the key
            raise WalletError("PRIVATE_KEY could not be parsed (invalid format)") from exc

    def pubkey(self):
        return self._keypair.pubkey()

    def sign_versioned_transaction(self, tx) -> bytes:
        """Sign a ``solders`` ``VersionedTransaction`` and return raw bytes."""
        from solders.transaction import VersionedTransaction

        signed = VersionedTransaction(tx.message, [self._keypair])
        return bytes(signed)

    def validate(self) -> None:
        """Sanity check before any execution: signing must work."""
        try:
            self._keypair.sign_message(b"wallet-validation")
        except Exception as exc:  # noqa: BLE001
            raise WalletError("Wallet failed signing validation") from exc

    def __repr__(self) -> str:  # never expose the key
        return f"WalletManager(address={self.address})"

    __str__ = __repr__
