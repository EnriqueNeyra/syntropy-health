-- Web sessions opened by the iPhone app for its Syntropy tab. Each belongs to the paired device that opened it, so
-- revoking the device (or pairing again) signs that web view out too.

ALTER TABLE user_sessions ADD COLUMN device_id TEXT;
CREATE INDEX IF NOT EXISTS idx_user_sessions_device ON user_sessions(device_id);
