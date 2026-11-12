-- Latest IP and location are private; public summaries never expose identifiers or IPs.
CREATE TABLE IF NOT EXISTS installations (
    id_hash TEXT PRIMARY KEY CHECK(length(id_hash) = 64),
    product TEXT NOT NULL DEFAULT 'decent_remote' CHECK(product IN ('decent_remote','this_cup')),
    country TEXT NOT NULL CHECK(length(country) = 2),
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    version TEXT CHECK(version IS NULL OR (length(version)>0 AND length(version)<=64)),
    region TEXT,
    city TEXT,
    last_ip TEXT,
    ip_updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS installations_last_seen ON installations(last_seen);
CREATE INDEX IF NOT EXISTS installations_product_last_seen ON installations(product,last_seen);

-- Research events are append-only and deliberately have no installation FK:
-- removing an installation must never remove its historical cup records.
CREATE TABLE IF NOT EXISTS cup_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE CHECK(length(event_id) = 64),
    installation_hash TEXT NOT NULL CHECK(length(installation_hash) = 64),
    machine_id TEXT CHECK(machine_id IS NULL OR length(machine_id) = 64),
    shot_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('shot_completed','assessment_saved')),
    occurred_at TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    payload_gzip BLOB NOT NULL,
    source_ip TEXT,
    country TEXT NOT NULL,
    region TEXT,
    city TEXT
);
CREATE INDEX IF NOT EXISTS cup_events_installation_shot ON cup_events(installation_hash,shot_id,sequence);
CREATE INDEX IF NOT EXISTS cup_events_machine_received ON cup_events(machine_id,received_at);
CREATE TRIGGER IF NOT EXISTS cup_events_no_update BEFORE UPDATE ON cup_events
BEGIN SELECT RAISE(ABORT,'cup events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS cup_events_no_delete BEFORE DELETE ON cup_events
BEGIN SELECT RAISE(ABORT,'cup events are append-only'); END;
