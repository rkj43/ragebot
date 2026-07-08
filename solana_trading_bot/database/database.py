"""Database access: everything gets recorded.

``TradeLog`` is the single write interface used across the bot. A database
write failure is logged at CRITICAL and counted — it must never crash the
trading loop mid-flight (that could strand an open position), but it is also
never silent, and ``write_failures`` lets the health loop halt trading if the
audit trail is broken.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from solana_trading_bot.database.models import (
    Base, OrderRecord, RiskEventRecord, TradeRecord,
)

logger = logging.getLogger(__name__)


class TradeLog:
    def __init__(self, db_url: str = "sqlite:///solana_trading_bot.db") -> None:
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(self._engine, expire_on_commit=False)
        self.write_failures = 0

    def _write(self, record) -> None:
        try:
            with self._session_factory() as session:
                session.add(record)
                session.commit()
        except Exception:  # noqa: BLE001 — loud, counted, non-fatal
            self.write_failures += 1
            logger.critical("DATABASE WRITE FAILED (%d total) for %r",
                            self.write_failures, record, exc_info=True)

    def record_trade(self, token: str, strategy: str, entry: float,
                     exit_: float, size: float, profit_loss: float) -> None:
        self._write(TradeRecord(token=token, strategy=strategy, entry=entry,
                                exit=exit_, size=size, profit_loss=profit_loss))

    def record_order(self, action: str, status: str, reason: str = "") -> None:
        self._write(OrderRecord(action=action, status=status, reason=reason))

    def record_risk_event(self, event: str, details: str = "") -> None:
        self._write(RiskEventRecord(event=event, details=details))

    # -- read helpers (reports, tests) ---------------------------------- #
    def trades(self) -> list[TradeRecord]:
        with self._session_factory() as session:
            return list(session.scalars(select(TradeRecord).order_by(TradeRecord.id)))

    def orders(self) -> list[OrderRecord]:
        with self._session_factory() as session:
            return list(session.scalars(select(OrderRecord).order_by(OrderRecord.id)))

    def risk_events(self) -> list[RiskEventRecord]:
        with self._session_factory() as session:
            return list(session.scalars(select(RiskEventRecord).order_by(RiskEventRecord.id)))
