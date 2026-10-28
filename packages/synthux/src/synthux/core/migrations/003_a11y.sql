-- Measured accessibility checks (contrast, text size, tap targets) from the run's screenshots.
CREATE TABLE a11y_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    check_name TEXT NOT NULL,        -- contrast | small_text | tap_target
    screen_id TEXT NOT NULL,
    text TEXT NOT NULL,
    measured REAL NOT NULL,
    required REAL NOT NULL,
    severity INTEGER NOT NULL,
    region_json TEXT,
    screenshot TEXT,
    devices_json TEXT NOT NULL,
    UNIQUE (run_id, check_name, screen_id, text)
);
CREATE INDEX idx_a11y_run ON a11y_findings(run_id);
