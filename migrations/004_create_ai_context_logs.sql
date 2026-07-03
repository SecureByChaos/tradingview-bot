CREATE TABLE IF NOT EXISTS ai_context_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    strategy VARCHAR(128) NOT NULL,
    signal VARCHAR(16) NOT NULL,
    event_type VARCHAR(16) NOT NULL,
    paper_live VARCHAR(16) NOT NULL DEFAULT '',
    trade_id VARCHAR(64),
    trade_number INTEGER NOT NULL DEFAULT 0,
    session VARCHAR(32) NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    request_json TEXT NOT NULL DEFAULT '{}',
    payload_size INTEGER NOT NULL DEFAULT 0,
    context_version VARCHAR(16) NOT NULL DEFAULT '',
    prompt_version VARCHAR(16) NOT NULL DEFAULT '',
    model VARCHAR(128) NOT NULL DEFAULT '',
    completeness_percent FLOAT NOT NULL DEFAULT 0,
    missing_fields TEXT NOT NULL DEFAULT '[]',
    latency_ms FLOAT,
    decision VARCHAR(16) NOT NULL DEFAULT '',
    confidence FLOAT,
    reason_to_buy TEXT NOT NULL DEFAULT '[]',
    reason_not_to_buy TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_ai_context_logs_trade_id ON ai_context_logs (trade_id);
CREATE INDEX IF NOT EXISTS ix_ai_context_logs_strategy ON ai_context_logs (strategy);
