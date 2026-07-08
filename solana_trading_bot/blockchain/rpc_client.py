"""Solana RPC access with retries and heartbeat integration.

Every RPC failure is reported to the heartbeat monitor so the risk engine can
halt trading immediately — the bot must never act on a chain it cannot see.
Retries use bounded exponential backoff; after the retries are exhausted the
error is raised, never swallowed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class RpcError(Exception):
    pass


class RpcClient:
    """Thin async wrapper around ``solana-py``'s ``AsyncClient``."""

    def __init__(self, url: str, heartbeat=None, max_retries: int = 3) -> None:
        self._url = url
        self._heartbeat = heartbeat
        self._max_retries = max_retries
        self._client = None

    async def connect(self) -> None:
        from solana.rpc.async_api import AsyncClient

        self._client = AsyncClient(self._url)
        await self.check_health()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _call(self, description: str, fn: Callable, *args: Any, **kwargs: Any):
        if self._client is None:
            raise RpcError("RPC client is not connected")
        delay = 0.5
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                result = await fn(*args, **kwargs)
                if self._heartbeat is not None:
                    self._heartbeat.beat_rpc()
                return result
            except Exception as exc:  # noqa: BLE001 — report, retry, then raise
                last_exc = exc
                logger.warning("RPC %s failed (attempt %d/%d): %s",
                               description, attempt, self._max_retries, exc)
                if attempt < self._max_retries:
                    await asyncio.sleep(delay)
                    delay *= 2
        if self._heartbeat is not None:
            self._heartbeat.report_rpc_failure(f"{description}: {last_exc}")
        raise RpcError(f"RPC {description} failed after {self._max_retries} attempts") from last_exc

    async def check_health(self) -> bool:
        async def _health():
            resp = await self._client.get_latest_blockhash()
            return resp.value is not None

        return bool(await self._call("get_latest_blockhash", _health))

    async def get_sol_balance(self, pubkey) -> float:
        """Balance of native SOL, in SOL."""
        resp = await self._call("get_balance", self._client.get_balance, pubkey)
        return resp.value / 1_000_000_000

    async def get_token_balance(self, owner, mint) -> float:
        """Total balance of an SPL token across the owner's token accounts."""
        from solana.rpc.types import TokenAccountOpts

        resp = await self._call(
            "get_token_accounts_by_owner",
            self._client.get_token_accounts_by_owner_json_parsed,
            owner,
            TokenAccountOpts(mint=mint),
        )
        total = 0.0
        for acc in resp.value:
            info = acc.account.data.parsed["info"]["tokenAmount"]
            total += float(info.get("uiAmount") or 0.0)
        return total

    async def send_raw_transaction(self, raw_tx: bytes) -> str:
        from solana.rpc.types import TxOpts

        resp = await self._call(
            "send_raw_transaction",
            self._client.send_raw_transaction,
            raw_tx,
            opts=TxOpts(skip_preflight=False, max_retries=2),
        )
        return str(resp.value)

    async def confirm_transaction(self, signature: str, timeout_s: float = 60.0) -> bool:
        """Poll for confirmation. Returns False (never hangs forever) on timeout."""
        from solders.signature import Signature

        sig = Signature.from_string(signature)
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            resp = await self._call(
                "get_signature_statuses", self._client.get_signature_statuses, [sig]
            )
            status = resp.value[0]
            if status is not None:
                if status.err is not None:
                    raise RpcError(f"Transaction {signature} failed on-chain: {status.err}")
                if status.confirmation_status is not None:
                    return True
            await asyncio.sleep(1.0)
        return False
