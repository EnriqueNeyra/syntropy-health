-- The Journal: entries people log themselves (notes, symptoms, mood, medication, periods) alongside what the iPhone
-- app sends. `note` is free text on any entry; `manual` marks entries typed in here, the only ones that can be edited.
ALTER TABLE health_events ADD COLUMN note TEXT;
ALTER TABLE health_events ADD COLUMN manual INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_events_profile_manual ON health_events(profile_id, manual, start_date DESC);
