import os
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

os.makedirs("/app/data/reflections", exist_ok=True)

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
    cycle_id = Column(Integer, ForeignKey("cycles.id"), nullable=True)


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
    mid_price = Column(Float, nullable=True)


class MagiVote(Base):
    """Records each MAGI agent's vote per cycle and round."""

    __tablename__ = "magi_votes"

    id         = Column(Integer, primary_key=True)
    cycle_id   = Column(Integer, ForeignKey("cycles.id"), nullable=False, index=True)
    agent_name = Column(String(20), nullable=False)   # 'melchior' | 'balthazar' | 'caspar'
    round      = Column(Integer, default=0)            # 0=初回, 1=再審議1, 2=再審議2, 3=再審議3
    decision   = Column(String(10), nullable=False)    # 'LONG' | 'SHORT' | 'HOLD' | 'EXIT'
    reasoning  = Column(Text, nullable=True)
    raw_output = Column(Text, nullable=True)
    timestamp  = Column(DateTime, default=datetime.utcnow)


class Reflection(Base):
    """Stores full post-trade reflection text, written after each trade closes."""

    __tablename__ = "reflections"

    id = Column(Integer, primary_key=True)
    trade_id = Column(Integer, ForeignKey("trades.id"), unique=True, index=True)
    reflection_text = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class HoldOpportunity(Base):
    """Tracks flat-HOLD decisions for deferred missed-opportunity analysis."""

    __tablename__ = "hold_opportunities"

    id = Column(Integer, primary_key=True)
    cycle_id = Column(Integer, ForeignKey("cycles.id"), nullable=False, index=True)
    coin = Column(String(20), nullable=False)
    hold_price = Column(Float, nullable=False)
    hold_time = Column(DateTime, nullable=False)
    chart_archive_dir = Column(String(500), nullable=True)
    check_time = Column(DateTime, nullable=True)
    max_favorable_price = Column(Float, nullable=True)
    max_favorable_direction = Column(String(10), nullable=True)  # 'long' | 'short'
    hypothetical_pnl = Column(Float, nullable=True)
    status = Column(String(20), default="pending")  # pending | checked | reflected | skipped
    reflection_path = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)

# Migrate existing tables: add cycle_id to trades if missing
with engine.connect() as conn:
    from sqlalchemy import text
    cols = [row[1] for row in conn.execute(text("PRAGMA table_info(trades)"))]
    if "cycle_id" not in cols:
        conn.execute(text("ALTER TABLE trades ADD COLUMN cycle_id INTEGER REFERENCES cycles(id)"))
    cols_cycles = [row[1] for row in conn.execute(text("PRAGMA table_info(cycles)"))]
    if "mid_price" not in cols_cycles:
        conn.execute(text("ALTER TABLE cycles ADD COLUMN mid_price REAL"))


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
