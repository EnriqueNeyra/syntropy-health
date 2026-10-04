-- Workout humidity from the iPhone app arrived as hundredths of a percent (Apple's Workout app stores 65% as
-- "6500 %"); keep it as 0–100 like every other source.
UPDATE workouts SET humidity_pct = humidity_pct / 100 WHERE humidity_pct > 100;
