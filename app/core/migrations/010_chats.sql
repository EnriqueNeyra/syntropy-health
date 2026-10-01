-- Ask conversations, kept here rather than in one browser so they follow the person between the computer, the desktop
-- apps and the iPhone app. Each account keeps its own conversations about each person it can see.
CREATE TABLE IF NOT EXISTS chats (
    id             TEXT PRIMARY KEY,
    profile_id     TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    account_id     TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL,
    messages_json  TEXT NOT NULL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chats_owner ON chats(profile_id, account_id, updated_at DESC);
