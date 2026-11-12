-- Content checks for a restored Home Assistant recorder database (recorder schema 53, Home
-- Assistant 2026.11). Any failed expectation raises, which fails the verification run.
--
-- The recorder writes state changes continuously, so the newest states row trails the
-- backup by minutes. last_updated_ts is seconds since the epoch.
DO $$
DECLARE
  newest double precision;
BEGIN
  SELECT max(last_updated_ts) INTO newest FROM states;
  IF newest IS NULL OR newest < extract(epoch FROM now() - interval '24 hours') THEN
    RAISE EXCEPTION 'newest states.last_updated_ts is %, older than 24 hours', to_timestamp(newest);
  END IF;

  RAISE NOTICE 'newest states.last_updated_ts: %', to_timestamp(newest);
END
$$;
