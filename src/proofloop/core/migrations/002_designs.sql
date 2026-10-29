-- CAD design iterations for physical products

CREATE TABLE designs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    meta_json TEXT
);

CREATE TABLE design_versions (
    id TEXT PRIMARY KEY,
    design_id TEXT NOT NULL REFERENCES designs(id),
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    file TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    printer TEXT NOT NULL,
    material TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    reactions_json TEXT,
    comparison_json TEXT,
    report_path TEXT,
    UNIQUE (design_id, version)
);

CREATE TABLE print_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL REFERENCES design_versions(id),
    created_at TEXT NOT NULL,
    measurements_json TEXT NOT NULL,
    defects_json TEXT NOT NULL,
    notes TEXT,
    analysis_json TEXT NOT NULL
);

CREATE TABLE printer_calibration (
    key TEXT PRIMARY KEY,           -- printer|material
    updated_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
