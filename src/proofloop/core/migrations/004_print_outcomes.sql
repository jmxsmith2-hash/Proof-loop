-- How real prints ended (imported from ClearLoop's print journal), for learning per printer + material

CREATE TABLE print_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,            -- clearloop
    source_id TEXT NOT NULL,         -- the record's id there
    ended_at REAL NOT NULL,
    printer_name TEXT NOT NULL,      -- the user's name for the printer
    printer TEXT,                    -- printer profile (bambu_p2s)
    material TEXT,
    job TEXT,
    loop_no INTEGER,
    outcome TEXT NOT NULL,           -- finished, failed, cancelled, unknown
    layer INTEGER,                   -- layer reached (in its loop)
    total_layers INTEGER,
    eject TEXT,                      -- clear, parts_left, or NULL
    cause TEXT,                      -- first_layer, flow, filament_feed, camera, cooling, unknown, or NULL
    evidence_json TEXT,              -- what was printing at the layer it stopped, and the whole file
    record_json TEXT NOT NULL,       -- the journal record as imported
    UNIQUE (source, source_id)
);
CREATE INDEX idx_outcomes_printer ON print_outcomes(printer, material, ended_at);
CREATE INDEX idx_outcomes_job ON print_outcomes(job);
