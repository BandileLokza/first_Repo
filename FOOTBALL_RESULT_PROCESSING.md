# Football result processing on Render

Ordinary result writers save the official score, bracket changes, audit, and
the match's pending processing revision in PostgreSQL. A separate dispatcher
publishes due work. The Celery worker commits standings, prediction points, and
the completed revision together. Run the dispatcher independently of Gunicorn
and Celery Beat.

## Rollout

1. Apply the additive migration using the new release and existing Django
   environment. It creates the outbox table without changing historical scores
   or standings.

   ~~~sh
   python manage.py migrate matches 0020_match_result_processing_outbox --noinput
   ~~~

2. Deploy the new code to the **existing Render Background Worker** with this
   Docker Command in the service settings:

   ~~~sh
   python /code/run_background_worker.py celery -A config worker --loglevel=INFO
   ~~~

   If your current Celery command has additional options, keep the full command
   after `python /code/run_background_worker.py`, including its queue, pool,
   and concurrency settings. Keep Celery in the foreground. The launcher
   passes the command through as given and starts
   `dispatch_football_results --interval 3 --batch-size 20` concurrently.

   The existing Dockerfile already copies the launcher into the image. Python
   runs it directly, so it only needs read permission. The Gunicorn command
   remains the default for the web service.

3. Supply the worker and dispatcher with the existing application environment,
   including **DATABASE_URL**, **REDIS_URL**, and **SECRET_KEY**, using the same
   Render environment group as appropriate. Both processes use
   **DJANGO_SETTINGS_MODULE=config.settings** by default. The dispatcher and
   Celery worker must share the production broker and database with the web
   service.

4. Confirm the dispatcher starts and the Celery worker registers
   `matches.tasks.process_football_result_task`. Then deploy the web release
   with the existing Gunicorn command. Ordinary results now depend on both
   background processes being available for eventual processing. Check the old
   worker queue and review known historical drift before prediction resets or
   season archiving. Those operations now reject outstanding outbox work.

The worker processes one match transaction at a time. A score request that
encounters a locked competition or fixture returns a retry message, so it
does not wait behind background prediction scoring.

## Verification and recovery

The launcher sends SIGTERM to both processes on service shutdown and waits for
them to exit. Celery can drain its active tasks within the service's Render
shutdown delay. The dispatcher unwinds its current work. An interrupted
publication can be retried using the existing outbox lease.

If either process exits unexpectedly, including with status zero, the launcher
logs the exit, asks its sibling to stop, and exits with a failure status so the
service can restart. In this failure case it waits up to 25 seconds for the
sibling before forcing it to stop. Check the shared Render logs for both
startup messages and use Render metrics to watch CPU and memory during peak
Celery activity. The dispatcher is one additional Django process on the same
instance.

Run the health command from the deployed application environment:

~~~sh
python manage.py dispatch_football_results --status
~~~

It reports pending and due jobs, oldest pending age, repeated failures, and
the latest completion. Check the dispatcher and worker logs for repeated
errors. In staging, save a result while Redis is unavailable. Confirm the score
and pending job commit and the standings retain their applied score. Restore
Redis, confirm the job completes, and verify the table and prediction points.
Repeat with a stopped worker and a successful publish before restarting it.

Three seconds between passes is an initial configuration, not a latency
guarantee. An idle dispatcher checks the indexed due-work queue once per pass.
A pass claims at most 20 rows by default and waits for the configured interval
before the next pass. Failed attempts back off up to five minutes. A successful
publish with no completion is eligible for redispatch after at least 60
seconds. An abandoned dispatch claim expires after 120 seconds. These settings
trade recovery speed for database polling and duplicate broker traffic.

### Historical work

To review stale applied scores for one competition before queuing work:

~~~sh
python manage.py reconcile_football_results --competition your-league-slug
python manage.py reconcile_football_results --competition your-league-slug --enqueue
~~~

Both commands are dry runs without `--apply`. Add `--enqueue --apply` to the
competition command to record eligible outstanding work for the dispatcher.
Use `--match-id` with a specific finished match ID to target known prediction
drift that the table snapshot comparison cannot detect. The existing direct
repair and `--rebuild` modes remain available for operator-approved
reconciliation. Rebuild is appropriate when the previous applied contribution
cannot safely be reversed.

### Rollback

Keep the additive schema while rolling back application code. Stop or roll
back the new web writers first and drain their outstanding revisions with the
new dispatcher and worker still running. Verify the backlog and points before
stopping those processes. If work cannot drain, retain the outbox for recovery
and keep its contents through the rollback. Removing the table would discard
outstanding work and completion history.
