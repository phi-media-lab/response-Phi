#!/usr/bin/env python3
"""One recorded Swift build; no candidate executable or model is run.

The fixed 120-second envelope allocates 110s to work, 4s to TERM,
4s to KILL/reap, and 2s to observation/reporting. OS scheduling and file
operations are not claimed to have a hard real-time bound.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import time

SWIFT = '/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/swift'
PRODUCTS = ('cm2-checked', 'cm2-deterministic', 'cm2-reference', 'cm2-cpu', 'ebd1-native-cpu', 'ebd1-source-cpu')


def identity(path):
    data = path.read_bytes()
    return {'path': str(path), 'resolved_path': str(path.resolve()),
            'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def group_exists(pgid, record):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as error:
        # SIG0 denied is not evidence of absence. Keep bounded polling/reaping
        # instead of aborting cleanup before Popen observes its child's exit.
        record['group_probe_permission_denied_count'] = record.get('group_probe_permission_denied_count', 0) + 1
        record.setdefault('first_group_probe_permission_error', {
            'operation': 'killpg(SIG0)', 'pgid': pgid,
            'errno': error.errno, 'message': str(error)})
        return True


def product_identity(path):
    row = {'path': str(path), 'regular': False, 'executable': False}
    try:
        resolved = path.resolve(strict=True)
        row['resolved_path'] = str(resolved)
        row['regular'] = stat.S_ISREG(resolved.stat().st_mode)
        row['executable'] = row['regular'] and os.access(resolved, os.X_OK)
        if row['regular']:
            row.update(identity(path))
    except OSError as error:
        row['error'] = {'type': type(error).__name__, 'message': str(error)}
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if not args.package.is_absolute() or not args.output_dir.is_absolute():
        parser.error('absolute package and output paths are required')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    command = [SWIFT, 'build', '--package-path', str(args.package),
               '--scratch-path', str(args.output_dir / 'build'),
               '--configuration', 'release', '--disable-sandbox', '--jobs', '2']
    started = time.monotonic()
    record = {'schema': 'ebd1-first-build-v1', 'command': command,
              'cwd': str(args.package), 'started_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'budgets_seconds': {'total': 120, 'work': 110, 'term': 4, 'kill_reap': 4, 'report_margin': 2},
              'runner': identity(Path(__file__)), 'swift': identity(Path(SWIFT)),
              'package': identity(args.package / 'Package.swift'),
              'candidate_executed': False, 'signals': [], 'timed_out': False}
    process = None
    interrupted = None
    previous_handlers = {}

    def on_signal(signum, _frame):
        nonlocal interrupted
        if interrupted is None:
            interrupted = signum

    def observe_roots():
        if process is None:
            return
        process.poll()  # Actually reap the directly created child if it exited.
        record['returncode'] = process.returncode
        record['direct_child_reaped'] = process.returncode is not None
        try:
            record['process_group_absent'] = not group_exists(process.pid, record)
        except OSError as error:
            record['process_group_absent'] = False
            record['group_observation_error'] = str(error)

    def cleanup():
        if process is None or 'cleanup_started_seconds' in record:
            return
        cleanup_started = time.monotonic()
        record['cleanup_started_seconds'] = cleanup_started - started
        # Both the actual cleanup start and the original total envelope bind
        # these stages, including an early failure with unused work budget.
        stages = [(signal.SIGTERM, min(cleanup_started + 4, started + 114)),
                  (signal.SIGKILL, min(cleanup_started + 8, started + 118))]
        record['cleanup_deadlines_seconds'] = [deadline - started for _, deadline in stages]
        try:
            for sig, deadline in stages:
                process.poll()
                remaining_group = group_exists(process.pid, record)
                if not remaining_group and process.returncode is not None:
                    break
                # start_new_session created this expected PGID. If getpgid
                # raced with leader exit, its descendants can still remain.
                # Never signal a different, merely observed process group.
                if remaining_group:
                    try:
                        os.killpg(process.pid, sig)
                        delivered = True
                    except ProcessLookupError:
                        delivered = False
                    record['signals'].append({'signal': sig.name, 'target': 'created-group',
                        'pgid': process.pid, 'delivered': delivered,
                        'elapsed_seconds': time.monotonic() - started})
                if record.get('pgid') != process.pid and process.returncode is None:
                    try:
                        process.send_signal(sig)
                        delivered = True
                    except ProcessLookupError:
                        delivered = False
                    record['signals'].append({'signal': sig.name, 'target': 'direct-child',
                        'pid': process.pid, 'delivered': delivered,
                        'elapsed_seconds': time.monotonic() - started})
                while time.monotonic() < deadline:
                    process.poll()
                    if not group_exists(process.pid, record) and process.returncode is not None:
                        break
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        except OSError as error:
            record['cleanup_error'] = {'type': type(error).__name__, 'message': str(error)}
        finally:
            observe_roots()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, on_signal)
    try:
        with (args.output_dir / 'stdout.log').open('xb') as out, (args.output_dir / 'stderr.log').open('xb') as err:
            if interrupted is not None:
                raise RuntimeError('interrupted before build child creation')
            process = subprocess.Popen(command, cwd=args.package, stdout=out, stderr=err, start_new_session=True)
            record['pid'] = process.pid
            record['expected_pgid'] = process.pid
            record['pgid'] = os.getpgid(process.pid)
            record['pgid_verified'] = record['pgid'] == process.pid
            if not record['pgid_verified']:
                raise RuntimeError('build child is not its new group leader')
            (args.output_dir / 'started.json').write_text(json.dumps(record, indent=2) + '\n')
            while True:
                code = process.poll()
                if interrupted is not None or code is not None:
                    break
                remaining = started + 110 - time.monotonic()
                if remaining <= 0:
                    record['timed_out'] = True
                    break
                time.sleep(min(0.05, remaining))
            record['original_child_returncode'] = process.returncode
            record['unexpected_group_remaining'] = process.returncode is not None and group_exists(process.pid, record)
            if interrupted is not None or record['timed_out'] or record['unexpected_group_remaining'] or process.returncode != 0:
                cleanup()
            observe_roots()
    except BaseException as error:
        record['error'] = {'type': type(error).__name__, 'message': str(error)}
        cleanup()
    try:
        record['products'] = {name: product_identity(args.output_dir / 'build' / 'release' / name)
                              for name in PRODUCTS}
        record['files'] = {name: identity(args.output_dir / name) for name in ['stdout.log', 'stderr.log'] if (args.output_dir / name).exists()}
        record['interrupted_signal'] = interrupted
        record['elapsed_seconds'] = time.monotonic() - started
        record['within_total_budget'] = record['elapsed_seconds'] <= 120
        record['passed'] = bool('error' not in record and 'cleanup_error' not in record
            and interrupted is None and not record['timed_out'] and record.get('pgid_verified')
            and record.get('original_child_returncode') == 0 and record.get('returncode') == 0
            and not record.get('unexpected_group_remaining') and record['within_total_budget']
            and record.get('direct_child_reaped') and record.get('process_group_absent')
            and all(row['regular'] and row['executable'] and 'sha256' in row for row in record['products'].values()))
        (args.output_dir / 'run.json').write_text(json.dumps(record, indent=2) + '\n')
        print(json.dumps({'output_dir': str(args.output_dir), 'passed': record['passed'],
                          'returncode': record.get('returncode'), 'timed_out': record['timed_out']}))
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0 if record['passed'] else 124 if record['timed_out'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
