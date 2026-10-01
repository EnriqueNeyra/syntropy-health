-- Workouts and health events (symptoms, cycle tracking, notifications, mindful sessions,
-- ECG summaries, State of Mind) sent by the iPhone app or imported from HealthKit export JSON.

CREATE TABLE IF NOT EXISTS workouts (
    id                   TEXT PRIMARY KEY,
    profile_id           TEXT NOT NULL,
    connection_id        TEXT,
    activity_type        INTEGER,
    name                 TEXT NOT NULL,
    start_date           TEXT NOT NULL,          -- ISO-8601 UTC
    end_date             TEXT NOT NULL,
    duration_s           REAL,
    active_energy_kcal   REAL,
    total_energy_kcal    REAL,
    distance_m           REAL,
    step_count           REAL,
    avg_hr               REAL,
    max_hr               REAL,
    min_hr               REAL,
    elevation_ascent_m   REAL,
    elevation_descent_m  REAL,
    indoor               INTEGER,
    temperature_c        REAL,
    humidity_pct         REAL,
    source_name          TEXT,
    device_name          TEXT,
    metadata_json        TEXT,
    heart_rate_json      TEXT,                   -- [{t, min, avg, max}] per minute
    route_json           TEXT,                   -- [{t, lat, lon, alt, speed}]
    created_at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workouts_profile_start ON workouts(profile_id, start_date DESC);

CREATE TABLE IF NOT EXISTS health_events (
    id              TEXT PRIMARY KEY,
    profile_id      TEXT NOT NULL,
    connection_id   TEXT,
    event_type      TEXT NOT NULL,              -- e.g. headache, menstrual_flow, ecg, state_of_mind
    category        TEXT NOT NULL,              -- symptoms, cycle, events, mindfulness, ecg, stateOfMind, ...
    name            TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    value           REAL,
    value_label     TEXT,
    source_name     TEXT,
    metadata_json   TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_profile_start ON health_events(profile_id, start_date DESC);
CREATE INDEX IF NOT EXISTS idx_events_profile_type ON health_events(profile_id, event_type, start_date DESC);
