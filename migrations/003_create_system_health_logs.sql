CREATE TABLE IF NOT EXISTS system_health_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_time DATETIME NOT NULL,
    overall_status VARCHAR(16) NOT NULL,
    health_score FLOAT NOT NULL,
    broker_status VARCHAR(16) NOT NULL,
    database_status VARCHAR(16) NOT NULL,
    webhook_status VARCHAR(16) NOT NULL,
    trading_status VARCHAR(16) NOT NULL,
    ai_status VARCHAR(16) NOT NULL,
    server_status VARCHAR(16) NOT NULL,
    ltp_latency_ms FLOAT,
    cpu_percent FLOAT,
    ram_percent FLOAT,
    disk_percent FLOAT,
    message TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_system_health_logs_run_time ON system_health_logs (run_time);
