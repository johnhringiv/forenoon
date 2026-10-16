-- A follow is chosen (the reader clicked Follow, added its address or pinned it) or made for
-- them (a new account's, or a feed being shared with the household). Only a chosen one keeps
-- a feed from being removed by another reader. Follows from before cannot be told apart, so
-- they count as chosen.
ALTER TABLE follows ADD COLUMN chosen INTEGER NOT NULL DEFAULT 1;
-- Whether a feed is followed for this reader when it is shared with the household.
ALTER TABLE users ADD COLUMN follow_new INTEGER NOT NULL DEFAULT 1;
-- When a source was last shared: one shared lately that a reader does not follow is marked new.
ALTER TABLE sources ADD COLUMN shared_at TEXT;
