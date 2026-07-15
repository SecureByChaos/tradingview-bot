from __future__ import annotations

import os

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()


def _resolve_database_url(raw_url: str) -> str:
    if not raw_url.startswith("sqlite:///"):
        return raw_url
    sqlite_path = raw_url.replace("sqlite:///", "", 1)
    if not sqlite_path or sqlite_path == ":memory:":
        return raw_url
    path = Path(sqlite_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.getenv("DATABASE_URL"):
        return raw_url
    if path.name != "platform.sqlite3":
        return raw_url
    fallback = path.parent / "banknifty_bot.db"
    if not fallback.exists():
        return raw_url
    try:
        probe_engine = create_engine(
            raw_url,
            connect_args={"check_same_thread": False},
            future=True,
        )
        with probe_engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(1) FROM ai_context_logs")).scalar_one()
        probe_engine.dispose()
        if int(count) > 0:
            return raw_url
    except Exception:
        pass
    return f"sqlite:///{fallback.as_posix()}"


resolved_database_url = _resolve_database_url(settings.database_url)
if resolved_database_url.startswith("sqlite:///"):
    sqlite_path = resolved_database_url.replace("sqlite:///", "", 1)
    if sqlite_path and sqlite_path != ":memory:":
        Path(sqlite_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    resolved_database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import db_models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _seed_default_strategy()
    _seed_default_ai_settings()


def _ensure_columns() -> None:
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    if "strategy_configs" not in table_names:
        return
    existing = {column["name"] for column in inspector.get_columns("strategy_configs")}
    statements = {
        "paper_trade": "ALTER TABLE strategy_configs ADD COLUMN paper_trade BOOLEAN NOT NULL DEFAULT 1",
        "live_trade": "ALTER TABLE strategy_configs ADD COLUMN live_trade BOOLEAN NOT NULL DEFAULT 0",
        "capital_per_trade": "ALTER TABLE strategy_configs ADD COLUMN capital_per_trade FLOAT NOT NULL DEFAULT 20000.0",
    }
    with engine.begin() as connection:
        for column, statement in statements.items():
            if column not in existing:
                connection.execute(text(statement))
    if "strategy_trades" in table_names:
        existing_trade_columns = {column["name"] for column in inspector.get_columns("strategy_trades")}
        trade_statements = {
            "highest_price": "ALTER TABLE strategy_trades ADD COLUMN highest_price FLOAT",
            "lowest_price": "ALTER TABLE strategy_trades ADD COLUMN lowest_price FLOAT",
            "trailing_active": "ALTER TABLE strategy_trades ADD COLUMN trailing_active BOOLEAN NOT NULL DEFAULT 0",
            "trailing_stop": "ALTER TABLE strategy_trades ADD COLUMN trailing_stop FLOAT",
        }
        with engine.begin() as connection:
            for column, statement in trade_statements.items():
                if column not in existing_trade_columns:
                    connection.execute(text(statement))


def _seed_default_strategy() -> None:
    from sqlalchemy import select

    from app.db_models import StrategyConfig, StrategyStats, TradingMode

    with SessionLocal() as db:
        strategy = db.scalar(select(StrategyConfig).where(StrategyConfig.name == settings.default_strategy_name))
        if strategy is not None:
            stats = db.scalar(select(StrategyStats).where(StrategyStats.strategy_name == strategy.name))
            if stats is None:
                db.add(StrategyStats(strategy_name=strategy.name))
                db.commit()
            return
        strategy = StrategyConfig(
            name=settings.default_strategy_name,
            enabled=True,
            mode=TradingMode.PAPER,
            tp_percent=20.0,
            sl_percent=10.0,
            max_active_trades=1,
            capital_per_trade=20000.0,
            paper_trade=True,
            live_trade=False,
        )
        db.add(strategy)
        db.add(StrategyStats(strategy_name=strategy.name))
        db.commit()


def _seed_default_ai_settings() -> None:
    from sqlalchemy import select

    from app.db_models import AISettings

    with SessionLocal() as db:
        settings_row = db.scalar(select(AISettings).limit(1))
        if settings_row is None:
            db.add(AISettings(id=1))
            db.commit()
