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
        """Spot USD price from Jupiter's price API v3.

        v3 quotes in USD only (``vs_mint`` is accepted for interface
        compatibility but ignored) — equivalent to USDC for our purposes.
        """
        info = await self.get_price_info(mint)
        return info["price"]

    async def get_price_info(self, mint: str) -> dict:
        """Price plus market context from price API v3.

        Returns ``{"price": float, "liquidity_usd": float | None}``.
        The v3 response is keyed by mint at the top level:
        ``{"<mint>": {"usdPrice": ..., "liquidity": ..., ...}}``.
        """
        try:
            resp = await self._http.get(self._price_url, params={"ids": mint})
            resp.raise_for_status()
            data = resp.json().get(mint)
        except httpx.HTTPError as exc:
            raise JupiterError(f"price request failed: {exc}") from exc
        if not data or data.get("usdPrice") is None:
            raise JupiterError(f"no price returned for {mint}")
        liquidity = data.get("liquidity")
        return {
            "price": float(data["usdPrice"]),
            "liquidity_usd": float(liquidity) if liquidity is not None else None,
        }
