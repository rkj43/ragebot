"""SQLAlchemy ORM models: the audit trail.

Three tables — trades (closed round-trips), orders (every attempt and its
outcome), and risk_events (every risk-layer trigger). Note what is absent:
keys, secrets, or wallet material are never stored.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TradeRecord(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    token: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    entry: Mapped[float] = mapped_column(Float, nullable=False)
    exit: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    profit_loss: Mapped[float] = mapped_column(Float, nullable=False)


class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)   # BUY / SELL
    status: Mapped[str] = mapped_column(String(16), nullable=False)   # SUBMITTED/FILLED/REJECTED/FAILED/IGNORED
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")


class RiskEventRecord(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
