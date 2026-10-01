-- ============================================================================
-- Syntropy Health — schema v1
-- SQLite (WAL). All health data is scoped to a profile (Self, Child, Parent...).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Instance settings & local authentication
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,              -- JSON encoded
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash    TEXT PRIMARY KEY,         -- sha256 of the cookie value
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    user_agent    TEXT,
    ip            TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    actor       TEXT NOT NULL,              -- 'user', 'device:<id>', 'scheduler', 'system'
    action      TEXT NOT NULL,              -- 'login', 'connection.created', 'sync.completed', ...
    profile_id  TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at DESC);

-- ---------------------------------------------------------------------------
-- Profiles (the people whose records are managed on this instance)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS profiles (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    relationship  TEXT NOT NULL DEFAULT 'self',   -- self, partner, child, parent, other
    birth_date    TEXT,
    sex           TEXT,
    color         TEXT,
    is_default    INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

-- ---------------------------------------------------------------------------
-- Connections: one row per linked data source (EHR portal, wearable, import)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS connections (
    id                TEXT PRIMARY KEY,
    profile_id        TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL,        -- 'ehr', 'wearable', 'device', 'import', 'manual'
    provider          TEXT NOT NULL,        -- 'epic', 'cerner', 'va', 'oura', 'whoop', 'apple_health', ...
    institution_id    TEXT,                 -- directory id for EHR connections
    display_name      TEXT NOT NULL,
    mode              TEXT NOT NULL DEFAULT 'live',   -- 'simulated', 'sandbox', 'production', 'live'
    fhir_base_url     TEXT,
    patient_ref       TEXT,                 -- FHIR Patient id (EHR) / remote user id (wearable)
    status            TEXT NOT NULL DEFAULT 'active', -- 'active', 'needs_reauth', 'error', 'disconnected'
    scopes            TEXT,
    credentials_enc   TEXT,                 -- encrypted JSON: access/refresh tokens, expiry, client/token endpoint
    last_sync_at      REAL,
    last_sync_status  TEXT,
    last_error        TEXT,
    metadata_json     TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_connections_profile ON connections(profile_id);

CREATE TABLE IF NOT EXISTS oauth_pending (
    state         TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,            -- 'smart', 'oura', 'whoop'
    payload_enc   TEXT NOT NULL,            -- encrypted JSON (code_verifier, endpoints, draft connection)
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    trigger       TEXT NOT NULL,            -- 'initial', 'manual', 'scheduled', 'webhook', 'ingest', 'import'
    started_at    REAL NOT NULL,
    finished_at   REAL,
    status        TEXT NOT NULL DEFAULT 'running', -- 'running', 'success', 'partial', 'error'
    stats_json    TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_runs_conn ON sync_runs(connection_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- Clinical data (normalized FHIR R4 / USCDI)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS patient_identities (
    connection_id  TEXT PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    profile_id     TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    full_name      TEXT,
    birth_date     TEXT,
    gender         TEXT,
    mrn            TEXT,
    telecom_json   TEXT,
    address_json   TEXT,
    raw_json       TEXT,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS clinical_records (
    id              TEXT PRIMARY KEY,       -- stable hash(connection, resource type, resource id)
    profile_id      TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    connection_id   TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource_type   TEXT NOT NULL,          -- FHIR resourceType
    resource_id     TEXT NOT NULL,
    category        TEXT NOT NULL,          -- vitals, labs, medications, conditions, allergies, immunizations,
                                            -- encounters, procedures, notes, reports, care_team, care_plans,
                                            -- goals, devices, coverage
    title           TEXT NOT NULL,
    code_system     TEXT,
    code            TEXT,
    code_display    TEXT,
    effective_at    TEXT,                   -- ISO-8601 (date or datetime)
    effective_end   TEXT,
    status          TEXT,
    value_num       REAL,
    value_text      TEXT,
    unit            TEXT,
    value_norm      REAL,                   -- value converted to the canonical unit for this code
    unit_norm       TEXT,
    ref_low         REAL,
    ref_high        REAL,
    ref_text        TEXT,
    interpretation  TEXT,                   -- normal, high, low, critical_high, critical_low, abnormal
    details_json    TEXT,                   -- category specific normalized fields
    narrative       TEXT,
    dedup_key       TEXT,                   -- equal across sources for the same real-world fact
    source_name     TEXT,
    raw_json        TEXT,                   -- original FHIR resource
    first_seen_at   REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_profile_cat ON clinical_records(profile_id, category, effective_at DESC);
CREATE INDEX IF NOT EXISTS idx_records_profile_code ON clinical_records(profile_id, code);
CREATE INDEX IF NOT EXISTS idx_records_dedup ON clinical_records(profile_id, dedup_key);
CREATE INDEX IF NOT EXISTS idx_records_connection ON clinical_records(connection_id);

-- ---------------------------------------------------------------------------
-- Continuous biometrics (wearables, phone sensors)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS biometric_samples (
    id              TEXT PRIMARY KEY,
    profile_id      TEXT,
    connection_id   TEXT,
    metric_type     TEXT NOT NULL,
    hk_identifier   TEXT,
    value           REAL NOT NULL,
    unit            TEXT NOT NULL,
    start_date      TEXT NOT NULL,          -- ISO-8601 UTC
    end_date        TEXT NOT NULL,
    device_id       TEXT,
    device_name     TEXT,
    source_name     TEXT,
    metadata_json   TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_profile_metric ON biometric_samples(profile_id, metric_type, start_date DESC);
CREATE INDEX IF NOT EXISTS idx_samples_start ON biometric_samples(start_date DESC);

-- Per-day, per-source aggregates. Rebuilt for affected days after every ingest.
CREATE TABLE IF NOT EXISTS biometric_daily (
    profile_id    TEXT NOT NULL,
    day           TEXT NOT NULL,            -- local calendar date YYYY-MM-DD
    metric_type   TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    value         REAL NOT NULL,            -- metric-specific aggregate (sum / mean / last)
    min_value     REAL,
    max_value     REAL,
    sample_count  INTEGER NOT NULL,
    unit          TEXT,
    PRIMARY KEY (profile_id, day, metric_type, source_name)
);
CREATE INDEX IF NOT EXISTS idx_daily_metric ON biometric_daily(profile_id, metric_type, day DESC);

CREATE TABLE IF NOT EXISTS sync_batches (
    batch_id      TEXT PRIMARY KEY,
    profile_id    TEXT,
    device_id     TEXT NOT NULL,
    device_name   TEXT NOT NULL,
    os_version    TEXT,
    app_version   TEXT,
    sync_trigger  TEXT NOT NULL,
    sample_count  INTEGER NOT NULL,
    inserted      INTEGER NOT NULL DEFAULT 0,
    synced_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_synced_at ON sync_batches(synced_at DESC);

-- ---------------------------------------------------------------------------
-- Companion devices (iOS app) & pairing
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,
    profile_id    TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    connection_id TEXT,
    name          TEXT NOT NULL,
    platform      TEXT,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    REAL NOT NULL,
    last_seen_at  REAL,
    revoked_at    REAL
);

CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash   TEXT PRIMARY KEY,
    profile_id  TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
