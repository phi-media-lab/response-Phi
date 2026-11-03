#!/usr/bin/env python3
"""Read-only audit of the six bounded file-effect traces. Imports no experiment code."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import sys

CASES = ['E0', 'E1', 'E2', 'E3', 'R', 'F']
ACTION = {'tool': 'edit_file', 'path': 'app.py', 'old': '1', 'new': '2'}
READ = {'tool': 'read_file', 'path': 'app.py'}
BEFORE, AFTER = 'answer = 1\n', 'answer = 2\n'
REFERENCE = {
    'protocol': 'c9a7c15a8fd6d8e59d317253159405426381496a1ecbbea0be017bdbb7a9b464',
    'generation': '482316433b8976e08dc4b357e2c5fe296e3da09f465e89c017f413885f442a99',
    'checkpoint': 'ae814fd810c058b306d482a940638f1af72762f542f9de1c1c445cf871143401',
    'receipts': 'a267493f08aee67dcd43de5f707ed1ae772bad7fd7aecae25477d1a04cd23909',
    'workspace': 'ad26c64f7d11518f1fedcc171e49c8cd605926255a576fd752e0efb7c75d0153',
    'loop': 'f19347b610955b28d404e4d825d6aac755b36c47a9ed38abc4427d908edbb93a',
    'steering': '28a5e8dc4e65c0c01de1f1d909be945d26a7e8466eb824ed72707378f56a7c94',
}


class Invalid(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def file_fact(value, path, content):
    expected = {'path': str(path), 'exists': content is not None}
    if content is not None:
        expected.update(bytes=len(content.encode()), sha256=sha(content.encode()), utf8=content)
    require(value == expected, 'file fact bytes/hash/path mismatch: '+str(path))


def receipt_for(call):
    value = {'tool_call_id': call, 'tool': 'edit_file',
             'arguments': {'path': 'app.py', 'old': '1', 'new': '2'},
             'exit_state': 'ok', 'result': 'edited app.py', 'changed_artifacts': ['app.py']}
    # This is the persisted format's digest definition, not the upstream helper.
    return {'receipt_id': sha(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()), **value}


def expected_receipt_failure(traceback_text, receipt_path):
    # Only the two declared exceptions may occur. Merely finding the expected
    # cause somewhere in a longer chain could conceal a later unknown error.
    summaries = [line for line in traceback_text.splitlines() if line and not line[0].isspace()]
    return summaries == [
        'Traceback (most recent call last):',
        'FileNotFoundError: [Errno 2] No such file or directory: '+repr(str(receipt_path)),
        'The above exception was the direct cause of the following exception:',
        'Traceback (most recent call last):',
        '_tiny_intent_effects_reference.protocol.AgentError: could not append receipt log',
    ]


def expected_sequence(case):
    prefix = ['intent.initialized', 'case.started', 'tool.execute_entered', 'tool.execute_returned']
    end = ['tool.execute_entered', 'tool.execute_returned', 'case.finished']
    proposal = ['proposal.created', 'proposal.admitted', 'action.start_claimed']
    effect = ['tool.execute_entered', 'tool.approved', 'file.commit_entered', 'file.commit_returned',
              'receipt.append_attempted', 'receipt.recorded', 'tool.execute_returned']
    gate = ['gate.reached', 'intent.advanced', 'gate.release_requested', 'gate.release_observed']
    if case == 'E0':
        return prefix+['proposal.created', 'intent.advanced', 'proposal.rejected']+end
    if case == 'E1':
        return prefix+proposal[:2]+['intent.advanced', 'action.start_rejected']+end
    if case == 'E2':
        return prefix+proposal+effect[:3]+gate+effect[3:]+['observation.produced', 'worker.joined', 'observation.consumed']+end
    if case == 'E3':
        return prefix+proposal+effect+['observation.produced']+gate+['worker.joined', 'observation.consumed']+end
    if case == 'R':
        return prefix+effect+['checkpoint.created', 'intent.advanced', 'checkpoint.resume_started']+end[:2]+['checkpoint.resume_returned', 'case.finished']
    return prefix+proposal+effect[:5]+['receipt.append_failed', 'tool.execute_returned', 'observation.produced', 'observation.consumed']+end


def checkpoint_check(o, events, fixture):
    saved = o['checkpoint']
    require(saved == o['checkpoint_after'], 'checkpoint changed during resume')
    require(saved['task'] == fixture['initial_intent'], 'checkpoint task')
    messages, model = saved['messages'], saved['model']
    require(len(messages) == 6 and [m[0] for m in messages] == ['system', 'user', 'assistant', 'user', 'assistant', 'user'], 'checkpoint complete observation shape')
    require(messages[0][1].startswith('You are a coding agent.') and messages[1][1] == fixture['initial_intent'], 'checkpoint initial messages')
    require(json.loads(messages[2][1]) == READ and messages[3][1] == 'Tool result:\n'+BEFORE, 'checkpoint read observation')
    require(json.loads(messages[4][1]) == ACTION and messages[5][1] == 'Tool result:\nedited app.py', 'checkpoint edit observation')
    require(model == {'conversation_position': 6, 'response_index': 2,
                     'cached_token_ids': [len(m[1]) for m in messages], 'layer_offsets': [6, 6]}, 'fake checkpoint position/index')
    payload = {k: saved[k] for k in ('task', 'messages', 'model')}
    require(saved['checkpoint_id'] == sha(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()), 'checkpoint canonical digest')
    before = [[{'role': role, 'content': content} for role, content in messages[:n]] for n in (2, 4)]
    require(o['generator_calls_before'] == before, 'pre-checkpoint generator inputs')
    steered = [{'role': r, 'content': t} for r, t in messages]+[{'role': 'user', 'content': 'Operator steering:\n'+fixture['next_intent']}]
    require(o['generator_calls_after'] == [steered, steered+[
        {'role': 'assistant', 'content': canonical(READ)}, {'role': 'user', 'content': 'Tool result:\n'+AFTER}]], 'resume prefix/steering/read evidence')
    require(o['resumed_response_index'] == 4 and o['resumed_completed'] is True and o['resumed_reason'] == 'completed', 'resume did not complete prescribed script')
    require(o['receipt_before_resume'] == o['receipt_after_resume'] == o['receipt_log'], 'receipt changed on reopen/resume')
    by_name = {e['name']: e['details'] for e in events}
    require(by_name['checkpoint.created'] == {'checkpoint_id': saved['checkpoint_id'], 'response_index': 2, 'conversation_position': 6}, 'checkpoint event identity')
    require(by_name['checkpoint.resume_started'] == {'checkpoint_id': saved['checkpoint_id'],
        'api': 'upstream resume_with_steering; only complete observation boundary'}, 'resume wrong checkpoint/API')
    require(by_name['checkpoint.resume_returned'] == {'completed': True, 'reason': 'completed'}, 'resume result event')


def check_case(c, fixture, recorded_root):
    case, o, events = c['case'], c['observations'], c['events']
    require(case in CASES and c['validation_passed'] is True, 'unknown/failed case')
    require(c['execution_succeeded'] is (case != 'F') and c['expected_failure'] is (case == 'F'), 'expected failure disguised as execution success')
    require(c['errors'] == [] and c['thread_errors'] == [], 'unknown execution failure')
    require(c['checks'] and all(x['passed'] is True for x in c['checks']), 'driver reported a failed check')
    names = [e['name'] for e in events]
    require(names == expected_sequence(case), 'case event schedule/multiplicity: '+case)
    times = [e['t_ms'] for e in events]
    require(all(type(t) in (int, float) and math.isfinite(t) and t >= 0 for t in times)
            and all(a <= b for a, b in zip(times, times[1:])), 'non-monotonic/invalid host time')
    require([e['seq'] for e in events] == list(range(len(events))), 'event sequence identity/order')
    app = recorded_root/'effects'/case/'workspace/app.py'
    receipt_path = recorded_root/'effects'/case/('missing-parent' if case == 'F' else 'audit')/'receipts.jsonl'
    call = 'R-auto-1' if case == 'R' else case+'-call-0'
    expected_receipt = receipt_for(call)
    has_effect, has_receipt = case not in ('E0', 'E1'), case in ('E2', 'E3', 'R')
    text, epoch, proposal, ticket, obs = BEFORE, 0, None, None, None
    phase, pending, current_call, recorded_receipt = None, False, None, None
    count = {'workspace_calls': 0, 'effect_execute_calls': 0, 'commits': 0, 'approvals': 0, 'receipt_appends': 0}
    errors = []
    worker_created = case in ('E2', 'E3')
    worker_events = {'action.start_claimed', 'tool.approved', 'file.commit_entered', 'file.commit_returned',
                     'receipt.append_attempted', 'receipt.recorded', 'observation.produced', 'gate.reached', 'gate.release_observed'}
    for e in events:
        n, d = e['name'], e['details']
        worker_event = worker_created and (n in worker_events or n == 'tool.execute_entered' and d['action']['tool'] == 'edit_file'
                                          or n == 'tool.execute_returned' and d['tool'] == 'edit_file')
        require(e['thread'] == ('effect-worker' if worker_event else 'MainThread'), 'wrong event thread')
        if n == 'intent.initialized':
            require(d == {'epoch': 0}, 'initial epoch')
        elif n == 'case.started':
            require(d['case'] == case and d['current_intent'] == fixture['initial_intent'], 'initial case/intent')
            file_fact(d['initial_file'], app, BEFORE)
        elif n == 'intent.advanced':
            require(d == {'previous_epoch': epoch, 'epoch': epoch+1, 'instruction': fixture['next_intent']}, 'intent increment')
            epoch += 1
        elif n == 'proposal.created':
            proposal = {'proposal_id': 'P0', 'call_id': call, 'epoch': 0, 'canonical_action': canonical(ACTION),
                        'action_sha256': sha(canonical(ACTION).encode()), 'state': 'proposed'}
            require(d == proposal, 'proposal immutable action binding')
        elif n == 'proposal.rejected':
            require(proposal and epoch != proposal['epoch'], 'rejection must be stale')
            require(d == {'proposal_id': 'P0', 'reason': 'stale_epoch', 'proposal_epoch': 0, 'current_epoch': epoch}, 'stale rejection details')
            proposal['state'] = 'rejected_stale'
        elif n == 'proposal.admitted':
            require(proposal and proposal['state'] == 'proposed' and epoch == proposal['epoch'], 'admit stale or duplicate proposal')
            ticket = {'ticket_id': 'ticket-P0', 'proposal_id': 'P0', 'state': 'admitted'}
            require(d == {**ticket, 'epoch': epoch, 'call_id': call, 'action_sha256': proposal['action_sha256']}, 'admission binding')
            proposal['state'] = 'admitted'
        elif n == 'action.start_rejected':
            require(ticket and ticket['state'] == 'admitted' and epoch != proposal['epoch'], 'revocation needs stale unconsumed ticket')
            require(d == {'ticket_id': 'ticket-P0', 'reason': 'stale_epoch', 'proposal_epoch': 0, 'current_epoch': epoch}, 'start rejection binding')
            ticket['state'] = proposal['state'] = 'revoked_before_start'
        elif n == 'action.start_claimed':
            require(ticket and ticket['state'] == 'admitted' and epoch == proposal['epoch'] and not pending, 'unauthorized/duplicate start')
            require(d == {'ticket_id': 'ticket-P0', 'epoch': 0, 'current_epoch': epoch, 'call_id': call, 'action_sha256': proposal['action_sha256']}, 'start binding')
            ticket['state'] = proposal['state'] = 'started'
            pending = True
        elif n == 'tool.execute_entered':
            require(current_call is None, 'overlapping tool calls')
            action = d['action']
            require(action in (READ, ACTION), 'unexpected file/tool action')
            wid = 'resumed' if case == 'R' and epoch == 1 else 'initial'
            require(d['workspace_id'] == wid, 'workspace instance changed')
            current_call = dict(d)
            count['workspace_calls'] += 1
            if action == ACTION:
                require(case == 'R' or pending and proposal['state'] == 'started', 'effect without completion obligation')
                require(d['call_id'] == call, 'effect call identity')
                count['effect_execute_calls'] += 1
                phase = 'entered'
            else:
                require(not pending and d['call_id'] is None, 'read before outstanding effect accounted')
        elif n == 'tool.approved':
            require(current_call and current_call['action'] == ACTION and phase == 'entered', 'approval without effect')
            require(d['action'] == ACTION and d['workspace_id'] == 'initial', 'approved different action')
            count['approvals'] += 1
            phase = 'approved'
        elif n == 'file.commit_entered':
            require(phase == 'approved' and text == BEFORE and d['relative_path'] == 'app.py', 'commit before approval/wrong bytes')
            require(d['workspace_id'] == 'initial' and d['proposed_sha256'] == sha(AFTER.encode()), 'commit content binding')
            file_fact(d['before'], app, BEFORE)
            phase = 'commit_entered'
        elif n == 'file.commit_returned':
            require(phase == 'commit_entered' and d['workspace_id'] == 'initial', 'commit return without entry')
            file_fact(d['after'], app, AFTER)
            text, phase = AFTER, 'committed'
            count['commits'] += 1
        elif n == 'receipt.append_attempted':
            require(phase == 'committed' and d['receipt'] == expected_receipt and d['already_present'] is False, 'receipt not bound to committed call')
            require(d['workspace_id'] == 'initial' and d['path'] == str(receipt_path), 'receipt destination')
            phase = 'receipt_attempted'
        elif n == 'receipt.recorded':
            require(phase == 'receipt_attempted' and d['receipt'] == expected_receipt and d['appended'] is True, 'receipt append evidence')
            require(d['workspace_id'] == 'initial', 'receipt workspace')
            log = json.dumps(expected_receipt, sort_keys=True)+'\n'
            file_fact(d['log'], receipt_path, log)
            recorded_receipt, phase = expected_receipt, 'receipted'
            count['receipt_appends'] += 1
        elif n == 'receipt.append_failed':
            require(case == 'F' and phase == 'receipt_attempted', 'unexpected receipt error')
            require(d['type'] == 'AgentError' and d['message'] == 'could not append receipt log'
                    and expected_receipt_failure(d['traceback'], receipt_path), 'failure did not reach actual missing-parent append or contains an unknown exception')
            require(d['workspace_id'] == 'initial', 'failed receipt workspace')
            errors.append({k: d[k] for k in ('type', 'message', 'traceback')})
            phase = 'receipt_failed'
        elif n == 'tool.execute_returned':
            require(current_call is not None and d['workspace_id'] == current_call['workspace_id']
                    and d['call_id'] == current_call['call_id'] and d['tool'] == current_call['action']['tool'], 'tool return mismatch')
            if d['tool'] == 'read_file':
                require(d['result'] == text, 'read differs from current file state')
            else:
                require(phase == ('receipt_failed' if case == 'F' else 'receipted'), 'tool returned before effect accounted')
                require(d['result'] == ('error: could not append receipt log' if case == 'F' else 'edited app.py'), 'tool effect result')
                phase = 'returned'
            current_call = None
        elif n == 'observation.produced':
            require(pending and phase == 'returned', 'observation discharged a nonexistent/incomplete action')
            state = 'effect_committed_receipt_failed' if case == 'F' else 'completed'
            file_fact(d['file_state'], app, AFTER)
            obs = {'observation_id': 'observation-'+call, 'epoch': 0, 'call_id': call,
                   'result': 'error: could not append receipt log' if case == 'F' else 'edited app.py',
                   'receipt': recorded_receipt, 'file_state': d['file_state'], 'execution_succeeded': case != 'F',
                   'effect_state': state, 'consumed': False}
            require(d == obs, 'produced observation lost effect/source/failure')
            pending = False
            proposal['state'] = ticket['state'] = state
        elif n == 'observation.consumed':
            require(obs and not obs['consumed'], 'observation duplicate or before produced')
            classification = 'historical_effect' if epoch != obs['epoch'] else 'current_effect'
            require(d == {'observation_id': obs['observation_id'], 'source_epoch': 0, 'current_epoch': epoch,
                          'classification': classification, 'satisfies_current_intent': False}, 'observation adopted by wrong intent')
            obs.update(consumed=True, classification=classification, consumed_at_epoch=epoch, satisfies_current_intent=False)
        elif n.startswith('gate.'):
            require(d['gate'] == ('before-original-commit' if case == 'E2' else 'before-observation-consumption'), 'gate location')
            if n == 'gate.release_requested':
                require(d['cleanup'] is False, 'normal gate escaped through failure cleanup')
        elif n == 'worker.joined':
            require(d == {}, 'worker joined through an unexpected cleanup path')
        elif n == 'case.finished':
            require(d == {'execution_succeeded': case != 'F', 'validation_passed': True}, 'case terminal result')
    require(current_call is None and not pending, 'unresolved tool/completion obligation')
    expected_controller = {'epoch': epoch, 'pending': None, 'proposals': {'P0': proposal} if proposal else {},
                           'tickets': {'ticket-P0': ticket} if ticket else {},
                           'observations': {obs['observation_id']: obs} if obs else {}}
    require(c['controller'] == expected_controller, 'controller snapshot disagrees with event replay')
    require(c['counts'] == count and count == {'workspace_calls': 3 if has_effect else 2,
            'effect_execute_calls': int(has_effect), 'commits': int(has_effect), 'approvals': int(has_effect),
            'receipt_appends': int(has_receipt)}, 'actual effect/read counts')
    require(c['receipt_errors'] == errors, 'receipt error lost or invented')
    require(c['worker'] == {'created': worker_created, 'joined': True if worker_created else None, 'live': False}, 'worker not joined')
    require(o['tool_policy'] == {'root': str(app.parent), 'allowed_tools': ['edit_file', 'read_file'],
            'allowed_commands': [], 'allow_writes': True, 'fixed_path': 'app.py'}, 'tool authorization scope changed')
    file_fact(o['final_file'], app, text)
    log = json.dumps(expected_receipt, sort_keys=True)+'\n' if has_receipt else None
    file_fact(o['receipt_log'], receipt_path, log)
    receipts = [expected_receipt] if has_receipt else []
    require(o['receipts'] == receipts and o['receipt_store_snapshots'] == [
        {'workspace_id': w, 'receipts': receipts} for w in (['initial', 'resumed'] if case == 'R' else ['initial'])], 'receipt memory/reopen identity')
    require(o['current_intent_read'] == text and o['workspace_files'] == ['app.py'], 'final observation/workspace files')
    require(o['workspace_modified_files'] == ([['app.py'], []] if case == 'R' else [['app.py']] if has_effect else [[]]), 'modified file ledger')
    if case == 'R':
        checkpoint_check(o, events, fixture)
    else:
        require(o['initial_read'] == BEFORE, 'initial real read missing')
    if obs:
        require(o['observation_id'] == obs['observation_id'] and o['consumed'] == obs, 'observation copies disagree')
    if case == 'E1':
        require(o['start_allowed'] is False, 'revoked ticket started')
    return {'case': case, 'passed': True, 'events': len(events), 'counts': count,
            'execution_succeeded': case != 'F', 'final_utf8': text, 'receipt_present': has_receipt,
            'classification': obs['classification'] if obs else None}


def check_data(data):
    require(data['schema_version'] == 1 and data['experiment'] == 'tiny-intent-effects', 'unsupported raw schema')
    require(data['passed'] is True and data['all_executions_succeeded'] is False, 'top-level failure semantics')
    require(not any(data[k] for k in ('model_executed', 'gpu_executed', 'performance_claimed'))
            and data['excluded_modules_loaded'] == [] and 'error' not in data, 'scope/unknown error')
    f = data['fixture']
    require(f['cases'] == CASES and f['action'] == ACTION and f['initial_file'] == {'path': 'app.py', 'utf8': BEFORE}
            and f['expected_edited_utf8'] == AFTER and f['performance_claimed'] is False, 'fixture mismatch')
    require([c['case'] for c in data['cases']] == CASES, 'missing/duplicate cases')
    root = PurePosixPath(data['snapshot']['manifest']).parent
    return [check_case(c, f, root) for c in data['cases']]


def check_artifacts(attempt, data, raw_path):
    attempt = Path(attempt).resolve()
    def read(relative):
        p = attempt/relative
        require(not p.is_symlink() and p.resolve().is_relative_to(attempt) and p.is_file(), 'missing/escaped artifact '+str(relative))
        return p.read_bytes()
    manifest_bytes = read('manifest.json')
    manifest = json.loads(manifest_bytes)
    identity = sha(manifest_bytes)
    snap = data['snapshot']
    require(snap['sha256'] == identity == json.loads(read('prepared.json'))['manifest_sha256'], 'snapshot identity')
    require(manifest['schema_version'] == 1 and manifest['kind'] == 'tiny-intent-effects-attempt', 'manifest schema')
    require(snap['pre_execution_verified'] is True and snap['post_execution_verified'] is True
            and snap['reference_origin'] == manifest['reference_origin'], 'snapshot runtime binding')
    required = {'harness/'+n for n in ['run.py', 'protocol.py', 'loader.py', 'attempt.py', 'scenario.json', 'test_protocol.py', 'README.md']}
    required |= {'reference/'+n+'.py' for n in REFERENCE} | {'scripts/run_recorded.py', 'P7_EFFECTS_PLAN.md'}
    actual = {str(p.relative_to(attempt/'payload')) for p in (attempt/'payload').rglob('*') if p.is_file()}
    require(set(manifest['payload_files']) == actual == required, 'payload source set missing/extra')
    for name, row in manifest['payload_files'].items():
        value = read('payload/'+name)
        require(sha(value) == row['sha256'] and len(value) == row['bytes'], 'payload bytes/hash: '+name)
    require(json.loads(read('payload/harness/scenario.json')) == data['fixture'], 'fixture not from frozen source')
    recorded = PurePosixPath(snap['manifest']).parent
    require(set(snap['imported_modules']) == set(REFERENCE), 'reference import set')
    for name, expected in REFERENCE.items():
        require(snap['imported_modules'][name] == {'path': str(recorded/'payload/reference'/(name+'.py')), 'sha256': expected}
                and sha(read('payload/reference/'+name+'.py')) == expected, 'reference pin/import: '+name)
    run = json.loads(read('execution/run.json'))
    require(run['returncode'] == 0 and run['timed_out'] is False, 'recorded execution failed/timed out')
    require(Path(raw_path).resolve() == attempt/'execution/stdout.json', 'raw input not recorded stdout')
    require(run['command'] == [manifest['runtime']['executable'], str(recorded/'payload/harness/run.py'),
            '--attempt-manifest', str(recorded/'manifest.json'), '--snapshot-sha256', identity, '--json']
            and run['cwd'] == str(recorded/'payload/harness'), 'actual command not bound to frozen source')
    for name in ('stdout.json', 'stderr.log'):
        value = read('execution/'+name)
        require(run['files'][name] == {'bytes': len(value), 'sha256': sha(value)}, 'run output hash: '+name)
    require(read('execution/stderr.log') == b'', 'unexpected execution stderr')
    require(json.loads(read('execution/stdout.json')) == data, 'audited data differs from recorded stdout')
    require({p.name for p in (attempt/'effects').iterdir()} == set(CASES), 'effect case directories')
    for c in data['cases']:
        case, o = c['case'], c['observations']
        require(read('effects/'+case+'/workspace/app.py') == o['final_file']['utf8'].encode(), 'actual final file: '+case)
        require({p.name for p in (attempt/'effects'/case/'workspace').iterdir()} == {'app.py'}, 'unexpected workspace file: '+case)
        log = Path('effects')/case/('missing-parent' if case == 'F' else 'audit')/'receipts.jsonl'
        if o['receipt_log']['exists']:
            value = read(log)
            require(value == o['receipt_log']['utf8'].encode() and [json.loads(s) for s in value.splitlines()] == o['receipts'], 'actual receipt JSONL: '+case)
        else:
            require(not (attempt/log).exists(), 'unexpected receipt: '+case)
        if case == 'F':
            require(not (attempt/'effects/F/missing-parent').exists(), 'missing-parent failure condition no longer present')
    return {'passed': True, 'payload_files': len(required), 'manifest_sha256': identity,
            'raw_sha256': sha(read('execution/stdout.json')),
            'runtime_scope': 'Interpreter identity recorded; stdlib/OS transitive closure not archived or re-proved.'}


def analyze(attempt):
    raw = Path(attempt)/'execution/stdout.json'
    data = json.loads(raw.read_bytes())
    cases = check_data(data)
    artifacts = check_artifacts(attempt, data, raw)
    return {'schema_version': 1, 'passed': True, 'cases': cases, 'artifacts': artifacts,
            'checker_sha256': sha(Path(__file__).read_bytes()),
            'scope': 'Read-only finite trace replay + actual saved files/receipts/source binding; no experiment imports or execution, no liveness/performance/crash guarantee.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--attempt', type=Path, required=True)
    args = p.parse_args()
    try:
        result = analyze(args.attempt)
    except (Invalid, KeyError, ValueError, TypeError, OSError, IndexError) as error:
        result = {'passed': False, 'error': type(error).__name__+': '+str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
