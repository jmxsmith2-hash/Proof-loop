-- synthux schema v1

CREATE TABLE personas (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    study TEXT,
    label TEXT NOT NULL,
    traits_json TEXT NOT NULL,
    narrative TEXT NOT NULL,
    source_summary TEXT,
    low_confidence INTEGER NOT NULL DEFAULT 0,
    engine TEXT,
    model TEXT,
    generation INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE persona_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id TEXT NOT NULL REFERENCES personas(id),
    source_file TEXT,
    snippet TEXT NOT NULL            -- anonymized text only
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    study TEXT NOT NULL,
    target_url TEXT NOT NULL,
    status TEXT NOT NULL,            -- running | done | failed
    config_json TEXT NOT NULL,
    calibration_version TEXT NOT NULL,
    cost_estimate_usd REAL,
    cost_actual_usd REAL,
    finished_at TEXT
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    persona_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    repeat_index INTEGER NOT NULL,
    visit_index INTEGER NOT NULL DEFAULT 0,  -- multi-session: 0 = first visit
    seed INTEGER NOT NULL,
    conditions_json TEXT NOT NULL,
    outcome TEXT,                    -- completed | quit | budget_exhausted | agent_failure | blocked
    quit_reason TEXT,
    steps_count INTEGER,
    sim_seconds REAL,
    wall_seconds REAL,
    final_frustration REAL,
    memory_json TEXT,
    started_at TEXT,
    ended_at TEXT
);
CREATE INDEX idx_sessions_run ON sessions(run_id);

CREATE TABLE steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    idx INTEGER NOT NULL,
    screenshot_path TEXT,
    screen_hash TEXT,
    screen_id TEXT,
    url TEXT,
    think_json TEXT NOT NULL,        -- sees / expects / does / feels
    action_json TEXT,                -- action actually executed (with noise)
    intended_json TEXT,              -- action the persona meant (used for replay)
    signals_json TEXT NOT NULL,
    frustration REAL NOT NULL,
    sim_seconds REAL NOT NULL
);
CREATE INDEX idx_steps_session ON steps(session_id);

CREATE TABLE issues (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    step_idx INTEGER,
    screen_id TEXT,
    region_json TEXT,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    severity INTEGER NOT NULL,       -- 1 cosmetic .. 4 blocker
    source TEXT NOT NULL,            -- behavior | elicited
    failure_class TEXT NOT NULL,     -- UX_ISSUE | AGENT_LIMITATION | BLOCKED_BY_SAFETY
    status TEXT NOT NULL,            -- pending | accepted | rejected
    replay_status TEXT,              -- CONFIRMED | NOT_REPRODUCED | REPLAY_FAILED | SKIPPED
    novelty INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL,
    judge_json TEXT,
    cluster_id TEXT
);
CREATE INDEX idx_issues_run ON issues(run_id);

CREATE TABLE issue_clusters (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    screen_id TEXT,
    severity REAL NOT NULL,
    runs_seen INTEGER NOT NULL,
    runs_total INTEGER NOT NULL,
    personas_json TEXT NOT NULL,
    score REAL NOT NULL,
    confidence TEXT NOT NULL,        -- high | medium | low
    promoted INTEGER NOT NULL,
    novelty INTEGER NOT NULL DEFAULT 0,
    low_confidence_personas INTEGER NOT NULL DEFAULT 0,
    replay_status TEXT,
    member_ids_json TEXT NOT NULL,
    detail_json TEXT
);

CREATE TABLE judge_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    verdict TEXT NOT NULL,           -- accept | reject | revise
    reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE replays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    status TEXT NOT NULL,
    hash_distance INTEGER,
    screenshot_path TEXT,
    detail_json TEXT
);

CREATE TABLE questionnaires (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    answers_json TEXT NOT NULL
);

CREATE TABLE llm_cache (
    key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    role TEXT NOT NULL,
    engine TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    cached INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE real_user_studies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    source_file TEXT,
    meta_json TEXT
);

CREATE TABLE real_user_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id TEXT NOT NULL REFERENCES real_user_studies(id),
    task_id TEXT NOT NULL,
    participants INTEGER NOT NULL,
    completed INTEGER NOT NULL,
    median_seconds REAL,
    quits_json TEXT NOT NULL         -- [{step/screen, reason, count}]
);

CREATE TABLE real_user_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id TEXT NOT NULL REFERENCES real_user_studies(id),
    task_id TEXT,
    screen TEXT,
    category TEXT,
    title TEXT NOT NULL,
    description TEXT,
    severity INTEGER,
    participants_affected INTEGER
);

CREATE TABLE calibration_profiles (
    version TEXT PRIMARY KEY,
    parent TEXT,
    created_at TEXT NOT NULL,
    params_json TEXT NOT NULL,
    note TEXT
);

CREATE TABLE calibration_runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    real_study_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    matches_json TEXT NOT NULL,
    suggestions_json TEXT NOT NULL,
    applied_version TEXT
);

CREATE TABLE physical_products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    inputs_json TEXT NOT NULL
);

CREATE TABLE review_complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    category TEXT NOT NULL,
    mentions INTEGER NOT NULL,
    share REAL NOT NULL,
    quotes_json TEXT NOT NULL
);

CREATE TABLE physical_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    persona_id TEXT,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE sensor_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
