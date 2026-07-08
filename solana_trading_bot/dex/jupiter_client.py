"""Jupiter aggregator HTTP client (quotes, swap transactions, prices).

All numbers coming back from Jupiter are normalized into the ``Quote``
dataclass so the rest of the system never touches raw API payloads. Errors
raise ``JupiterError`` — no silent fallbacks.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from solana_trading_bot.domain import Quote

logger = logging.getLogger(__name__)


class JupiterError(Exception):
    pass


class JupiterClient:
    def __init__(
        self,
        base_url: str = "https://quote-api.jup.ag/v6",
        price_url: str = "https://lite-api.jup.ag/price/v2",
        timeout_s: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._price_url = price_url
        self._http = httpx.AsyncClient(timeout=timeout_s)

    async def close(self) -> None:
        await self._http.aclose()

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
        base_decimals: int,
        quote_decimals: int,
        base_mint: str,
    ) -> Quote:
        """Fetch a swap quote. ``amount_raw`` is in the input mint's raw units."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(slippage_bps),
            "swapMode": "ExactIn",
        }
        try:
            resp = await self._http.get(f"{self._base_url}/quote", params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise JupiterError(f"quote request failed: {exc}") from exc
        if "error" in data:
            raise JupiterError(f"quote error: {data['error']}")

        in_amount = int(data["inAmount"])
        out_amount = int(data["outAmount"])
        impact = float(data.get("priceImpactPct") or 0.0)

        # Expected price expressed as quote units per base unit regardless of
        # swap direction, so validation code has one convention.
        if input_mint == base_mint:
            base_ui = in_amount / 10**base_decimals
            quote_ui = out_amount / 10**quote_decimals
        else:
            base_ui = out_amount / 10**base_decimals
            quote_ui = in_amount / 10**quote_decimals
        if base_ui <= 0:
            raise JupiterError("quote returned zero base amount")

        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=in_amount,
            out_amount=out_amount,
            price_impact_pct=impact,
            expected_price=quote_ui / base_ui,
            route_hops=len(data.get("routePlan", [])) or 1,
            raw=data,
        )

    async def get_swap_transaction(self, quote: Quote, user_pubkey: str) -> str:
        """Exchange a quote for a base64 serialized transaction to sign."""
        if quote.raw is None:
            raise JupiterError("quote has no raw payload; cannot build swap")
        body = {
            "quoteResponse": quote.raw,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
        }
        try:
            resp = await self._http.post(f"{self._base_url}/swap", json=body)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise JupiterError(f"swap request failed: {exc}") from exc
        tx = data.get("swapTransaction")
        if not tx:
            raise JupiterError(f"swap response missing transaction: {data}")
        return tx

    async def get_price(self, mint: str, vs_mint: Optional[str] = None) -> float:
        """Spot price from Jupiter's price API (USD unless ``vs_mint`` given)."""
        params = {"ids": mint}
        if vs_mint:
            params["vsToken"] = vs_mint
        try:
            resp = await self._http.get(self._price_url, params=params)
            resp.raise_for_status()
            data = resp.json()["data"][mint]
        except (httpx.HTTPError, KeyError, TypeError) as exc:
            raise JupiterError(f"price request failed: {exc}") from exc
        if data is None or data.get("price") is None:
            raise JupiterError(f"no price returned for {mint}")
        return float(data["price"])
