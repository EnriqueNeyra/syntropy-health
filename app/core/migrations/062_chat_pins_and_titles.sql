-- A conversation someone pinned stays at the top of their list and is never the one the limit pushes out. title_set says the
-- person chose its title, so saving another answer doesn't put back the one made from the first question.
ALTER TABLE chats ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chats ADD COLUMN title_set INTEGER NOT NULL DEFAULT 0;
