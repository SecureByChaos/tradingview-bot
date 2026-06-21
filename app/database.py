from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from sqlalchemy import inspect, text

from app.config import get_settings

settings = get_settings()
if settings.database_url.startswith("sqlite:///"):
    sqlite_path = settings.database_url.replace("sqlite:///", "", 1)
    if sqlite_path and sqlite_path != ":memory:":
        from pathlib import Path

        Path(sqlite_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
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

    from app.db_models import StrategyConfig, TradingMode

    with SessionLocal() as db:
        strategy = db.scalar(select(StrategyConfig).where(StrategyConfig.name == settings.default_strategy_name))
        if strategy is not None:
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
        db.commit()
