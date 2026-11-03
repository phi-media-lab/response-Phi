#!/usr/bin/env python3
"""Independent A0-A4 CPU evidence audit; no author driver/oracle/ledger imports.

The protocol audit consumes saved real traces. Artifact verification binds those
traces to the frozen source, exact compiler command and produced executable.
It neither runs Swift nor treats a log event alone as proof of a language throw.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import re

CASES = ('direct-await-normal', 'registered-child-cleanup-fault',
         'body-and-two-cleanup-faults', 'cancel-parent-failed-sibling',
         'propagated-cleanup-fault')
SWIFT = ('Support.swift', 'AccessOwner.swift', 'NestedScope.swift', 'Cases.swift', 'Main.swift')
SOURCE = SWIFT + ('driver.py', 'test_driver.py', 'fixtures.json', 'README.md', 'DESIGN.md',
                  'test-fixtures/direct-await-normal-attempt-1.json', 'test-fixtures/provenance.json')
DERIVED = {
    'c2/Support.swift': '721c2300015abf074fce04bffb6d959ce53575f5af9e2b37ad4125d94ccd84d7',
    'c2/LiveOwner.swift': 'afabcf1616e3c4d4694b4cf69bf3b77a4e40ffba0b7bd765a049a1047f4f63ad',
    'c2/Cases.swift': '5fcfe1bea7906323875c3dca9f1e37917583d1514162d29b14a6e78facbe0e4e',
    'c3/Protocol.swift': '588b86853880e2ef3441ed90ebcafff30bfae1ad0c68cd2a341684bb17cfa1ac',
    'c3/NestedScope.swift': 'c1cb2807e5d5a3c914dbb7d1ed5ef281e467bfc2991c088d4edb6b1593974931',
    'c3/Cases.swift': '8572dab10e737afd1d3c2fa1be2ce4e1dbe03fb4bd18d5887b52a3cc71800a54',
}
# Previously archived compiler identity on this machine. A changed toolchain
# requires explicit review, not learning an expected value from the new report.
COMPILER = {'sha256': '2ed38571e92c0283091838c1649e27650ad9c99950288e883c7b2dc6c4ce89fb',
            'bytes': 171036592}
SEEDS = {'X': 0x13579bdf, 'Y': 0x2468ace0, 'Z': 0xabcdef01, 'Y/B': 0x13579bdf}
IDENTITY_KEYS = {'invocation_id', 'scope_id', 'owner_id', 'allocation_id', 'epoch', 'generation'}
EVENT_NAMES = {
    'case.started', 'case.completed', 'case.checkpoint', 'case.direct_await_started',
    'case.direct_await_returned', 'case.child_handle_dropped', 'case.cancel_requested',
    'case.cancel_accepted', 'case.parent_task_join_returned', 'owner.permit_granted',
    'owner.permit_returned', 'owner.claim_rejected', 'owner.metadata_observed',
    'scope.admitted', 'scope.body_started', 'scope.body_error', 'scope.body_returned',
    'scope.admission_closed', 'scope.drained', 'scope.run_returned', 'producer.registered',
    'producer.started', 'producer.access_started', 'producer.write_ended',
    'producer.read_pass_started', 'producer.read_pass_returned', 'producer.access_ended',
    'producer.ready', 'producer.join_returned', 'cleanup.registered', 'cleanup.disposition',
    'cleanup.started', 'cleanup.error_caught', 'cleanup.task_returned', 'cleanup.join_started',
    'cleanup.join_returned', 'read.access_started', 'read.pass_started', 'read.pass_returned',
    'read.access_ended', 'local.start_gate_opening', 'child.registered', 'child.start_gate_opening',
    'child.started', 'child.run_returned', 'child.join_started', 'child.join_returned',
    'child.handle_await_started', 'child.handle_await_returned', 'fault.raised', 'fault.observed',
    'gate.waiter_registered', 'gate.open_requested', 'gate.resume_issued', 'gate.await_returned',
    'runtime.snapshot',
}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    return json.loads(Path(path).read_text())


def identity(path):
    data = Path(path).read_bytes()
    return {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


def one(events, name):
    values = [e for e in events if e['name'] == name]
    need(len(values) == 1, 'exactly one event ' + name)
    return values[0]


def before(*events):
    seqs = [e['seq'] for e in events]
    need(all(a < b for a, b in zip(seqs, seqs[1:])), 'strict causal order ' + ','.join(e['name'] for e in events))


def details(event, expected):
    need(event['details'] == expected, 'event details ' + event['name'])


def flag(value):
    return 'true' if value else 'false'


def csv(values):
    return ','.join(values)


def typename(value, basename):
    need(isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*\.' + re.escape(basename), value),
         'actual qualified Swift type ' + basename)


def specification(name):
    need(name in CASES, 'unknown case')
    multi = name in CASES[2:4]
    scopes = ['X', 'Y'] + (['Z'] if multi else []) + ['Y/B']
    child_scopes = [] if name == CASES[0] else ['Y'] + (['Z'] if multi else [])
    failed_cleanup = set() if name == CASES[0] else {'Y'}
    body_faults = set()
    if name == CASES[2]:
        failed_cleanup.add('Z')
        body_faults = {'X', 'Y'}
    failed_body = body_faults | ({'X'} if name == CASES[4] else set())
    failed_runs = (set() if name == CASES[0] else {'X', 'Y'}) | ({'Z'} if name == CASES[2] else set())
    gated = (set() if name == CASES[0] else {'Y'}) | ({'Z'} if multi else set())
    return dict(scopes=scopes, children=child_scopes, cleanup_faults=failed_cleanup,
                body_faults=body_faults, failed_body=failed_body, failed_runs=failed_runs,
                gated=gated, multi=multi)


def expected_scan(seed, passes):
    # Independently exponentiate affine maps modulo 2^32. This differs from
    # both the Swift recurrence loop and the author's unbounded-power formula.
    mask32 = (1 << 32) - 1
    multiplier, addition = 1, 0
    base_multiplier, base_addition, exponent = 1664525, 1013904223, 31
    while exponent:
        if exponent & 1:
            multiplier, addition = ((base_multiplier * multiplier) & mask32,
                                    (base_multiplier * addition + base_addition) & mask32)
        base_multiplier, base_addition = ((base_multiplier * base_multiplier) & mask32,
                                         (base_multiplier * base_addition + base_addition) & mask32)
        exponent >>= 1
    values = [(multiplier * (index ^ seed) + addition) & mask32 for index in range(128)]
    mask64, checksum, samples = (1 << 64) - 1, 0x9e3779b97f4a7c15, []
    for p in range(passes):
        for index, value in enumerate(values):
            checksum = (((checksum & ((1 << 57) - 1)) << 7) | (checksum >> 57)) ^ value
            checksum = (checksum + index + p) & mask64
        samples.append(dict(pass_index=p, actual_values=values[:], checksum=checksum,
                            visits=(p + 1) * 128, mismatches=0))
    return dict(checksum=checksum, visits=128 * passes, mismatches=0, passes=samples)


def audit_case(raw):
    need(raw['schema_version'] == 1 and raw['experiment'] == 'swift-nested-access', 'raw schema identity')
    need(raw['completed'] is True and raw['validation_passed'] is True and raw['errors'] == [], 'raw collection failure')
    need(raw['ledger_tests'] == [] and raw['incomplete_events'] == [] and raw['incomplete_pass_samples'] == [],
         'no invented unknown ledger or incomplete trace')
    need(len(raw['cases']) == 1, 'one real case per process')
    case = raw['cases'][0]
    name = case['case_id']
    spec = specification(name)
    need(case['validation_passed'] is True and all(c['passed'] is True for c in case['checks']), 'original self-check failure')
    need([c['name'] for c in case['checks']] == ['original_execution_status_preserved',
         'all_original_fault_identities_preserved', 'actual_partial_read_failure', 'safe_complete_drain',
         'real_full_array_evidence', 'same_owner_next_epoch_success', 'busy_probe_zero_admission'],
         'complete original self-check inventory')
    events = case['events']
    need([e['seq'] for e in events] == list(range(len(events))), 'continuous event sequence')
    need(all(type(e['t_ns']) is int and e['t_ns'] >= 0 and type(e['cancelled']) is bool for e in events), 'actual clock/cancellation samples')
    need(all(a['t_ns'] <= b['t_ns'] for a, b in zip(events, events[1:])), 'monotonic event clock')
    need(all(e['name'] in EVENT_NAMES for e in events), 'unexpected event/error/unknown transition')
    need(all(isinstance(e['details'], dict) and all(isinstance(k, str) and isinstance(v, str)
             for k, v in e['details'].items()) for e in events), 'event details are exact string fields')
    details(one(events, 'case.started'), {'case_id': name})
    finished = one(events, 'case.completed')
    details(finished, {'case_id': name, 'validation_passed': 'true', 'original_execution_succeeded': flag(name == CASES[0])})
    need(finished == events[-1], 'complete final case event')

    owners = case['owners']
    records = [r for o in owners for r in o['records']]
    by_scope = {r['identity']['scope_id']: r for r in records}
    need(len(by_scope) == len(records) and set(by_scope) == set(spec['scopes']), 'exact admitted run set')
    ids = {s: r['identity'] for s, r in by_scope.items()}
    by_inv = {i['invocation_id']: i for i in ids.values()}
    need(len(by_inv) == len(ids), 'unique admitted invocation identities')
    for scope, ident in ids.items():
        need(set(ident) == IDENTITY_KEYS and all(isinstance(ident[k], str) and ident[k]
             for k in IDENTITY_KEYS - {'epoch', 'generation'}), 'complete run identity')
        epoch = 2 if scope == 'Y/B' else 1
        need(type(ident['epoch']) is int and ident['epoch'] == epoch and ident['generation'] == epoch, 'actual owner epoch/generation')
        need(re.fullmatch(r'CPU-allocation-[1-9][0-9]*', ident['allocation_id']), 'private allocation identity')
    need(ids['Y']['owner_id'] == ids['Y/B']['owner_id'] and ids['Y']['allocation_id'] == ids['Y/B']['allocation_id'], 'same Y allocation across actual epochs')
    principal = ['X', 'Y'] + (['Z'] if spec['multi'] else [])
    need(len({ids[s]['owner_id'] for s in principal}) == len(principal)
         and len({ids[s]['allocation_id'] for s in principal}) == len(principal), 'independent owner allocations in track A')
    need([o['owner_id'] for o in owners] == [ids[s]['owner_id'] for s in principal], 'complete owner inventory/order')
    for event in events:
        ident = event.get('identity')
        if ident is not None:
            need(ident == by_inv.get(ident['invocation_id']), 'event original identity routed to actual admitted run')
        else:
            need(event['name'] in {'case.started', 'case.completed', 'case.checkpoint', 'case.cancel_requested',
                 'case.cancel_accepted', 'case.parent_task_join_returned', 'gate.open_requested',
                 'owner.claim_rejected', 'owner.metadata_observed'}, 'runtime event cannot lose original invocation')
    local = {s: [e for e in events if e.get('identity') == ident] for s, ident in ids.items()}
    end = {}
    expected_samples = {}
    access_ids = set()
    for scope, record in by_scope.items():
        ident, ev = ids[scope], local[scope]
        work = record['work']
        need(work == dict(request=scope if scope == 'Y/B' else scope + '/A', seed=SEEDS[scope],
                          elements=128, iterations=31, initialization_passes=2, cleanup_passes=3), 'independent fixed work fixture')
        producer, cleanup = record['producer'], record['cleanup']
        need(producer == dict(ready=True, access_ended=True, evidence=expected_scan(work['seed'], 2), faults=[]), 'producer full 128-element passes/counters')
        count = 2 if scope in spec['cleanup_faults'] else 3
        expected = expected_scan(work['seed'], count)
        need(cleanup['evidence'] == expected, 'cleanup complete actual arrays and cumulative counters')
        need(cleanup['successful_result'] == expected if scope not in spec['cleanup_faults'] else cleanup.get('successful_result') is None,
             'failed partial cleanup cannot publish successful result')
        need(cleanup['work_status'] == ('failed' if scope in spec['cleanup_faults'] else 'success')
             and cleanup['access_status'] == 'ended' and cleanup['completion_proof'] == 'all_synchronous_calls_returned'
             and cleanup['not_executed_passes'] == list(range(count, 3)), 'separate work failure and real access completion')
        need(cleanup['obligation_id'] == ident['invocation_id'] + '/required-cleanup', 'original mandatory cleanup identity')
        access = cleanup['access_id']
        need(isinstance(access, str) and access and access not in access_ids, 'unique cleanup access identity')
        access_ids.add(access)
        need(record['body_succeeded'] is (scope not in spec['failed_body']) and record['execution_succeeded'] is (scope not in spec['failed_runs']), 'original business failure/status preserved')
        need(record['local_accesses_ended'] is True and record['fully_drained'] is True and record['owner_safely_returned'] is True
             and record['unresolved'] == [] and record['rejections'] == [], 'actual complete drain, no hidden rejection/unknown')
        values = {'X': 17, 'Y': 23, 'Z': 31, 'Y/B': 41}
        need(record.get('value') == (None if scope in spec['failed_body'] else values[scope]), 'body return value is not successful scope outcome')
        grant, admitted = one(ev, 'owner.permit_granted'), one(ev, 'scope.admitted')
        pr, cr, opened = one(ev, 'producer.registered'), one(ev, 'cleanup.registered'), one(ev, 'local.start_gate_opening')
        before(grant, admitted, pr, cr, opened, one(ev, 'producer.started'), one(ev, 'producer.access_started'), one(ev, 'producer.write_ended'))
        details(pr, {'request': work['request'], 'task_retained': 'true'})
        details(cr, {'obligation_id': cleanup['obligation_id'], 'access_id': access, 'requested_passes': '3',
                     'task_retained': 'true', 'mandatory': 'true'})
        initialized = one(ev, 'producer.access_ended')
        details(initialized, {'proof': 'all_synchronous_calls_returned'})
        before(initialized, one(ev, 'producer.ready'), one(ev, 'producer.join_returned'), one(ev, 'scope.body_started'), one(ev, 'scope.body_returned'))
        details(one(ev, 'producer.join_returned'), {'ready': 'true', 'access_ended': 'true'})
        body_returned, closed = one(ev, 'scope.body_returned'), one(ev, 'scope.admission_closed')
        details(body_returned, {'succeeded': flag(record['body_succeeded'])})
        before(body_returned, closed, one(ev, 'cleanup.disposition'), one(ev, 'cleanup.started'), one(ev, 'read.access_started'))
        details(one(ev, 'cleanup.disposition'), {'access_id': access, 'disposition': 'run', 'producer_ready': 'true', 'producer_access_ended': 'true'})
        details(one(ev, 'cleanup.started'), {'obligation_id': cleanup['obligation_id'], 'access_id': access})
        details(one(ev, 'read.access_started'), {'access_id': access})
        for prefix, role, sample_access, scan, preceding, following in (
            ('producer.read_pass_', 'initialization', ident['invocation_id'] + '/producer-read', producer['evidence'], one(ev, 'producer.write_ended'), initialized),
            ('read.pass_', 'cleanup', access, expected, one(ev, 'read.access_started'), one(ev, 'read.access_ended')),
        ):
            starts = [e for e in ev if e['name'] == prefix + 'started']
            returns = [e for e in ev if e['name'] == prefix + 'returned']
            need(len(starts) == len(returns) == len(scan['passes']), 'exact real pass start/return count')
            previous = preceding
            for index, (started, returned, sample) in enumerate(zip(starts, returns, scan['passes'])):
                before(previous, started, returned, following)
                details(started, dict(pass_index=str(index), **({'access_id': access} if role == 'cleanup' else {})))
                details(returned, {'access_id': sample_access, 'pass_index': str(index), 'visits': str(sample['visits']),
                                   'checksum': str(sample['checksum']), 'mismatches': '0'})
                expected_samples[returned['seq']] = dict(event_seq=returned['seq'], identity=ident,
                                                        access_id=sample_access, role=role, evidence=sample)
                previous = returned
        ended, task_returned, joined = one(ev, 'read.access_ended'), one(ev, 'cleanup.task_returned'), one(ev, 'cleanup.join_returned')
        drained, returned = one(ev, 'scope.drained'), one(ev, 'owner.permit_returned')
        before(ended, task_returned, joined, drained, returned, one(ev, 'scope.run_returned'))
        before(closed, one(ev, 'cleanup.join_started'), joined)
        details(ended, {'access_id': access, 'proof': 'all_synchronous_calls_returned',
                        'work_status': cleanup['work_status'], 'completed_passes': str(count)})
        details(task_returned, {'access_id': access, 'status': 'ended', 'fault_ids': csv(f['fault_id'] for f in cleanup['faults'])})
        details(joined, {'access_id': access, 'access_status': 'ended'})
        details(one(ev, 'cleanup.join_started'), {'access_id': access})
        details(returned, {'cleanup_access_id': access})
        details(one(ev, 'scope.run_returned'), {'execution_succeeded': flag(record['execution_succeeded']),
                    'fully_drained': 'true', 'owner_returned': 'true', 'fault_ids': csv(f['fault_id'] for f in record['faults'])})
        end[scope] = dict(close=closed, drained=drained, permit=returned, returned=one(ev, 'scope.run_returned'))

    need(case['pass_samples'] == [expected_samples[k] for k in sorted(expected_samples)], 'every actual sample tied to its exact pass event')
    # Independent replay of full child identities, not count equality.
    for scope, record in by_scope.items():
        child_scopes = spec['children'] if scope == 'X' else []
        child_ids = ['child-' + s for s in child_scopes]
        need(record['frozen_child_ids'] == record['joined_child_ids'] == child_ids, 'exact frozen/joined child identities')
        need(len(record['children']) == len(child_ids) and len(record['child_outcomes']) == len(child_ids), 'all actual child results retained')
        ev = local[scope]
        need(all(e['details'].get('child_id') in child_ids for e in ev
                 if e['name'].startswith('child.') and not e['name'].startswith('child.handle_')),
             'no orphan child lifecycle event')
        need([e['details']['child_id'] for e in ev if e['name'] == 'child.registered'] == child_ids, 'all child registrations accounted')
        details(end[scope]['close'], {'frozen_child_ids': csv(child_ids), 'frozen_cleanup_ids': record['cleanup']['obligation_id'],
                                     'frozen_access_ids': record['cleanup']['access_id']})
        details(end[scope]['drained'], {'frozen_child_ids': csv(child_ids), 'joined_child_ids': csv(child_ids),
                                       'unresolved': '', 'fault_ids': csv(f['fault_id'] for f in record['faults'])})
        for row, outcome, child, cid in zip(record['children'], record['child_outcomes'], child_scopes, child_ids):
            ident = ids[child]
            expected_row = dict(child_id=cid, invocation_id=ident['invocation_id'], scope_id=child,
                                owner_id=ident['owner_id'], launch_mode='detached',
                                phase='failedSafe' if child in spec['failed_runs'] else 'succeeded', joined=True, fully_drained=True)
            need(row == expected_row and outcome.get('rejection') is None and outcome['record'] == by_scope[child], 'child original invocation/owner/failure outcome')
            child_ev = [e for e in ev if e['details'].get('child_id') == cid and e['name'].startswith('child.')]
            registered, opened, started = [one(child_ev, n) for n in ('child.registered', 'child.start_gate_opening', 'child.started')]
            child_returned, join_started, joined = [one(child_ev, n) for n in ('child.run_returned', 'child.join_started', 'child.join_returned')]
            before(registered, opened, started, one(local[child], 'owner.permit_granted'))
            before(registered, end[scope]['close'], join_started, joined, end[scope]['drained'])
            before(end[child]['returned'], child_returned, joined)
            details(registered, dict(child_id=cid, child_invocation=ident['invocation_id'], child_scope=child,
                                    child_owner=ident['owner_id'], launch_mode='detached', task_retained='true', owner_retained='true'))
            for event in (opened, started):
                details(event, {'child_id': cid, 'child_invocation': ident['invocation_id']})
            details(child_returned, {'child_id': cid, 'child_invocation': ident['invocation_id'], 'fully_drained': 'true', 'admitted': 'true'})
            details(join_started, {'child_id': cid})
            details(joined, {'child_id': cid, 'child_invocation': ident['invocation_id'], 'fully_drained': 'true'})

    # Closed continuation registration and actual resume/return, not a bare await.
    gate_events = [e for e in events if e['name'].startswith('gate.')]
    need(len(gate_events) == 4 * len(spec['gated']), 'exact real gate lifecycle count')
    gate_bounds = {}
    for scope in spec['gated']:
        gate_id = scope + '/after-pass-1'
        ev = [e for e in gate_events if e['details']['gate_id'] == gate_id]
        registered, opened, resumed, returned = [one(ev, n) for n in
            ('gate.waiter_registered', 'gate.open_requested', 'gate.resume_issued', 'gate.await_returned')]
        before(registered, opened, resumed, returned)
        waiter = registered['details']['waiter_id']
        need(isinstance(waiter, str) and waiter, 'actual continuation waiter identity')
        details(registered, {'gate_id': gate_id, 'waiter_id': waiter, 'gate_open': 'false'})
        details(opened, {'gate_id': gate_id, 'waiters': '1'})
        for event in (resumed, returned):
            details(event, {'gate_id': gate_id, 'waiter_id': waiter})
        need(opened.get('identity') is None and all(e.get('identity') == ids[scope] for e in (registered, resumed, returned)), 'gate controller and cleanup identities')
        passes = [e for e in local[scope] if e['name'] == 'read.pass_returned']
        starts = [e for e in local[scope] if e['name'] == 'read.pass_started']
        before(passes[0], registered, returned, starts[1])
        gate_bounds[scope] = (registered, opened, resumed, returned)

    expected_fault_origins = {(s, 'read_cleanup') for s in spec['cleanup_faults']} | {(s, 'body') for s in spec['body_faults']}
    raised = [e for e in events if e['name'] == 'fault.raised']
    need(len(raised) == len(expected_fault_origins) and {(e['identity']['scope_id'], e['details']['origin']) for e in raised} == expected_fault_origins, 'all original faults, no same-name merge or fabricated parent fault')
    faults = {}
    for event in raised:
        d, ident = event['details'], event['identity']
        scope, origin, fid = ident['scope_id'], d['origin'], d['fault_id']
        need(isinstance(fid, str) and fid and fid not in faults, 'unique actual fault ID')
        cleanup = by_scope[scope]['cleanup']
        is_read = origin == 'read_cleanup'
        expected_kind = 'injectedAfterSecondPass' if is_read else 'injectedBody'
        need(d['kind'] == d['cause_text'] == expected_kind, 'exact original error kind/text')
        typename(d['cause_type'], 'ReadFailure' if is_read else 'InjectedBodyFault')
        typename(d['actual_thrown_type'], 'RaisedReadFault' if is_read else 'InjectedBodyFault')
        need(d['cause_type'].rsplit('.', 1)[0] == d['actual_thrown_type'].rsplit('.', 1)[0], 'same actual executable error module')
        need(d['obligation_id'] == (cleanup['obligation_id'] if is_read else ident['invocation_id'] + '/body')
             and d['access_id'] == (cleanup['access_id'] if is_read else 'none'), 'fault exact obligation/resource access')
        base = dict(fault_id=fid, identity=ident, obligation_id=d['obligation_id'], origin=origin,
                    kind=d['kind'], cause_type=d['cause_type'], cause_text=d['cause_text'], actual_thrown_type=d['actual_thrown_type'])
        if is_read:
            base['access_id'] = d['access_id']
        faults[fid] = (base, event)
        if is_read:
            ev = local[scope]
            caught = one(ev, 'cleanup.error_caught')
            details(caught, {'access_id': cleanup['access_id'], 'fault_ids': fid, 'actual_caught_type': d['actual_thrown_type']})
            before([e for e in ev if e['name'] == 'read.pass_returned'][1], event, caught, one(ev, 'read.access_ended'))
    observations = [e for e in events if e['name'] == 'fault.observed']
    for event in observations:
        d, observer = event['details'], event.get('identity')
        need(observer is not None and d['fault_id'] in faults, 'actual original fault observer')
        base, raised_event = faults[d['fault_id']]
        need(d['origin_invocation'] == base['identity']['invocation_id'] and d['obligation_id'] == base['obligation_id']
             and d['access_id'] == base.get('access_id', 'none'), 'fault propagation cannot rewrite source/access')
        need(d['channel'].startswith(observer['invocation_id'] + '/'), 'observation belongs to actual observer invocation')
        before(raised_event, event)

    def check_fault_list(actual, wanted_ids, cutoff, only_raised=False):
        need(len(actual) == len(wanted_ids) and {f['fault_id'] for f in actual} == set(wanted_ids), 'complete distinct fault set')
        for value in actual:
            base, _ = faults[value['fault_id']]
            need({k: v for k, v in value.items() if k != 'reported_via'} == base, 'unchanged original fault identity/type')
            channels = {base['identity']['invocation_id'] + '/raised'}
            if not only_raised:
                channels.update(e['details']['channel'] for e in observations if e['details']['fault_id'] == value['fault_id'] and e['seq'] < cutoff)
            need(len(value['reported_via']) == len(channels) and set(value['reported_via']) == channels, 'all actual fault channels preserved exactly')

    for scope, record in by_scope.items():
        descendants = {scope} | (set(spec['children']) if scope == 'X' else set())
        wanted = [fid for fid, (base, _) in faults.items() if base['identity']['scope_id'] in descendants]
        local_read = [fid for fid, (base, _) in faults.items() if base['identity']['scope_id'] == scope and base['origin'] == 'read_cleanup']
        check_fault_list(record['faults'], wanted, end[scope]['drained']['seq'])
        check_fault_list(record['cleanup']['faults'], local_read, 0, only_raised=True)
        body_errors = [e for e in local[scope] if e['name'] == 'scope.body_error']
        need(len(body_errors) == int(scope in spec['failed_body']), 'actual body error event count')
        if body_errors:
            propagated = name == CASES[4] and scope == 'X'
            original = [fid for fid, (base, _) in faults.items() if (base['identity']['scope_id'], base['origin']) == (('Y', 'read_cleanup') if propagated else (scope, 'body'))]
            details(body_errors[0], {'propagated': flag(propagated), 'fault_ids': csv(original)})
            before(one(local[scope], 'scope.body_started'), body_errors[0], one(local[scope], 'scope.body_returned'))
        need(len([e for e in local[scope] if e['name'] == 'cleanup.error_caught']) == int(scope in spec['cleanup_faults']), 'no extra swallowed cleanup error')
    # Require each intended observation channel, not just consistency with a
    # possibly truncated list of observation events.
    for fid, (base, _) in faults.items():
        origin = base['identity']['scope_id']
        channels = []
        if base['origin'] == 'read_cleanup':
            channels.append(ids[origin]['invocation_id'] + '/local.cleanup.drain')
        if origin in spec['children']:
            channels.append(ids['X']['invocation_id'] + '/child.drain/child-' + origin)
        if name == CASES[4]:
            channels += [ids['X']['invocation_id'] + '/child.handle.value', ids['X']['invocation_id'] + '/body.catch']
        actual = [e['details']['channel'] for e in observations if e['details']['fault_id'] == fid]
        need(len(actual) == len(channels) and set(actual) == set(channels), 'required independent propagation channels')

    need(case['parent_outcome'].get('rejection') is None and case['parent_outcome']['record'] == by_scope['X'], 'parent outcome bound to original run')
    need(case['reuse_outcome'].get('rejection') is None and case['reuse_outcome']['record'] == by_scope['Y/B'], 'reuse outcome bound to next real epoch')
    before(end['Y']['permit'], one(local['Y/B'], 'owner.permit_granted'))
    _audit_case_observations(case, spec, events, local, ids, by_scope, end, gate_bounds)
    return dict(passed=True, case_name=name, original_execution_succeeded=by_scope['X']['execution_succeeded'],
                admitted_runs=len(records), registered_children=len(spec['children']),
                actual_full_pass_arrays=len(expected_samples), actual_element_reads=len(expected_samples) * 128,
                original_fault_count=len(faults), failed_cleanup_scopes=sorted(spec['cleanup_faults']),
                faults_preserved=by_scope['X']['faults'], real_unknown_completion_tested=False,
                parent_live_lease_tested=False, performance_claim=False)


def _audit_case_observations(case, spec, events, local, ids, records, end, gate_bounds):
    name = case['case_id']
    parent_join = one(events, 'case.parent_task_join_returned')
    details(parent_join, {'case_id': name, 'fully_drained': 'true'})
    before(end['X']['returned'], parent_join)
    fault_case = name != CASES[0]
    busy = case.get('busy_probe')
    rejected = [e for e in events if e['name'] == 'owner.claim_rejected']
    if fault_case:
        need(busy is not None and busy.get('record') is None and busy.get('rejection') is not None, 'busy is zero admission, not unknown completion')
        r = busy['rejection']
        need(r['classification'] == 'rejectedBeforeAdmission' and r['scope_id'] == 'rejected-B'
             and r['owner_id'] == ids['Y']['owner_id'] and r['error_text'] == 'busy', 'precise busy probe')
        typename(r['error_type'], 'ProtocolFailure')
        need(r['invocation_id'] not in {i['invocation_id'] for i in ids.values()}, 'rejected run has no admitted epoch')
        e = one(rejected, 'owner.claim_rejected')
        need(e.get('identity') is None, 'busy probe has no invented run identity')
        details(e, {'rejection_id': r['rejection_id'], 'invocation_id': r['invocation_id'], 'scope_id': 'rejected-B',
                    'owner_id': ids['Y']['owner_id'], 'kind': 'busy', 'current_epoch': '1', 'current_phase': 'leased', 'admitted': 'false'})
        before(gate_bounds['Y'][0], e, gate_bounds['Y'][1])
    else:
        need(busy is None and rejected == [], 'normal case has no busy experiment')
    for owner in case['owners']:
        label = next(s for s in ('X', 'Y', 'Z') if s in ids and ids[s]['owner_id'] == owner['owner_id'])
        expected = [records[label]] + ([records['Y/B']] if label == 'Y' else [])
        need(owner['records'] == expected and owner['phase'] == 'idle' and owner.get('active') is None
             and owner['epoch'] == (2 if label == 'Y' else 1) and owner['allocation_id'] == ids[label]['allocation_id'], 'complete final owner history and storage')
        need(owner['rejections'] == ([busy['rejection']] if fault_case and label == 'Y' else []), 'actual rejected-run history')

    # Replay metadata and active-runtime snapshots at their distinct marked
    # event indices. No global atomicity is inferred across different owners.
    snapshots = list(case['owners']) + [o for p in case['checkpoints'] for o in p['owners']]
    metadata_indices = [o['metadata_event_seq'] for o in snapshots]
    runtime_indices = [o['active']['event_seq'] for o in snapshots if o.get('active') is not None]
    need(sorted(metadata_indices) == [e['seq'] for e in events if e['name'] == 'owner.metadata_observed']
         and sorted(runtime_indices) == [e['seq'] for e in events if e['name'] == 'runtime.snapshot'],
         'all marked snapshots retained exactly once')
    known_by_owner = {o['owner_id']: o for o in case['owners']}
    for owner in snapshots:
        seq = owner['metadata_event_seq']
        marker = events[seq]
        need(marker['name'] == 'owner.metadata_observed', 'exact owner metadata observation index')
        oid = owner['owner_id']
        need(oid in known_by_owner and owner['allocation_id'] == known_by_owner[oid]['allocation_id'], 'snapshot actual allocation')
        admitted = [r for r in known_by_owner[oid]['records'] if one(local[r['identity']['scope_id']], 'owner.permit_granted')['seq'] < seq]
        returned = [r for r in admitted if end[r['identity']['scope_id']]['permit']['seq'] < seq]
        phase = 'idle' if len(admitted) == len(returned) else 'leased'
        epoch = len(admitted)
        details(marker, {'owner_id': oid, 'phase': phase, 'epoch': str(epoch)})
        need(owner['phase'] == phase and owner['epoch'] == epoch and owner['records'] == returned, 'metadata replay from actual grant/return history')
        expected_rejected = [busy['rejection']] if fault_case and oid == ids['Y']['owner_id'] and rejected[0]['seq'] < seq else []
        need(owner['rejections'] == expected_rejected, 'snapshot rejection history')
        active = owner.get('active')
        if phase == 'idle':
            need(active is None and marker.get('identity') is None, 'idle snapshot has no live runtime')
            continue
        ident = admitted[-1]['identity']
        need(marker.get('identity') == ident and active is not None and active['identity'] == ident, 'metadata/active captured run identity')
        scope = ident['scope_id']
        at = active['event_seq']
        need(seq < at and events[at]['name'] == 'runtime.snapshot' and events[at].get('identity') == ident, 'separate marked runtime snapshot')
        prefix = [e for e in local[scope] if e['seq'] < at]
        names = {e['name'] for e in prefix}
        runtime_phase = 'drained' if 'scope.drained' in names else 'closing' if 'scope.admission_closed' in names else 'open'
        child_events = [e for e in prefix if e['name'] == 'child.registered']
        child_ids = [e['details']['child_id'] for e in child_events]
        joined = [e['details']['child_id'] for e in prefix if e['name'] == 'child.join_returned']
        frozen = child_ids if runtime_phase != 'open' else []
        cleanup_id = records[scope]['cleanup']['access_id']
        need(active['phase'] == runtime_phase and active['child_ids'] == child_ids
             and active['frozen_child_ids'] == frozen and active['joined_child_ids'] == joined
             and active['cleanup_access_id'] == cleanup_id
             and active['cleanup_disposition'] == ('undecided' if runtime_phase == 'open' else 'run'), 'runtime snapshot state replay')
        details(events[at], {'phase': runtime_phase, 'child_ids': csv(child_ids), 'joined_child_ids': csv(joined), 'cleanup_access_id': cleanup_id})
        expected_children = []
        for e in child_events:
            d, cid = e['details'], e['details']['child_id']
            occurred = {v['name'] for v in prefix if v['details'].get('child_id') == cid}
            child = d['child_scope']
            ended = 'child.run_returned' in occurred
            row = dict(child_id=cid, invocation_id=d['child_invocation'], scope_id=child,
                       owner_id=d['child_owner'], launch_mode='detached', joined=cid in joined,
                       phase=('failedSafe' if child in spec['failed_runs'] else 'succeeded') if ended else
                             ('running' if 'child.started' in occurred else 'registered'))
            if ended:
                row['fully_drained'] = True
            expected_children.append(row)
        need(active['children'] == expected_children, 'snapshot child phase/identity/join replay')
        if active.get('producer') is not None:
            need(active['producer'] == records[scope]['producer'] and 'producer.ready' in names, 'observed producer actual ready result')
        else:
            need('producer.join_returned' not in names, 'joined producer cannot be absent from snapshot')
        need(active.get('cleanup') == (records[scope]['cleanup'] if runtime_phase == 'drained' else None), 'cleanup result not fabricated while task pending')

    labels = ['before_reuse'] if not fault_case else ['cleanup_pending_before_release'] + (['Y_failed_Z_still_pending'] if spec['multi'] else []) + ['before_reuse']
    need([c['label'] for c in case['checkpoints']] == labels, 'complete intended checkpoint set')
    by_label = {}
    for checkpoint in case['checkpoints']:
        marker = events[checkpoint['event_seq']]
        need(marker['name'] == 'case.checkpoint' and checkpoint['atomic_across_owners'] is False, 'explicit non-atomic cross-owner observation')
        details(marker, {'label': checkpoint['label'], 'atomic_across_owners': 'false'})
        need([o['owner_id'] for o in checkpoint['owners']] == [o['owner_id'] for o in case['owners']], 'checkpoint complete owner inventory')
        for owner in checkpoint['owners']:
            need(owner['metadata_event_seq'] < marker['seq'] and (owner.get('active') is None or owner['active']['event_seq'] < marker['seq']), 'checkpoint follows marked constituent observations')
        by_label[checkpoint['label']] = checkpoint
    reuse_checkpoint = events[by_label['before_reuse']['event_seq']]
    before(parent_join, reuse_checkpoint, one(local['Y/B'], 'owner.permit_granted'))
    need(all(o['phase'] == 'idle' and o['epoch'] == 1 for o in by_label['before_reuse']['owners']), 'all original epochs returned before separate reuse')
    if fault_case:
        checkpoint = by_label['cleanup_pending_before_release']
        marker = events[checkpoint['event_seq']]
        before(gate_bounds['Y'][0], marker, rejected[0], gate_bounds['Y'][1])
        sampled = {o['owner_id']: o for o in checkpoint['owners']}
        need(sampled[ids['Y']['owner_id']]['active']['phase'] == 'closing', 'Y actual cleanup pending')
        need(sampled[ids['X']['owner_id']]['active']['phase'] == ('open' if name == CASES[4] else 'closing'), 'parent correct await/close phase')
        if spec['multi']:
            before(gate_bounds['Z'][0], marker)
            checkpoint = by_label['Y_failed_Z_still_pending']
            marker = events[checkpoint['event_seq']]
            sampled = {o['owner_id']: o for o in checkpoint['owners']}
            before(end['Y']['returned'], marker, gate_bounds['Z'][1])
            need(sampled[ids['Y']['owner_id']]['phase'] == 'idle' and sampled[ids['Y']['owner_id']]['records'][0]['execution_succeeded'] is False,
                 'failed Y genuinely returned while sibling pending')
            need(sampled[ids['Z']['owner_id']]['phase'] == 'leased' and sampled[ids['Z']['owner_id']]['active']['phase'] == 'closing'
                 and sampled[ids['Z']['owner_id']]['records'] == [] and sampled[ids['Z']['owner_id']]['active'].get('cleanup') is None,
                 'Z actual unfinished cleanup retained')
            need(sampled[ids['X']['owner_id']]['phase'] == 'leased' and sampled[ids['X']['owner_id']]['records'] == [], 'parent cannot return on first child failure')

    cancels = [e for e in events if e['name'] in ('case.cancel_requested', 'case.cancel_accepted')]
    if name == CASES[3]:
        requested, accepted = one(cancels, 'case.cancel_requested'), one(cancels, 'case.cancel_accepted')
        before(end['X']['close'], requested, accepted, gate_bounds['Y'][1])
        for e in (requested, accepted):
            details(e, {'target': 'parent-X-caller'})
            need(e.get('identity') is None, 'controller cancellation record is not target task sample')
        actual = [e for e in local['X'] if e['name'] in ('child.join_returned', 'cleanup.join_started', 'cleanup.join_returned',
                                                       'scope.drained', 'owner.permit_returned', 'scope.run_returned')]
        need(actual and all(e['seq'] > accepted['seq'] and e['cancelled'] is True for e in actual), 'cancelled parent actually continues complete drain')
        need(all(e['cancelled'] is False for s in ('Y', 'Z', 'Y/B') for e in local[s]), 'parent cancellation does not invent child cancellation')
    else:
        need(cancels == [] and all(e['cancelled'] is False for e in events), 'no unrequested cancellation')
    handles = [e for e in events if e['name'].startswith('child.handle_await_')]
    if name == CASES[4]:
        started, returned = one(handles, 'child.handle_await_started'), one(handles, 'child.handle_await_returned')
        need(started['identity'] == returned['identity'] == ids['X'], 'handle observer is parent')
        details(started, {'child_invocation': ids['Y']['invocation_id']})
        details(returned, {'child_invocation': ids['Y']['invocation_id'], 'fully_drained': 'true'})
        before(started, returned, one(local['X'], 'scope.body_error'), end['X']['close'])
        before(end['Y']['returned'], returned)
    else:
        need(handles == [], 'no invented handle observations')
    drops = [e for e in events if e['name'] == 'case.child_handle_dropped']
    expected_drops = (['child-Z', 'child-Y'] if spec['multi'] else ['child-Y']) if name in CASES[1:4] else []
    need([e['details']['child_id'] for e in drops] == expected_drops and all(e.get('identity') == ids['X'] for e in drops), 'real handle-drop scenario')
    direct = [e for e in events if e['name'].startswith('case.direct_await_')]
    if name == CASES[0]:
        a, b = one(direct, 'case.direct_await_started'), one(direct, 'case.direct_await_returned')
        need(a.get('identity') == b.get('identity') == ids['X'], 'direct await actual caller')
        details(a, {'target_owner': ids['Y']['owner_id']})
        details(b, {'fully_drained': 'true'})
        before(a, one(local['Y'], 'owner.permit_granted'), end['Y']['returned'], b, one(local['X'], 'scope.body_returned'))
    else:
        need(direct == [], 'no invented direct control')


def artifact_audit(attempt, outer, expected_manifest):
    root, outer = Path(attempt).resolve(), Path(outer).resolve()
    need(re.fullmatch(r'[0-9a-f]{64}', expected_manifest), 'externally supplied frozen manifest pin')
    manifest, prepared = load(root / 'manifest.json'), load(root / 'prepared.json')
    pin, report = load(root / 'execution-pin.json'), load(root / 'report.json')
    need(identity(root / 'manifest.json')['sha256'] == expected_manifest == prepared['manifest_sha256'] == pin['manifest_sha256'], 'frozen manifest binding')
    need(manifest['schema_version'] == 1 and manifest['experiment'] == 'swift-nested-access', 'manifest schema')
    inventory = {'source/' + n for n in SOURCE} | {'derived/' + n for n in DERIVED}
    actual = {str(p.relative_to(root)) for folder in ('source', 'derived') for p in (root / folder).rglob('*') if p.is_file()}
    need(set(manifest['files']) == actual == inventory, 'complete fixed payload inventory')
    known = {}
    for folder in ('source', 'derived'):
        need(not (root / folder).is_symlink(), 'payload directory must not be redirected')
    for relative, row in manifest['files'].items():
        path = root / relative
        need(not path.is_symlink() and path.resolve().is_relative_to(root), 'bounded source path')
        known[relative] = {k: row[k] for k in ('sha256', 'bytes')}
        need(identity(path) == known[relative], 'source bytes ' + relative)
        need(row['compiled'] is (relative in {'source/' + n for n in SWIFT}), 'actual compiled-source declaration')
    for relative, sha in DERIVED.items():
        need(known['derived/' + relative]['sha256'] == sha, 'fixed earlier derivation source')
    prior_fixture = load(root / 'source/test-fixtures/provenance.json')
    need({k: prior_fixture[k] for k in ('sha256', 'bytes')} == known['source/test-fixtures/direct-await-normal-attempt-1.json']
         and prior_fixture['original_attempt_validation_passed'] is False
         and prior_fixture['original_attempt_completed'] is False
         and prior_fixture['actual_case_process_returncode'] == 0
         and len(prior_fixture['original_error']) == 1 and "KeyError: 'active'" in prior_fixture['original_error'][0],
         'earlier successful case fixture retains original failed collection provenance')
    prior_raw = load(root / 'source/test-fixtures/direct-await-normal-attempt-1.json')
    need(prior_raw['completed'] is True and prior_raw['validation_passed'] is True
         and len(prior_raw['cases']) == 1 and prior_raw['cases'][0]['case_id'] == CASES[0],
         'earlier fixture is only one actual case, not five new trial results')
    driver_hash = known['source/driver.py']['sha256']
    need(driver_hash == prepared['driver_sha256'] == pin['driver_sha256'], 'actual frozen driver identity')
    command = prepared['execute_command']
    need(len(command) == 9, 'exact prepared command length')
    origin = Path(command[4])
    expected = [manifest['runtime']['python'], str(origin / 'source/driver.py'), 'execute', '--attempt', str(origin),
                '--manifest-sha256', expected_manifest, '--driver-sha256', driver_hash]
    need(command == expected and prepared['prepared_without_compilation_or_execution'] is True, 'prepared exact invocation')
    need(pin['driver_path'] == str(origin / 'source/driver.py') and pin['argv'] == command[1:], 'actual executed frozen driver argv')
    need(pin['python_identity'] == manifest['runtime']['python_identity'] and pin['compiler_identity'] == COMPILER, 'recorded Python and independently pinned compiler identity')
    need(report['schema_version'] == 1 and report['experiment'] == 'swift-nested-access'
         and report['completed'] is True and report['validation_passed'] is True
         and report['post_binding_passed'] is True and report['errors'] == [] and report['provenance'] == pin,
         'original collection failures or provenance mismatch')
    cfg = manifest['configuration']
    need(Path(cfg['compiler']).name == 'swiftc' and cfg['target'] == 'arm64-apple-macosx26.0'
         and Path(cfg['sdk']).name == 'MacOSX.sdk', 'actual compiler entrypoint/target/explicit SDK')
    swift = ['source/' + n for n in SWIFT]
    binary = 'build/nested-access'
    stages = {
        'compiler-version': ([cfg['compiler'], '--version'], set(), set(), 90),
        'compile': ([cfg['compiler'], '-swift-version', '6', '-target', cfg['target'], '-sdk', cfg['sdk'],
                     '-O', '-parse-as-library', *[str(origin / p) for p in swift], '-o', str(origin / binary)], set(swift), {binary}, 90),
    }
    stages.update({'cases/' + name: ([str(origin / binary), '--case', name], {binary}, set(), 30) for name in CASES})
    need([s['name'] for s in report['stages']] == list(stages), 'all seven actual stages in fixed order')
    for row in report['stages']:
        name = row['name']
        folder = root / name
        argv, inputs, outputs, timeout = stages[name]
        need(load(folder / 'run.json') == {k: v for k, v in row.items() if k != 'name'}, 'raw stage and summary continuity')
        need(load(folder / 'started.json') == dict(command=row['command'], inputs_before=row['inputs_before'], timeout_seconds=timeout), 'pre-execution actual input binding')
        need(row['command'] == argv and set(row['inputs_before']) == inputs and set(row['artifacts']) == outputs, 'exact stage argv/input/artifact set')
        need(row['returncode'] == 0 and row['timed_out'] is False and row['inputs_unchanged_after'] is True
             and not row.get('error'), 'nonzero/timeout/mutated actual stage')
        need(type(row['elapsed_seconds']) in (float, int) and math.isfinite(row['elapsed_seconds']) and row['elapsed_seconds'] > 0, 'actual stage elapsed evidence')
        need(row['descendant_termination_verified'] is False, 'do not invent process-tree verification')
        for relative, value in row['inputs_before'].items():
            need(known[relative] == value == identity(root / relative), 'prior source/artifact fed to actual stage')
        for relative, value in row['artifacts'].items():
            need(relative not in known and identity(root / relative) == value, 'new compiled artifact bytes')
            known[relative] = value
        need(set(row['logs']) == {'stdout.log', 'stderr.log'}, 'both saved raw streams')
        for filename, value in row['logs'].items():
            need(identity(folder / filename) == value, 'raw log identity ' + name + '/' + filename)
        stderr = (folder / 'stderr.log').read_bytes()
        need(stderr == (b'swift-driver version: 1.148.6 ' if name == 'compiler-version' else b''), 'unexpected compiler/runtime diagnostics')
    version = (root / 'compiler-version/stdout.log').read_text()
    need('Swift version 6.3.3' in version and 'Target: arm64-apple-macosx' in version, 'actual compiler version output')
    need((root / 'compile/stdout.log').read_bytes() == b'', 'unexpected compiler stdout')
    fixture = load(root / 'source/fixtures.json')
    need(fixture['schema_version'] == 1 and fixture['experiment'] == 'swift-nested-access' and fixture['track'] == 'A', 'frozen fixture scope')
    need(fixture['work'] == dict(elements=128, iterations=31, initialization_passes=2, cleanup_passes=3, failure_after_passes=2)
         and fixture['seeds'] == dict(X=SEEDS['X'], Y=SEEDS['Y'], Z=SEEDS['Z'], Y_reuse_B=SEEDS['Y/B']), 'independently fixed numerical fixture')
    expected_cases = [dict(id=name, parent_execution_succeeded=name == CASES[0],
                          unique_faults=4 if name == CASES[2] else int(name != CASES[0]),
                          registered_children=len(specification(name)['children'])) for name in CASES]
    need(fixture['cases'] == expected_cases, 'fixed semantic fixture')
    need([c['case_id'] for c in report['cases']] == list(CASES), 'all original per-case summary records')
    for entry, name in zip(report['cases'], CASES):
        need(entry['validation']['passed'] is True and entry['original_execution_succeeded'] is (name == CASES[0]), 'failed business result not upgraded by outer pass')
        raw = load(root / 'cases' / name / 'stdout.log')
        need(raw['cases'][0]['case_id'] == name and raw['cases'][0]['parent_outcome']['record']['execution_succeeded'] is entry['original_execution_succeeded'], 'summary versus actual case outcome')
    run = load(outer / 'run.json')
    need(run['command'] == command and run['returncode'] == 0 and run['timed_out'] is False, 'outer exact prepared command and success')
    need(math.isfinite(run['elapsed_seconds']) and run['elapsed_seconds'] > 0 and run['timeout_seconds'] > 0, 'outer bounded process evidence')
    need(set(run['files']) == {'stdout.json', 'stderr.log'}, 'outer full streams')
    for filename, row in run['files'].items():
        need(identity(outer / filename) == row, 'outer stream hash binding')
    need(load(outer / 'stdout.json') == report and (outer / 'stderr.log').read_bytes() == b'', 'outer and saved report agreement')
    return dict(passed=True, manifest_sha256=expected_manifest, report_identity=identity(root / 'report.json'),
                binary=known[binary], payload_identities={p: known[p] for p in sorted(inventory)},
                compiler=pin['compiler_identity'], stage_count=len(stages),
                external_scope='Recorded compiler identity checked against earlier independent pin; current external binaries, SDK and dylib closure are not rehashed.')


def analyze(attempt, outer, expected_manifest):
    artifacts = artifact_audit(attempt, outer, expected_manifest)
    root = Path(attempt)
    cases = [audit_case(load(root / 'cases' / name / 'stdout.log')) for name in CASES]
    return dict(schema_version=1, experiment='swift-nested-access-independent-review', passed=True,
                evidence_integrity_passed=True, artifacts=artifacts, cases=cases, case_count=len(cases),
                original_failed_business_cases=[c['case_name'] for c in cases if not c['original_execution_succeeded']],
                actual_full_pass_arrays=sum(c['actual_full_pass_arrays'] for c in cases),
                actual_element_reads=sum(c['actual_element_reads'] for c in cases),
                limits=['Bounded CPU A0-A4 only; no parent live lease, actual unknown-completion injection, GPU or performance experiment.',
                        'Actual throw assessment combines frozen source/binary identity with raised/caught/partial-read evidence; trace events alone are not a language-proof theorem.',
                        'Per-owner metadata and active-runtime snapshots have separate markers; cross-owner checkpoints are not globally atomic.',
                        'A private fixed CPU allocation is reused across two epochs; no Apple GPU buffer or static public client capability guarantee.',
                        'Cancellation is an observed parent task flag while registered detached work is drained, not prompt detachment or forced termination.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt', type=Path, required=True)
    parser.add_argument('--outer', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    try:
        result = analyze(args.attempt, args.outer, args.manifest_sha256)
    except Exception as error:
        result = dict(passed=False, evidence_integrity_passed=False, error=type(error).__name__ + ': ' + str(error))
    result['checker_identity'] = identity(__file__)
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    print(json.dumps(dict(passed=result['passed'], output=str(args.output))))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
