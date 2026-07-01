CREATE TABLE IF NOT EXISTS ai_settings (
    id INTEGER PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    mode VARCHAR(16) NOT NULL DEFAULT 'SHADOW',
    provider VARCHAR(32) NOT NULL DEFAULT 'dummy',
    model VARCHAR(128) NOT NULL DEFAULT '',
    api_key VARCHAR(512) NOT NULL DEFAULT '',
    base_url VARCHAR(512) NOT NULL DEFAULT '',
    temperature FLOAT NOT NULL DEFAULT 0.2,
    timeout_seconds INTEGER NOT NULL DEFAULT 3,
    confidence_threshold INTEGER NOT NULL DEFAULT 90,
    system_prompt TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO ai_settings (
    id, enabled, mode, provider, model, api_key, base_url, temperature,
    timeout_seconds, confidence_threshold, system_prompt
)
SELECT 1, FALSE, 'SHADOW', 'dummy', '', '', '', 0.2, 3, 90, ''
WHERE NOT EXISTS (SELECT 1 FROM ai_settings);
