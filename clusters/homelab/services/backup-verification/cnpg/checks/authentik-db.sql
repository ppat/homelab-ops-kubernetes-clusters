-- Content checks for a restored authentik database (authentik 2026.8). Any failed
-- expectation raises, which fails the verification run.
--
-- Freshness reads the task queue, not the event log. authentik's worker records every
-- scheduled task in authentik_tasks_task, and several schedules run every 5 minutes, so the
-- newest mtime trails the backup by minutes. Events are written only when someone logs in or
-- changes something, and this instance has had whole days without one.
DO $$
DECLARE
  users bigint;
  newest timestamptz;
BEGIN
  SELECT count(*) INTO users FROM authentik_core_user;
  IF users = 0 THEN
    RAISE EXCEPTION 'authentik_core_user is empty';
  END IF;

  SELECT max(mtime) INTO newest FROM authentik_tasks_task;
  IF newest IS NULL OR newest < now() - interval '24 hours' THEN
    RAISE EXCEPTION 'newest authentik_tasks_task.mtime is %, older than 24 hours', newest;
  END IF;

  RAISE NOTICE 'authentik_core_user rows: %, newest task mtime: %', users, newest;
END
$$;
