#!/usr/bin/env python3
"""One first static run: fixed 60-second observation/cleanup envelope.

58s runner work (its own limit is 55s), 1s TERM, 0.5s KILL/reap, and
0.5s report reserve. These are budgets, not real-time OS guarantees.
Only stdlib is imported here. The fixed child imports the candidate.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

PYTHON = Path('/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/bin/python3.14')


def identity(path):
    data = path.read_bytes()
    return {'path': str(path), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def save(path, value):
    with path.open('x') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if not args.source_root.is_absolute() or not args.output_dir.is_absolute():
        parser.error('absolute paths required')
    root = args.source_root.resolve(strict=True)
    script = root / 'static-controls/run_static.py'
    if not script.is_file():
        parser.error('fixed static runner missing')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    command = [str(PYTHON), '-I', '-S', '-B', str(script),
               '--output-dir', str(args.output_dir / 'driver')]
    start = time.monotonic()
    work_end, term_end, kill_end = start + 58, start + 59, start + 59.5
    interrupted = []
    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, lambda number, frame: interrupted.append(number))
    process = None
    timed_out = False
    terminal_observed_at = None
    failure = None
    signals = []
    observations = []
    record = {'schema': 'cm2-first-static-launch-v1', 'command': command, 'cwd': str(root),
              'started_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'python': identity(PYTHON), 'runner': identity(script),
              'budgets_seconds': {'outer': 60, 'internal_runner': 55, 'work': 58,
                                  'term': 1, 'kill_reap': 0.5, 'report_reserve': 0.5}}

    def observe():
        nonlocal terminal_observed_at
        code = process.poll()  # Popen is the sole direct-child waitpid owner.
        exists = group_exists(process.pid)
        observations.append({'elapsed_seconds': time.monotonic() - start,
                             'direct_child_exit': code, 'process_group_exists': exists})
        complete = code is not None and not exists
        if complete and terminal_observed_at is None:
            terminal_observed_at = time.monotonic()
        return complete

    def stop_group(sig):
        try:
            os.killpg(process.pid, sig)
            sent = True
        except ProcessLookupError:
            sent = False
        signals.append({'signal': sig, 'sent': sent, 'elapsed_seconds': time.monotonic() - start})

    try:
        with (args.output_dir / 'stdout.json').open('xb') as stdout, (args.output_dir / 'stderr.log').open('xb') as stderr:
            process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            record['pid'] = process.pid
            record['pgid'] = process.pid
            save(args.output_dir / 'started.json', record)
            complete = False
            while time.monotonic() < work_end and not interrupted:
                if observe():
                    complete = True
                    break
                if process.returncode is not None:
                    # A dead runner with a live group is not a completed run.
                    failure = 'runner exited while its process group remained'
                    break
                time.sleep(min(0.05, max(0, work_end - time.monotonic())))
            if not complete:
                timed_out = not interrupted and time.monotonic() >= work_end
                cleanup_start = time.monotonic()
                stop_group(signal.SIGTERM)
                # Early signal/error does not wait for a distant work deadline.
                until = min(term_end, cleanup_start + 1)
                while time.monotonic() < until:
                    if observe():
                        complete = True
                        break
                    time.sleep(min(0.02, max(0, until - time.monotonic())))
                if not complete:
                    stop_group(signal.SIGKILL)
                    until = min(kill_end, time.monotonic() + 0.5)
                    while time.monotonic() < until:
                        if observe():
                            complete = True
                            break
                        time.sleep(min(0.01, max(0, until - time.monotonic())))
            observe()
    except BaseException as error:
        failure = {'type': type(error).__name__, 'message': str(error)}
        if process is not None:
            # Do not perform an unbounded wait even on a launcher exception.
            stop_group(signal.SIGKILL)
            until = min(kill_end, time.monotonic() + 0.5)
            while time.monotonic() < until:
                if observe():
                    break
                time.sleep(min(0.01, max(0, until - time.monotonic())))
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    code = None if process is None else process.poll()
    exists = False if process is None else group_exists(process.pid)
    if process is not None:
        observations.append({'elapsed_seconds': time.monotonic() - start,
                             'direct_child_exit': code, 'process_group_exists': exists})
    work_elapsed = None if terminal_observed_at is None else terminal_observed_at - start
    work_within_budget = work_elapsed is not None and work_elapsed < 58
    timed_out = timed_out or (work_elapsed is not None and not work_within_budget)
    files = {}
    for name in ('stdout.json', 'stderr.log'):
        path = args.output_dir / name
        if path.exists():
            files[name] = identity(path)
    total_elapsed = time.monotonic() - start
    passed = process is not None and code == 0 and not exists and not timed_out and not interrupted and failure is None and work_within_budget and total_elapsed < 60
    record.update({'returncode': code, 'direct_child_reaped': code is not None,
                   'work_elapsed_seconds': work_elapsed, 'work_within_budget': work_within_budget,
                   'total_within_budget': total_elapsed < 60,
                   'process_group_absent': not exists, 'timed_out': timed_out,
                   'signals': signals, 'interrupted': interrupted, 'failure': failure,
                   'observations': observations, 'files': files, 'passed': passed,
                   'elapsed_seconds': total_elapsed,
                   'indirect_children_individually_reaped_claimed': False})
    save(args.output_dir / 'run.json', record)
    print(json.dumps({'output_dir': str(args.output_dir), 'returncode': code,
                      'passed': passed, 'timed_out': timed_out, 'process_group_absent': not exists}))
    return 0 if passed else 124 if timed_out else 1


if __name__ == '__main__':
    raise SystemExit(main())
