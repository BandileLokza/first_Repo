"""Run the Celery worker and football results dispatcher in one Linux container."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


DISPATCHER_ARGS = ("dispatch_football_results", "--interval", "3", "--batch-size", "20")
FAILURE_SHUTDOWN_SECONDS = 25


def run_dispatcher():
    def stop_dispatcher(signum, frame):
        # Unwind the dispatcher's cleanup and any open database transaction.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_dispatcher)
    from manage import main

    sys.argv = ["manage.py", *DISPATCHER_ARGS]
    main()


def stop_children(children, *, failed):
    for process in children.values():
        if process.poll() is None:
            # Signal the Celery main process so it can drain its own pool.
            process.terminate()

    # Render bounds normal shutdown. After a child fails, bound the wait
    # here so a stuck sibling cannot keep the failed container alive.
    deadline = time.monotonic() + FAILURE_SHUTDOWN_SECONDS if failed else None
    for name, process in children.items():
        timeout = None if deadline is None else max(0, deadline - time.monotonic())
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"{name} did not stop in time. Killing its process group.", file=sys.stderr, flush=True)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def supervise(celery_command):
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    commands = {
        "Celery": celery_command,
        "Football result dispatcher": [sys.executable, str(Path(__file__).resolve()), "--dispatcher"],
    }
    children = {}
    failed = False
    try:
        for name, command in commands.items():
            if stop_requested:
                break
            children[name] = subprocess.Popen(command, start_new_session=True)
            print(f"Started {name} (PID {children[name].pid}).", flush=True)

        while not stop_requested and not failed:
            for name, process in children.items():
                status = process.poll()
                if status is not None:
                    print(f"{name} exited unexpectedly (status {status}). Stopping worker service.",
                          file=sys.stderr, flush=True)
                    failed = True
                    break
            if not failed:
                time.sleep(0.2)
    except OSError as error:
        print(f"Worker service error: {error}", file=sys.stderr, flush=True)
        failed = True
    finally:
        stop_children(children, failed=failed)
    return int(failed)


if __name__ == "__main__":
    if sys.argv[1:] == ["--dispatcher"]:
        run_dispatcher()
    elif len(sys.argv) > 1:
        sys.exit(supervise(sys.argv[1:]))
    else:
        sys.exit("Usage: python run_background_worker.py <existing Celery worker command>")
