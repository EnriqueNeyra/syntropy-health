-- Household accounts. A profile is a person whose health data is kept here; an account is someone who signs in. Each
-- account is one of the people, with a password of its own; it sees its own data and the people it's given access to
-- ('view' or 'manage'). A person without an account (a child, a parent someone cares for) is looked after by whoever
-- manages them. The owner, who set the server up, also changes the settings that affect everyone.

CREATE TABLE IF NOT EXISTS accounts (
    id               TEXT PRIMARY KEY,
    profile_id       TEXT NOT NULL UNIQUE REFERENCES profiles(id) ON DELETE CASCADE,
    password_hash    TEXT,                   -- encrypted like the other secrets; NULL only while developing without one
    role             TEXT NOT NULL DEFAULT 'member',     -- 'owner' or 'member'
    terms_version    INTEGER,                -- the agreement this person accepted
    onboarding       INTEGER NOT NULL DEFAULT 0,          -- first-run steps still to show
    prefs            TEXT,                   -- JSON: units, accent
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    last_login_at    REAL
);

CREATE TABLE IF NOT EXISTS profile_access (
    account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    profile_id  TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    level       TEXT NOT NULL,              -- 'view' or 'manage'
    created_at  REAL NOT NULL,
    PRIMARY KEY (account_id, profile_id)
);
CREATE INDEX IF NOT EXISTS idx_profile_access_profile ON profile_access(profile_id);

-- One-time codes that let a person choose their own password and sign in as themselves.
CREATE TABLE IF NOT EXISTS invites (
    code_hash   TEXT PRIMARY KEY,
    profile_id  TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    created_by  TEXT,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);

ALTER TABLE user_sessions ADD COLUMN account_id TEXT;
ALTER TABLE devices ADD COLUMN account_id TEXT;
ALTER TABLE agent_tokens ADD COLUMN account_id TEXT;
ALTER TABLE pairing_codes ADD COLUMN account_id TEXT;
CREATE INDEX IF NOT EXISTS idx_user_sessions_account ON user_sessions(account_id);

-- An instance set up before accounts: its password becomes the owner's, who is the first person and keeps managing
-- everyone else until they're invited to sign in as themselves.
INSERT INTO accounts(id, profile_id, password_hash, role, terms_version, onboarding, prefs, created_at, updated_at)
SELECT 'acc_owner', p.id,
       (SELECT value FROM app_settings WHERE key = 'auth.password_hash'),
       'owner',
       (SELECT json_extract(value, '$.version') FROM app_settings WHERE key = 'terms.accepted'),
       CASE WHEN EXISTS (SELECT 1 FROM app_settings WHERE key = 'onboarding.pending' AND value = 'true') THEN 1 ELSE 0 END,
       json_object('units', (SELECT json_extract(value, '$') FROM app_settings WHERE key = 'display.units'),
                   'accent', (SELECT json_extract(value, '$') FROM app_settings WHERE key = 'display.accent')),
       CAST(strftime('%s', 'now') AS REAL), CAST(strftime('%s', 'now') AS REAL)
FROM profiles p
WHERE EXISTS (SELECT 1 FROM app_settings WHERE key = 'setup.completed' AND value = 'true')
ORDER BY p.is_default DESC, p.created_at
LIMIT 1;

INSERT INTO profile_access(account_id, profile_id, level, created_at)
SELECT 'acc_owner', p.id, 'manage', CAST(strftime('%s', 'now') AS REAL)
FROM profiles p
WHERE EXISTS (SELECT 1 FROM accounts WHERE id = 'acc_owner')
  AND p.id != (SELECT profile_id FROM accounts WHERE id = 'acc_owner');

UPDATE user_sessions SET account_id = 'acc_owner' WHERE EXISTS (SELECT 1 FROM accounts WHERE id = 'acc_owner');
UPDATE devices SET account_id = 'acc_owner' WHERE EXISTS (SELECT 1 FROM accounts WHERE id = 'acc_owner');
UPDATE agent_tokens SET account_id = 'acc_owner' WHERE EXISTS (SELECT 1 FROM accounts WHERE id = 'acc_owner');
-- The owner's agreement that an online AI may see their records is theirs alone now (app.services.ai).
UPDATE app_settings SET key = 'ai.cloud_ack.acc_owner'
WHERE key = 'ai.cloud_ack' AND EXISTS (SELECT 1 FROM accounts WHERE id = 'acc_owner');
-- How the owner arranged Overview and which alerts they'd seen: now kept per account (app.api.profiles).
UPDATE app_settings SET key = key || '.acc_owner'
WHERE (key LIKE 'overview.layout.%' OR key LIKE 'alerts.seen.%' OR key LIKE 'alerts.dismissed.%')
  AND EXISTS (SELECT 1 FROM accounts WHERE id = 'acc_owner');
DELETE FROM app_settings WHERE key IN ('auth.password_hash', 'terms.accepted', 'onboarding.pending')
  AND EXISTS (SELECT 1 FROM accounts WHERE id = 'acc_owner');
