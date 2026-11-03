#!/usr/bin/env python3
"""Independent P6 numeric, event, command, timing and artifact audit (stdlib only)."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys
from control_math import ARRAY_NAMES, C, D, PENALTY, array_hash, reference, require, verify_reference_document

MODES = ('cpu-feedback', 'gpu-step', 'gpu-preencoded', 'device-loop-global')
POLICIES = ('none', 'before-submit', 'after-first-commit')
CHECKS = {'all_arrays_match_independent_reference', 'completed_rounds_match_cancel_boundary',
          'actual_command_dispatch_counts', 'all_commands_completed', 'complete_raw_array_hashes',
          'all_gpu_intervals_recorded'}
COMMAND_EVENTS = {'command.encode_started', 'dispatch.encoded', 'command.encode_ended', 'command.submitted',
                  'command.commit_returned', 'command.completed', 'completion.observed'}
BASE_EVENTS = ['trial.started', 'resource.acquired', 'work.started', 'host.submissions_closed',
               'algorithm.completed_prefix', 'work.completed', 'validation.started', 'validation.completed',
               'resource.released', 'trial.drained']


def strict_json(path):
    def reject(value):
        raise ValueError('non-finite JSON: ' + value)
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, 'duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), parse_constant=reject, object_pairs_hook=pairs)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def number(value):
    require(type(value) in (int, float) and math.isfinite(value), 'invalid finite number')
    return value


def close(actual, expected, label, tolerance=0.00001):
    require(abs(number(actual) - number(expected)) <= tolerance, 'incorrect ' + label)


def integer(value, low, high, label):
    require(type(value) is int and low <= value <= high, 'invalid ' + label)


def check_config(c):
    require(set(c) == {'mode', 'C', 'D', 'T', 'seed', 'penalty', 'dtype', 'cancel_policy', 'trials', 'warmups', 'timeout_seconds'}, 'configuration keys')
    require(c['mode'] in MODES and c['cancel_policy'] in POLICIES, 'unknown mode or cancellation')
    for name, value in [('C', C), ('D', D), ('seed', 7), ('penalty', PENALTY)]:
        require(type(c[name]) is int and c[name] == value, 'fixed config differs: ' + name)
    require(type(c['T']) is int and c['T'] in (16, 64) and c['dtype'] == 'int32', 'unsupported shape/dtype')
    integer(c['trials'], 1, 1000, 'trial count'); integer(c['warmups'], 0, 10, 'warmup count')
    integer(c['timeout_seconds'], 1, 60, 'timeout')


def command_specs(mode, completed):
    if not completed:
        return []
    if mode == 'cpu-feedback':
        return [(r, 1, stage, [(stage, r)]) for r in range(completed) for stage in ('score', 'update')]
    if mode == 'gpu-step':
        return [(r, 1, 'round', [(s, r) for s in ('score', 'select', 'update')]) for r in range(completed)]
    if mode == 'gpu-preencoded':
        return [(0, completed, 'batch', [(s, r) for r in range(completed) for s in ('score', 'select', 'update')])]
    return [(0, completed, 'loop', [('loop', 0)])]


def analyze_trial(t, c, trial_id, warmup):
    require(t['passed'] is True and not t.get('error'), 'failed trial')
    require(type(t['trial_id']) is int and t['trial_id'] == trial_id and t['warmup'] is warmup and t['config'] == c, 'trial identity/config')
    completed = 0 if c['cancel_policy'] == 'before-submit' else (
        1 if c['cancel_policy'] == 'after-first-commit' and c['mode'] in MODES[:2] else c['T'])
    expected = reference(c['T'], c['seed'], completed)
    actual = t['actual']
    integer(actual['completed_rounds'], 0, c['T'], 'completed rounds')
    for name in ARRAY_NAMES:
        require(array_hash(actual[name]) == expected['sha256'][name], 'incorrect full array: ' + name)
    require(actual == expected['actual'], 'full numeric state mismatch')
    require(t['actual_sha256'] == expected['sha256'] == t['expected_sha256'], 'array hashes mismatch')
    checks = t['checks']
    require(len(checks) == len(CHECKS) and {x['name'] for x in checks} == CHECKS and all(x['passed'] is True for x in checks), 'self-check set or status')
    events = t['events']
    require(isinstance(events, list) and events, 'empty events')
    previous = -1
    for e in events:
        require(set(e) in ({'name', 't_ms', 'request_id', 'details'}, {'name', 't_ms', 'request_id', 'details', 'command_id'}), 'event keys')
        require(number(e['t_ms']) >= max(0, previous) and e['request_id'] == 'A', 'event clock/order/request')
        previous = e['t_ms']
        require(type(e['details']) is dict and all(type(k) is str and type(v) is str for k, v in e['details'].items()), 'event details must be strings')
        require(('command_id' in e) == (e['name'] in COMMAND_EVENTS), 'command event ID missing/extra')
    def indices(name, command=None):
        return [i for i, e in enumerate(events) if e['name'] == name and (command is None or e.get('command_id') == command)]
    def one(name, command=None):
        found = indices(name, command)
        require(len(found) == 1, 'missing/duplicate event: ' + name)
        return found[0]
    def stamp(name, command=None):
        return events[one(name, command)]['t_ms']
    def ordered(*indexes):
        require(all(a < b for a, b in zip(indexes, indexes[1:])), 'event causal order violated')
    ordered(*(one(name) for name in BASE_EVENTS))
    counts = Counter(BASE_EVENTS)
    specs = command_specs(c['mode'], completed)
    commands = t['commands']
    require(len(commands) == len(specs), 'command count')
    origin = None
    enc_ms = 0
    for cid, (cmd, spec) in enumerate(zip(commands, specs)):
        first, count, stage, dispatches = spec
        require(type(cmd['command_id']) is int and cmd['command_id'] == cid, 'command identity')
        require(type(cmd['first_round']) is int and type(cmd['round_count']) is int, 'command integer shape')
        require((cmd['first_round'], cmd['round_count'], cmd['stage']) == (first, count, stage), 'command shape')
        require(cmd['status'] == 'completed' and not cmd.get('error'), 'command failed')
        want = [{'stage': s, 'round': r, 'parameters': {'candidates': C, 'dimensions': D, 'rounds': c['T'], 'step': r, 'penalty': PENALTY}} for s, r in dispatches]
        require(cmd['dispatches'] == want, 'dispatch parameters/stages differ')
        for row in cmd['dispatches']:
            require(type(row['round']) is int and all(type(v) is int for v in row['parameters'].values()), 'noninteger dispatch ABI')
        for name in COMMAND_EVENTS - {'dispatch.encoded'}:
            counts[name] += 1
        counts['dispatch.encoded'] += len(dispatches)
        start, end, submit, returned, callback, observed = [one(name, cid) for name in
            ('command.encode_started', 'command.encode_ended', 'command.submitted', 'command.commit_returned', 'command.completed', 'completion.observed')]
        encoded = indices('dispatch.encoded', cid)
        require(len(encoded) == len(dispatches), 'dispatch event count')
        ordered(one('work.started'), start, *encoded, end, submit, returned, observed, one('host.submissions_closed'))
        ordered(submit, callback, observed)  # callback is allowed before commit returns
        if cid:
            ordered(one('completion.observed', cid - 1), start)
        require(events[start]['details'] == {'stage': stage, 'first_round': str(first), 'round_count': str(count)}, 'encode metadata')
        require(events[callback]['details'] == events[observed]['details'] == {'status': 'completed'}, 'completion status events')
        for index, (s, r) in zip(encoded, dispatches):
            require(events[index]['details'] == {'stage': s, 'round': str(r), 'parameter_step': str(r), 'parameter_bytes': '20'}, 'dispatch event ABI')
        enc_ms += events[end]['t_ms'] - events[start]['t_ms']
        submitted = number(cmd['submitted_mach_seconds']); cb = number(cmd['callback_mach_seconds'])
        current_origin = submitted - events[submit]['t_ms'] / 1000
        if origin is None:
            origin = current_origin
        close((current_origin - origin) * 1000, 0, 'command clock origin')
        close((cb - origin) * 1000, events[callback]['t_ms'], 'callback clock alignment')
        gs = number(cmd['gpu_start_seconds']); ge = number(cmd['gpu_end_seconds'])
        require(gs > 0 and submitted - 0.00001 <= gs <= ge <= cb + 0.00001, 'GPU interval outside submission/completion')
        if cid:
            require(commands[cid - 1]['gpu_end_seconds'] <= gs, 'serial commands GPU order')
    cpu_ms = 0
    if c['mode'] == 'cpu-feedback':
        starts, ends = indices('host.selection_started'), indices('host.selection_ended')
        require(len(starts) == len(ends) == completed, 'host selection count')
        counts.update({'host.selection_started': completed, 'host.selection_ended': completed})
        for r, (start, end) in enumerate(zip(starts, ends)):
            ordered(one('completion.observed', 2*r), start, end, one('command.encode_started', 2*r+1))
            winner = actual['winner_history'][r]
            require(events[start]['details'] == {'round': str(r)}, 'host selection round')
            require(events[end]['details'] == {'round': str(r), 'winner': str(winner), 'score': str(actual['score_history'][r*C+winner])}, 'host choice mismatch')
            cpu_ms += events[end]['t_ms'] - events[start]['t_ms']
    obs = t['observations']
    expected_obs = {'planned_rounds': c['T'], 'completed_rounds': completed, 'unexecuted_rounds': c['T']-completed,
                    'completed_dot_elements': completed*C*D, 'commands_submitted': len(specs),
                    'dispatches_encoded': sum(len(s[3]) for s in specs), 'final_inflight': 0, 'final_owned': 0}
    for name, value in expected_obs.items():
        require(type(obs[name]) is int and obs[name] == value, 'incorrect observation: ' + name)
    require(obs['errors'] == [], 'errors hidden in observations')
    require(events[one('algorithm.completed_prefix')]['details'] == {'completed_rounds': str(completed), 'planned_rounds': str(c['T'])}, 'prefix event')
    logical_bytes = 4*(C*D + D + C + c['T']*C + c['T'] + c['T']*D + 1)
    require(events[one('resource.acquired')]['details'] == {'resource_id': f'working-set-{trial_id}', 'buffers': '7', 'logical_bytes': str(logical_bytes)}, 'working set resource shape')
    require(events[one('resource.released')]['details']['resource_id'] == f'working-set-{trial_id}', 'released resource identity')
    metrics = {'work_ms': stamp('work.completed')-stamp('work.started'),
               'validation_ms': stamp('validation.completed')-stamp('validation.started'),
               'full_trial_ms': stamp('trial.drained')-stamp('trial.started'), 'encoding_ms': enc_ms,
               'cpu_selection_ms': cpu_ms,
               'gpu_execution_sum_ms': sum((x['gpu_end_seconds']-x['gpu_start_seconds'])*1000 for x in commands),
               'gpu_span_ms': (max(x['gpu_end_seconds'] for x in commands)-min(x['gpu_start_seconds'] for x in commands))*1000 if commands else 0}
    if commands:
        metrics['first_submit_to_completion_ms'] = stamp('completion.observed', len(commands)-1)-stamp('command.submitted', 0)
    cancelled = c['cancel_policy'] != 'none'
    require(obs['cancellation_requested'] is cancelled, 'cancel flag')
    require(events[one('host.submissions_closed')]['details'] == {'cancelled': str(cancelled).lower()}, 'submissions closed flag')
    phase = 'not-requested'
    if cancelled:
        counts.update({'cancel.requested': 1, 'cancel.accepted': 1})
        req, ack = one('cancel.requested'), one('cancel.accepted')
        ordered(one('work.started'), req, ack, one('host.submissions_closed'))
        require(events[req]['details'] == {'policy': c['cancel_policy'], 'source': 'synchronous harness injection'}, 'cancellation source')
        cancel = number(obs['cancel_mach_seconds'])
        if commands:
            ordered(one('command.commit_returned', 0), req, ack, one('completion.observed', 0))
            close((cancel-origin)*1000, events[req]['t_ms'], 'cancel clock')
            first = commands[0]
            phase = 'queued' if cancel < first['gpu_start_seconds'] else 'running' if cancel < first['gpu_end_seconds'] else 'finished'
        else:
            require(c['cancel_policy'] == 'before-submit', 'unexpected empty cancellation')
            phase = 'not-submitted'
        metrics.update(cancel_ack_ms=events[ack]['t_ms']-events[req]['t_ms'],
                       cancel_to_release_ms=stamp('resource.released')-events[req]['t_ms'])
    else:
        require('cancel_mach_seconds' not in obs, 'unexpected cancel timestamp')
    require(obs['cancellation_phase_first_command'] == phase, 'wrong GPU cancellation phase')
    require(Counter(e['name'] for e in events) == +counts, 'extra/missing events')
    require(set(t['metrics']) == set(metrics), 'metric names differ')
    for name, value in metrics.items():
        require(value >= 0, 'negative derived duration')
        close(t['metrics'][name], value, name)
    return {'trial_id': trial_id, 'warmup': warmup, 'mode': c['mode'], 'rounds': c['T'], 'cancel_policy': c['cancel_policy'],
            'completed_rounds': completed, 'commands': len(commands), 'dispatches': expected_obs['dispatches_encoded'],
            'cancellation_phase': phase, 'metrics': metrics, 'passed': True}


def analyze_document(d):
    require(d['schema_version'] == 1 and d['experiment'] == 'metal-control-location', 'wrong report schema')
    require(d['passed'] is True and not d.get('error'), 'top-level failure')
    c = d['config']; check_config(c)
    require(d['inputs'] == reference(c['T'], c['seed'])['inputs'], 'input values/hash differ from formula')
    for key in ('features', 'initial_context'):
        array_hash(d['inputs'][key])
    require(len(d['warmups']) == c['warmups'] and len(d['trials']) == c['trials'], 'missing trials/warmups')
    rows = []
    for i, t in enumerate(d['warmups'] + d['trials']):
        rows.append(analyze_trial(t, c, i, i < c['warmups']))
    return rows


def audit_artifacts(path, d, build_dir=None, reference_path=None):
    p = d['provenance']
    build_dir = Path(build_dir) if build_dir else Path(p['source_root']).parent
    build_path = build_dir / 'build.json'; build = strict_json(build_path)
    require(digest(build_path) == p['build_manifest_sha256'] and build['returncode'] == 0, 'build manifest mismatch')
    require(digest(build_dir/'metal-control-location') == p['binary_sha256'] == build['binary_sha256'], 'binary mismatch')
    required = ('Main.swift','Support.swift','Driver.swift','Kernels.metal','oracle.py','protocol.py','build.py')
    require(set(required) <= set(build['source_sha256']), 'required frozen source missing from build manifest')
    require(all('source_sha256:'+name in p for name in required), 'missing runtime source hashes')
    for name, sha in build['source_sha256'].items():
        require(digest(build_dir/'source'/name) == sha, 'frozen source changed: ' + name)
        if 'source_sha256:'+name in p:
            require(p['source_sha256:'+name] == sha, 'runtime source hash mismatch: ' + name)
    reference_path = Path(reference_path) if reference_path else Path(p['reference_file'])
    require(digest(reference_path) == d['reference_file_sha256'], 'CPU reference artifact mismatch')
    ref = strict_json(reference_path); verify_reference_document(ref)
    require(ref['config']['T'] == d['config']['T'] and ref['inputs'] == d['inputs'], 'reference config/input mismatch')
    run = strict_json(Path(path).parent/'run.json')
    require(run['returncode'] == 0 and run['timed_out'] is False, 'execution failed/timed out')
    require(digest(path) == run['files']['stdout.json']['sha256'], 'analyzed input is not recorded stdout')
    require(d == strict_json(path), 'analyzed document differs from input artifact')
    for name in ('stdout.json','stderr.log'):
        f = Path(path).parent/name
        require(digest(f) == run['files'][name]['sha256'] and f.stat().st_size == run['files'][name]['bytes'], 'raw execution output changed')
    argv = run['command']; require(Path(argv[0]).name == 'metal-control-location', 'wrong executable command')
    opts = {}; index = 1
    while index < len(argv):
        key = argv[index]; require(key not in opts, 'duplicate run option')
        if key == '--json':
            opts[key] = True; index += 1
        else:
            require(index+1 < len(argv), 'missing run option value')
            opts[key] = argv[index+1]; index += 2
    require(set(opts) <= {'--json','--reference','--mode','--rounds','--cancel-policy','--trials','--warmups','--timeout-seconds'}, 'unknown run options')
    c = d['config']
    expected = {'--mode': c['mode'], '--rounds': str(c['T']), '--cancel-policy': c['cancel_policy'],
                '--trials': str(c['trials']), '--warmups': str(c['warmups'])}
    defaults = {'--mode':'gpu-step','--rounds':'16','--cancel-policy':'none','--trials':'1','--warmups':'0','--timeout-seconds':'30'}
    for key, value in expected.items():
        require(opts.get(key, defaults[key]) == value, 'command config mismatch: ' + key)
    require(opts.get('--timeout-seconds', '30') == str(c['timeout_seconds']), 'timeout command mismatch')
    require('--reference' in opts, 'command has no reference')
    original_ref = Path(opts['--reference'])
    if not original_ref.is_absolute(): original_ref = Path(run['cwd'])/original_ref
    require(str(original_ref.resolve()) == p['reference_file'], 'recorded reference path mismatch')
    original_binary = Path(argv[0])
    if not original_binary.is_absolute(): original_binary = Path(run['cwd'])/original_binary
    require(str(original_binary.parent/'source') == p['source_root'], 'recorded source/binary path mismatch')
    require(p['has_unified_memory'] == 'true' and p['device_future_claimed'] == 'false', 'platform/scope claims')
    return {'passed': True, 'binary_sha256': p['binary_sha256'], 'build_manifest_sha256': p['build_manifest_sha256'],
            'reference_file_sha256': d['reference_file_sha256']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--build-dir', type=Path)
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    results = []
    for path in args.files:
        try:
            document = strict_json(path)
            rows = analyze_document(document)
            artifacts = audit_artifacts(path, document, args.build_dir, args.reference)
            results.append({'path': str(path), 'passed': True, 'trials': rows, 'artifacts': artifacts})
        except (ValueError, KeyError, TypeError, IndexError, OSError, OverflowError) as error:
            results.append({'path': str(path), 'passed': False, 'error': str(error)})
    report = {'schema_version': 1, 'passed': all(r['passed'] for r in results),
              'scope': 'full integer trajectories, event/command/metric replay and immutable build/run artifact binding; finite samples, no performance threshold',
              'checker_sha256': digest(__file__), 'math_sha256': digest(Path(__file__).with_name('control_math.py')), 'results': results}
    rendered = json.dumps(report, indent=2, allow_nan=False)+'\n'
    if args.output:
        with args.output.open('x') as f: f.write(rendered)
    print(rendered, end='')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
