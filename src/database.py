import os
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

os.makedirs("/app/data", exist_ok=True)

engine = create_engine("sqlite:////app/data/trading.db", echo=False)


class Base(DeclarativeBase):
    pass


class Trade(Base):
    """Represents an open or closed position."""

    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    coin = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)  # 'long' | 'short'
    size_usd = Column(Float, nullable=False)
    qty = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    entry_time = Column(DateTime, nullable=False)
    entry_order_id = Column(Integer, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_time = Column(DateTime, nullable=True)
    exit_order_id = Column(Integer, nullable=True)
    pnl_usd = Column(Float, nullable=True)
    status = Column(String(20), default="open")  # 'open' | 'closed' | 'error'
    created_at = Column(DateTime, default=datetime.utcnow)


class Cycle(Base):
    """Logs each AI trading cycle."""

    __tablename__ = "cycles"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    coin = Column(String(20), nullable=False)
    chart_path = Column(String(500), nullable=True)
    ai_decision = Column(String(10), nullable=True)   # 'LONG' | 'SHORT' | 'HOLD'
    ai_reasoning = Column(Text, nullable=True)
    action_taken = Column(String(20), nullable=True)  # 'long' | 'short' | 'hold' | 'skipped' | 'error'
    skip_reason = Column(String(200), nullable=True)
    claude_raw_output = Column(Text, nullable=True)


Base.metadata.create_all(engine)


@contextmanager
def get_session():
    session = Session(engine)
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
