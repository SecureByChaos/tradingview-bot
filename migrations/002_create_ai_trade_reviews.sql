CREATE TABLE IF NOT EXISTS ai_trade_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id VARCHAR(64),
    strategy VARCHAR(128) NOT NULL,
    signal VARCHAR(16) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    model VARCHAR(128) NOT NULL DEFAULT '',
    prompt_version VARCHAR(16) NOT NULL,
    context_version VARCHAR(16) NOT NULL,
    framework_version VARCHAR(16) NOT NULL,
    decision VARCHAR(16) NOT NULL,
    confidence FLOAT NOT NULL DEFAULT 0,
    entry_quality VARCHAR(64) NOT NULL DEFAULT '',
    market_type VARCHAR(64) NOT NULL DEFAULT '',
    risk VARCHAR(64) NOT NULL DEFAULT '',
    reason_to_buy TEXT NOT NULL DEFAULT '[]',
    reason_not_to_buy TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    latency_ms FLOAT NOT NULL DEFAULT 0,
    actual_result VARCHAR(16),
    actual_pnl FLOAT,
    ai_correct BOOLEAN,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_ai_trade_reviews_trade_id ON ai_trade_reviews (trade_id);
CREATE INDEX IF NOT EXISTS ix_ai_trade_reviews_strategy ON ai_trade_reviews (strategy);
