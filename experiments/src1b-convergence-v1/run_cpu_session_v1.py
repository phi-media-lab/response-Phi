#!/usr/bin/env python3
"""SRC1 B one-fixture CPU wrapper; original permit and process cleanup.

Exactly one B-C01..B-C10 fixture per fresh OS session. No GPU or model entry is selectable.
The native fixture check may pass with an expected original Core failure; the
original Core status and retained observer roots remain in its own raw report.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import select
import selectors
import signal
import subprocess
import sys
import time
import uuid

LIMIT = 1 << 20
CPU_MODES = ('parent-composition-cpu',)
WORK, TOTAL, TERM, KILL_REAP, REPORT = 55, 60, 1.5, 1.5, 2


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def decode(line):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate stdout JSON key')
            result[key] = value
        return result
    obj = json.loads(line, object_pairs_hook=pairs,
                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON')))
    require(type(obj) is dict, 'Swift stdout must be JSON objects')
    return obj


def processes():
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,stat=,comm='],
                            capture_output=True, timeout=1, check=False)
    require(result.returncode == 0, 'OS process query failed')
    rows = []
    for line in result.stdout.decode('utf-8', errors='replace').splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5:
            rows.append({'pid': int(fields[0]), 'ppid': int(fields[1]), 'pgid': int(fields[2]),
                         'state': fields[3], 'command': fields[4]})
    return rows


class SessionRun:
    def __init__(self, args):
        self.start = time.monotonic()
        self.args = args
        self.session = str(uuid.uuid4())
        self.output = Path(args.output_dir)
        self.output.mkdir(parents=True, exist_ok=False)
        # Only the wrapper owns this root. Swift exclusively creates its own
        # initially absent directory, including frontend/worker evidence.
        self.driver_output = self.output / 'driver'
        self.result_path = self.driver_output / 'result.json'
        self.control = (self.output / 'control.jsonl').open('xb', buffering=0)
        self.permits = (self.output / 'permits.jsonl').open('xb', buffering=0)
        self.raw = {name: (self.output / f'swift.{name}').open('xb', buffering=0)
                    for name in ['stdout.jsonl', 'stderr.log']}
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.stdout_buffer = bytearray()
        self.seen_workers = {}
        self.seen_pids = set()
        self.control_closed = False
        self.stream_eof = {'stdout.jsonl': False, 'stderr.log': False}
        self.failures = []
        self.timed_out = False
        self.phase = 'running'
        self.stop_deadline = None
        self.last_members = None
        self.exit_seen = False
        self.interrupted = None
        self.input_sha = digest(args.inputs)
        self.work_deadline = self.start + args.timeout
        self.cleanup_started = None
        self.total_deadline = self.start + TOTAL
        self.terminal_observed_at = None
        require(digest(args.swift) == args.binary_sha256, 'binary differs from accepted build identity')
        self.binary_sha = args.binary_sha256
        self.vocabulary_sha = digest(args.vocab)
        require(self.vocabulary_sha == 'aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4',
                'fixed production vocabulary authority required even for native failure fixtures')
        self.command = [args.swift, '--inputs', args.inputs,
                        '--session', self.session, '--pgid', '0',
                        '--output-dir', str(self.driver_output), '--vocab', args.vocab,
                        '--sources-dir', args.sources_dir, '--fixture', args.fixture]

    def event(self, kind, **data):
        row = {'kind': kind, 'monotonic_ns': time.monotonic_ns(), 'session': self.session, **data}
        self.control.write((json.dumps(row, separators=(',', ':'), allow_nan=False) + '\n').encode())

    def members(self):
        rows = [r for r in processes() if r['pgid'] == self.process.pid]
        self.last_members = rows
        self.event('group_observation', members=rows)
        return rows

    def permit_health(self):
        if time.monotonic() >= self.work_deadline:
            self.timed_out = True
        require(self.phase == 'running' and self.interrupted is None and not self.timed_out,
                'permit blocked by observed stop/signal/session deadline')

    def send_permit(self, obj):
        line = (json.dumps(obj, separators=(',', ':')) + '\n').encode()
        sent, deadline = 0, time.monotonic() + 1
        while sent < len(line):
            self.permit_health()
            remaining = min(deadline, self.work_deadline) - time.monotonic()
            require(remaining > 0, 'permit write deadline')
            writable = select.select([], [self.process.stdin.fileno()], [], remaining)[1]
            self.permit_health()  # Recheck after the potentially blocking OS wait, before each write.
            require(writable, 'permit pipe blocked')
            written = os.write(self.process.stdin.fileno(), line[sent:])
            require(written > 0, 'permit write made no progress')
            self.permits.write(line[sent:sent + written])
            sent += written
        self.event('permit_written', worker_id=obj['worker_id'], pid=obj['pid'], bytes=sent,
                   permit_id=obj['permit_id'])

    def swift_event(self, obj):
        require(type(obj.get('v')) is int and obj.get('v') == 1 and obj.get('session') == self.session and
                type(obj.get('kind')) is str, 'invalid Swift event envelope')
        if obj['kind'] == 'control_close':
            require(not self.control_closed, 'duplicate control_close')
            current = {r['pid'] for r in self.members()}
            require(not current.intersection(self.seen_pids), 'control_close before worker exit/reap')
            self.process.stdin.close()
            self.control_closed = True
            self.event('control_stdin_closed')
            return
        if obj['kind'] != 'worker_hello':
            return
        require(not self.control_closed, 'HELLO after control close')
        hello = obj.get('hello')
        require(type(hello) is dict, 'missing HELLO')
        wid, pid = obj.get('worker_id'), obj.get('created_pid')
        require(type(wid) is str and bool(wid) and type(pid) is int and pid > 1, 'invalid worker identity')
        require(wid not in self.seen_workers and pid not in self.seen_pids, 'duplicate HELLO')
        require(all(type(hello.get(k)) is int for k in ['v', 'pid', 'pgid', 'swift_pid', 'event_seq', 'worker_ns']) and
                hello.get('v') == 1 and hello.get('kind') == 'HELLO' and
                hello.get('session') == self.session and hello.get('worker_id') == wid and
                hello.get('pid') == pid and hello.get('swift_pid') == self.process.pid and
                hello.get('pgid') == self.process.pid and type(hello.get('event_seq')) is int and hello.get('event_seq') == 1 and
                hello.get('inputs_sha256') == self.input_sha, 'HELLO binding mismatch')
        expected_backend = 'stub' if self.args.mode in CPU_MODES else 'mlx'
        require(hello.get('backend') == expected_backend, 'wrong backend for session mode')
        flags = hello.get('python_flags')
        require(type(flags) is dict and set(flags) == {'isolated', 'no_site', 'dont_write_bytecode'} and
                all(type(value) is int and value == 1 for value in flags.values()),
                'worker did not start under -I -S -B')
        require(self.args.mode in CPU_MODES or not self.seen_workers, 'real session may have only one worker')
        observed = [r for r in processes() if r['pid'] == pid]
        require(len(observed) == 1 and observed[0]['ppid'] == self.process.pid and
                observed[0]['pgid'] == self.process.pid and os.getpgid(pid) == self.process.pid,
                'OS child/group mismatch')
        self.event('worker_os_verified', worker_id=wid, hello=hello, os=observed[0])
        self.seen_workers[wid] = pid
        self.seen_pids.add(pid)
        scenario = obj.get('scenario', 'success')
        if scenario == 'no_permit':
            require(self.args.mode in CPU_MODES, 'no_permit is CPU-only')
            self.event('permit_withheld_cpu', worker_id=wid, pid=pid)
            return
        require(self.args.mode in CPU_MODES or scenario == 'success', 'real worker stub scenario forbidden')
        permit = {'v': 1, 'kind': 'LOAD_PERMIT', 'session': self.session, 'worker_id': wid,
                  'pid': pid, 'pgid': self.process.pid, 'swift_pid': self.process.pid,
                  'permit_id': str(uuid.uuid4()), 'inputs_sha256': self.input_sha}
        self.send_permit(permit)

    def drain(self, timeout):
        for key, _ in self.selector.select(timeout):
            name = key.data
            block = os.read(key.fd, 65536)
            if not block:
                self.selector.unregister(key.fd)
                self.stream_eof[name] = True
                self.event('swift_stream_eof', stream=name)
                continue
            self.raw[name].write(block)
            if name == 'stdout.jsonl':
                self.stdout_buffer.extend(block)
                while b'\n' in self.stdout_buffer:
                    line, rest = self.stdout_buffer.split(b'\n', 1)
                    self.stdout_buffer = bytearray(rest)
                    require(len(line) + 1 <= LIMIT, 'Swift stdout line too long')
                    # After a fatal failure preserve further raw data, but grant no more permits.
                    if self.phase == 'running':
                        self.swift_event(decode(line.decode('utf-8')))
                require(len(self.stdout_buffer) < LIMIT, 'Swift partial line too long')

    def signal_group(self, sig):
        try:
            os.killpg(self.process.pid, sig)
            self.event('group_signal', signal=int(sig), delivered=True)
        except ProcessLookupError:
            self.event('group_signal', signal=int(sig), delivered=False, reason='no_such_group')
        except OSError as exc:
            self.failures.append(f'group signal failed: {exc}')
            self.event('group_signal', signal=int(sig), delivered=False, reason=str(exc))

    def stop(self, reason):
        self.failures.append(reason)
        self.event('failure', reason=reason)
        if self.phase == 'running':
            self.phase = 'term'
            self.cleanup_started = time.monotonic()
            self.signal_group(signal.SIGTERM)
            self.stop_deadline = min(self.cleanup_started + TERM, self.work_deadline + TERM, self.total_deadline - KILL_REAP - REPORT)

    def run(self):
        started = {'schema': 'src1b-cpu-session-start-v1', 'session': self.session, 'mode': self.args.mode,
                   'return_conversion': 'original', 'return_service': 'unchanged', 'competition_tasks': 0,
                   'fixture': self.args.fixture, 'binary_sha256': self.binary_sha,
                   'vocabulary_sha256': self.vocabulary_sha,
                   'started_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   'command': self.command, 'cwd': str(Path.cwd()),
                   'output_dir': str(self.output), 'driver_output_dir': str(self.driver_output),
                   'result_path': str(self.result_path),
                   'timeout_seconds': self.args.timeout, 'inputs_sha256': self.input_sha,
                   'total_budget_seconds': TOTAL, 'term_seconds': TERM, 'kill_reap_seconds': KILL_REAP,
                   'work_deadline_origin': 'one wrapper monotonic start', 'report_reserve_seconds': REPORT}
        (self.output / 'started.json').write_text(json.dumps(started, indent=2) + '\n')
        previous_handlers = {}
        def interrupted(signum, _frame):
            self.interrupted = signum
        for signum in [signal.SIGTERM, signal.SIGINT]:
            previous_handlers[signum] = signal.signal(signum, interrupted)
        try:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
            if time.monotonic() >= self.work_deadline:
                self.timed_out = True
            require(not self.timed_out and self.interrupted is None,
                    'input preparation crossed work/signal gate before spawn')
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, start_new_session=True, env=env)
            self.event('swift_spawned', pid=self.process.pid, expected_pgid=self.process.pid)
            os.set_blocking(self.process.stdin.fileno(), False)
            for stream, name in [(self.process.stdout, 'stdout.jsonl'), (self.process.stderr, 'stderr.log')]:
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ, name)
            require(os.getpgid(self.process.pid) == self.process.pid, 'Swift group creation failed')
            while True:
                now = time.monotonic()
                if self.phase == 'running' and self.interrupted is not None:
                    self.stop(f'wrapper_received_signal_{self.interrupted}')
                if self.phase == 'running' and now >= self.work_deadline:
                    self.timed_out = True
                    self.stop('outer_timeout')
                if self.phase == 'term' and now >= self.stop_deadline:
                    self.signal_group(signal.SIGKILL)
                    self.phase = 'kill'
                    self.stop_deadline = min(self.cleanup_started + TERM + KILL_REAP, self.work_deadline + TERM + KILL_REAP, self.total_deadline - REPORT)
                if self.phase == 'kill' and now >= self.stop_deadline:
                    self.event('post_kill_observation_deadline')
                    break
                try:
                    self.drain(0.05)
                except Exception as exc:
                    self.stop(f'stream/protocol: {type(exc).__name__}: {exc}')
                code = self.process.poll()  # waitpid on the direct child; never an unbounded wait.
                if code is not None and not self.exit_seen:
                    self.exit_seen = True
                    self.event('swift_reaped', returncode=code)
                    if code != 0:
                        self.stop(f'swift_exit_{code}')
                    try:
                        if self.members():
                            self.stop('residual_group_after_swift_exit')
                    except Exception as exc:
                        self.stop(f'OS observation: {exc}')
                if code is not None and all(self.stream_eof.values()):
                    try:
                        if not self.members():
                            self.terminal_observed_at = time.monotonic()
                            if self.phase == 'running' and self.terminal_observed_at >= self.work_deadline:
                                self.timed_out = True
                                self.failures.append('terminal observation at or after work deadline')
                            break
                    except Exception as exc:
                        self.stop(f'OS observation: {exc}')
            require(not self.stdout_buffer, 'truncated Swift stdout')
            if self.phase == 'running':
                require(self.control_closed, 'missing final control_close')
                result = json.loads(self.result_path.read_text())
                require(result.get('passed') is True, 'missing/failed Swift result')
                require(digest(self.args.inputs) == self.input_sha, 'inputs changed during session')
                require(digest(self.args.swift) == self.binary_sha, 'binary changed during session')
                require(digest(self.args.vocab) == self.vocabulary_sha, 'vocabulary changed during session')
        except Exception as exc:
            self.failures.append(f'{type(exc).__name__}: {exc}')
            self.event('wrapper_exception', error=self.failures[-1])
            previous_phase = self.phase
            self.phase = 'exception'  # No further bootstrap permits during cleanup.
            if self.process is not None and previous_phase == 'running':
                # Startup/outer exceptions use the same bounded group cleanup.
                self.cleanup_started = time.monotonic()
                self.signal_group(signal.SIGTERM)
                deadline = min(self.cleanup_started + TERM, self.work_deadline + TERM, self.total_deadline - KILL_REAP - REPORT)
                while time.monotonic() < deadline:
                    try:
                        self.drain(0.05)
                        if self.process.poll() is not None and all(self.stream_eof.values()) and not self.members():
                            break
                    except Exception:
                        pass
                try:
                    remains = bool(self.members())
                except Exception:
                    remains = True
                if remains or self.process.poll() is None:
                    self.signal_group(signal.SIGKILL)
                    deadline = min(self.cleanup_started + TERM + KILL_REAP, self.work_deadline + TERM + KILL_REAP, self.total_deadline - REPORT)
                    while time.monotonic() < deadline:
                        try:
                            self.drain(0.05)
                            if self.process.poll() is not None and all(self.stream_eof.values()) and not self.members():
                                break
                        except Exception:
                            pass
        finally:
            if self.process is not None:
                try:
                    self.members()
                except Exception as exc:
                    self.last_members = None
                    self.failures.append(f'final OS observation unavailable: {exc}')
                for stream in [self.process.stdin, self.process.stdout, self.process.stderr]:
                    if stream and not stream.closed:
                        stream.close()
            self.selector.close()
            for stream in [*self.raw.values(), self.permits, self.control]:
                stream.close()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        code = self.process.poll() if self.process else None
        if code is None or self.last_members is None or self.last_members or not all(self.stream_eof.values()):
            self.failures.append('process/IO closure incomplete; unresolved')
        files = {}
        for path in sorted(self.output.rglob('*')):
            if path.is_file():
                files[str(path.relative_to(self.output))] = {'bytes': path.stat().st_size, 'sha256': digest(path)}
        elapsed = time.monotonic() - self.start
        if elapsed >= TOTAL:
            self.timed_out = True
            self.failures.append('total envelope exhausted before final record')
        status = 124 if self.timed_out else (1 if self.failures or code != 0 else 0)
        record = {**started, 'schema': 'src1b-cpu-session-run-v1', 'returncode': code,
                  'work_terminal_seconds': None if self.terminal_observed_at is None else self.terminal_observed_at - self.start,
                  'within_total_budget': elapsed < TOTAL,
                  'wrapper_returncode': status, 'timed_out': self.timed_out,
                  'interrupted_signal': self.interrupted,
                  'elapsed_seconds': elapsed, 'failures': self.failures,
                  'worker_pids': self.seen_workers, 'remaining_group': self.last_members,
                  'stream_eof': self.stream_eof, 'control_closed': self.control_closed, 'files': files}
        (self.output / 'run.json').write_text(json.dumps(record, indent=2) + '\n')
        print(json.dumps({'output_dir': str(self.output), 'driver_output_dir': str(self.driver_output),
                          'result_path': str(self.result_path), 'wrapper_returncode': status,
                          'returncode': code, 'timed_out': self.timed_out}), flush=True)
        # Recheck after report serialization/output; late completion is retained
        # as failure, not silently returned as a successful bounded run.
        record['elapsed_seconds'] = time.monotonic() - self.start
        record['within_total_budget'] = record['elapsed_seconds'] < TOTAL
        if not record['within_total_budget']:
            self.timed_out = True
            self.failures.append('total envelope exhausted after final record')
        if self.interrupted is not None and not self.failures:
            self.failures.append('wrapper interrupted before return')
        status = 124 if self.timed_out else (1 if self.failures or code != 0 else 0)
        record.update(wrapper_returncode=status, timed_out=self.timed_out, failures=self.failures)
        (self.output / 'run.json').write_text(json.dumps(record, indent=2) + '\n')
        # No I/O remains on the success path after this final time/signal gate.
        final_elapsed = time.monotonic() - self.start
        if status == 0 and (final_elapsed >= TOTAL or self.interrupted is not None):
            self.timed_out = self.timed_out or final_elapsed >= TOTAL
            self.failures.append('final record write crossed total/signal return gate')
            status = 124 if self.timed_out else 1
            record.update(wrapper_returncode=status, timed_out=self.timed_out,
                          failures=self.failures, interrupted_signal=self.interrupted,
                          elapsed_seconds=final_elapsed, within_total_budget=final_elapsed < TOTAL,
                          final_record_write_crossed_return_gate=True)
            (self.output / 'run.json').write_text(json.dumps(record, indent=2) + '\n')
        return status


def main():
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    required = ('swift', 'binary-sha256', 'inputs', 'output-dir', 'sources-dir', 'vocab', 'mode', 'fixture')
    for option in required:
        options = {'required': True}
        if option == 'mode':
            options['choices'] = CPU_MODES
        elif option == 'fixture':
            options['choices'] = ['B-C%02d' % i for i in range(1, 11)]
        p.add_argument('--' + option, **options)
    p.add_argument('--timeout', type=float)
    argv = sys.argv[1:]
    for option in required + ('timeout',):
        count = sum(arg == '--' + option or arg.startswith('--' + option + '=') for arg in argv)
        if (option in required and count != 1) or (option == 'timeout' and count > 1):
            p.error('exact option population: --' + option)
    args = p.parse_args(argv)
    args.timeout = WORK if args.timeout is None else args.timeout
    require(0 < args.timeout <= WORK, 'timeout must not extend plan budget')
    require(Path(args.swift).name == 'src1b-cpu', 'fixed parent CPU product required')
    require(len(args.binary_sha256) == 64 and all(c in '0123456789abcdef' for c in args.binary_sha256),
            'exact lowercase build SHA256 required')
    for name in ('swift', 'inputs', 'output_dir', 'sources_dir', 'vocab'):
        require(Path(getattr(args, name)).is_absolute(), 'all paths must be absolute')
    return SessionRun(args).run()


if __name__ == '__main__':
    raise SystemExit(main())
