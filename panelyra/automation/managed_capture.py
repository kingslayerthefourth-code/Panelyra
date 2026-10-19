"""Interactive Capture under the local engine's lifetime, never a scheduler."""
import argparse
import sys
from pathlib import Path
from automation_runtime import reporter_from_environment
from panelyra.services.worker_runtime import interruptible_worker, WorkerInterrupted


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--root', type=Path, required=True)
    args, remaining = parser.parse_known_args()
    sys.argv = ['capture_dispatch', *remaining]
    try:
        with interruptible_worker(args.root, 'capture'):
            import os
            reporter = reporter_from_environment()
            if reporter:
                reporter.update(status='running', pid=os.getpid())
            from automation_runtime import browser_lock_owner
            import time
            while browser_lock_owner():
                if reporter:
                    reporter.update(stage='waiting-for-browser', message='Waiting for the current chapter/browser task to finish; no source attempt was consumed.')
                time.sleep(.5)
            from panelyra.automation.capture_dispatch import main as capture
            return capture()
    except WorkerInterrupted as exc:
        reporter = reporter_from_environment()
        if reporter:
            reporter.update(status='interrupted', stage='interrupted', message=str(exc), stop_reason=str(exc))
        return 130
    except ValueError as exc:
        reporter = reporter_from_environment()
        if reporter:
            reporter.update(status='failed', stage='failed', message=str(exc), stop_reason=str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
