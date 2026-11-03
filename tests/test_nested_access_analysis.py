"""Independent-checker regressions using disposable copies of real A0–A4 evidence.

No fixture synthesizes a successful execution.  Set NESTED_ACCESS_ATTEMPT and
NESTED_ACCESS_OUTER to the completed, frozen CPU attempt being reviewed.  Missing
evidence is an error, never a silently skipped or simulated positive control.
This module imports only the independent checker, not the experiment driver,
Swift ledger, or author oracle.  It never compiles Swift or starts GPU work.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import analyze_nested_access as audit

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'experiments/swift-nested-access'
ATTEMPT = Path(os.environ.get('NESTED_ACCESS_ATTEMPT', BASE / 'attempts/attempt-1'))
OUTER = Path(os.environ.get('NESTED_ACCESS_OUTER', BASE / 'verification/execute-attempt-1'))
CASES = (
    'direct-await-normal',
    'registered-child-cleanup-fault',
    'body-and-two-cleanup-faults',
    'cancel-parent-failed-sibling',
    'propagated-cleanup-fault',
)
REJECTIONS = (ValueError, KeyError, IndexError, TypeError, AssertionError)


def require_actual_evidence():
    expected = [ATTEMPT / 'report.json', OUTER / 'run.json']
    expected += [ATTEMPT / 'cases' / name / 'stdout.log' for name in CASES]
    absent = [str(path) for path in expected if not path.is_file()]
    if absent:
        raise FileNotFoundError(
            'Actual completed A0–A4 evidence is required; set '
            'NESTED_ACCESS_ATTEMPT/NESTED_ACCESS_OUTER. Missing: ' + ', '.join(absent))


def raw(name):
    return audit.load(ATTEMPT / 'cases' / name / 'stdout.log')


def case(data):
    return data['cases'][0]


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in list(value.values()):
            yield from walk(child)
    elif isinstance(value, list):
        for child in list(value):
            yield from walk(child)


def record(data, scope, epoch=1):
    return next(r for owner in case(data)['owners'] for r in owner['records']
                if r['identity']['scope_id'] == scope and r['identity']['epoch'] == epoch)


def event(data, name, scope=None):
    return next(e for e in case(data)['events'] if e['name'] == name
                and (scope is None or (e.get('identity') or {}).get('scope_id') == scope))


def all_records(data, scope, epoch=1):
    return [d for d in walk(data) if 'producer' in d and 'cleanup' in d
            and 'work' in d and d.get('identity', {}).get('scope_id') == scope
            and d['identity']['epoch'] == epoch]


def reindex(data):
    """Keep every surviving event reference coherent after a negative edit."""
    events = case(data)['events']
    mapping = {e['seq']: index for index, e in enumerate(events)}
    for d in walk(case(data)):
        for key in ('event_seq', 'metadata_event_seq'):
            if key in d and d[key] in mapping:
                d[key] = mapping[d[key]]
    for index, e in enumerate(events):
        e['seq'] = index
        e['t_ns'] = index


def remove_event(data, name, scope=None):
    case(data)['events'].remove(event(data, name, scope))
    reindex(data)


def move_before(data, name, target, scope=None, target_scope=None):
    events = case(data)['events']
    moving = event(data, name, scope)
    anchor = event(data, target, target_scope if target_scope is not None else scope)
    events.remove(moving)
    events.insert(events.index(anchor), moving)
    reindex(data)


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_actual_evidence()

    def reject(self, name, change):
        data = raw(name)
        change(data)
        with self.assertRaises(REJECTIONS):
            audit.audit_case(data)

    def test_all_five_actual_cases(self):
        for name in CASES:
            with self.subTest(case=name):
                self.assertTrue(audit.audit_case(raw(name))['passed'])

    def test_actual_array_element_rejected_even_when_all_copies_agree(self):
        def change(data):
            for d in walk(data):
                if 'actual_values' in d:
                    d['actual_values'][77] ^= 1
        self.reject('direct-await-normal', change)

    def test_cumulative_checksum_is_recomputed(self):
        def change(data):
            for d in walk(data):
                if 'checksum' in d:
                    value = d['checksum']
                    d['checksum'] = str(int(value) ^ 1) if isinstance(value, str) else value ^ 1
        self.reject('registered-child-cleanup-fault', change)

    def test_cumulative_visits_are_recomputed(self):
        def change(data):
            for d in walk(data):
                if 'visits' in d:
                    value = d['visits']
                    d['visits'] = str(int(value) - 1) if isinstance(value, str) else value - 1
        self.reject('direct-await-normal', change)

    def test_every_full_pass_has_128_elements(self):
        def change(data):
            for d in walk(data):
                if 'actual_values' in d:
                    d['actual_values'].pop()
        self.reject('registered-child-cleanup-fault', change)

    def test_fault_cannot_invent_third_pass(self):
        def change(data):
            for r in all_records(data, 'Y'):
                extra = copy.deepcopy(r['cleanup']['evidence']['passes'][-1])
                extra['pass_index'] = 2
                r['cleanup']['evidence']['passes'].append(extra)
                r['cleanup']['not_executed_passes'] = []
        self.reject('registered-child-cleanup-fault', change)

    def test_failed_cleanup_cannot_publish_successful_result(self):
        def change(data):
            for r in all_records(data, 'Y'):
                r['cleanup']['successful_result'] = copy.deepcopy(r['cleanup']['evidence'])
        self.reject('registered-child-cleanup-fault', change)

    def test_original_business_failure_cannot_be_promoted(self):
        def change(data):
            for r in all_records(data, 'X'):
                r['execution_succeeded'] = True
        self.reject('body-and-two-cleanup-faults', change)

    def test_fault_summary_requires_actual_raise_event(self):
        self.reject('registered-child-cleanup-fault',
                    lambda d: remove_event(d, 'fault.raised', 'Y'))

    def test_async_throw_requires_actual_catch_event(self):
        self.reject('registered-child-cleanup-fault',
                    lambda d: remove_event(d, 'cleanup.error_caught', 'Y'))

    def test_read_cleanup_cannot_be_relabelled_backend_failure(self):
        def change(data):
            for d in walk(data):
                if d.get('origin') == 'read_cleanup':
                    d['origin'] = 'producer/backend_after_access'
        self.reject('registered-child-cleanup-fault', change)

    def test_original_cause_type_is_distinct_from_thrown_wrapper(self):
        def change(data):
            for d in walk(data):
                if d.get('origin') == 'read_cleanup' and 'actual_thrown_type' in d:
                    d['actual_thrown_type'] = d['cause_type']
            caught = event(data, 'cleanup.error_caught', 'Y')
            caught['details']['actual_caught_type'] = event(data, 'fault.raised', 'Y')['details']['cause_type']
        self.reject('registered-child-cleanup-fault', change)

    def test_actual_caught_type_matches_original_wrapper(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    event(d, 'cleanup.error_caught', 'Y')['details'].__setitem__(
                        'actual_caught_type', 'UnexpectedOtherError'))

    def test_cleanup_fault_requires_two_completed_real_reads(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    move_before(d, 'fault.raised', 'read.pass_returned', 'Y'))

    def test_cleanup_is_registered_before_body_exit(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    move_before(d, 'scope.body_returned', 'cleanup.registered', 'Y'))

    def test_cleanup_must_wait_until_body_returns(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    move_before(d, 'cleanup.started', 'scope.body_returned', 'Y'))

    def test_gate_waiter_must_be_registered_while_closed(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    event(d, 'gate.waiter_registered', 'Y')['details'].__setitem__('gate_open', 'true'))

    def test_gate_resumption_precedes_actual_wait_return(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    move_before(d, 'gate.await_returned', 'gate.resume_issued', 'Y'))

    def test_gate_resume_keeps_exact_waiter_identity(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    event(d, 'gate.resume_issued', 'Y')['details'].__setitem__('waiter_id', 'other-waiter'))

    def test_child_task_is_retained_before_start(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    event(d, 'child.registered', 'X')['details'].__setitem__('task_retained', 'false'))

    def test_child_cannot_start_before_registration_gate(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    move_before(d, 'child.started', 'child.start_gate_opening', 'X'))

    def test_parent_must_join_every_registered_child(self):
        self.reject('body-and-two-cleanup-faults', lambda d:
                    remove_event(d, 'child.join_returned', 'X'))

    def test_equal_child_counts_do_not_replace_exact_identity(self):
        def change(data):
            for r in all_records(data, 'X'):
                r['joined_child_ids'][0] = 'other-child'
        self.reject('registered-child-cleanup-fault', change)

    def test_child_actual_invocation_is_bound_to_registered_identity(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    event(d, 'child.run_returned', 'X')['details'].__setitem__('child_invocation', 'other-run'))

    def test_cleanup_actual_access_id_is_bound_to_registration(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    event(d, 'read.access_ended', 'Y')['details'].__setitem__('access_id', 'other-access'))

    def test_owner_cannot_return_before_actual_access_end(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    move_before(d, 'owner.permit_returned', 'read.access_ended', 'Y'))

    def test_owner_cannot_return_before_child_join(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    move_before(d, 'owner.permit_returned', 'child.join_returned', 'X'))

    def test_same_named_faults_cannot_be_coalesced(self):
        def change(data):
            original = record(data, 'Y')['cleanup']['faults'][0]['fault_id']
            other = record(data, 'Z')['cleanup']['faults'][0]['fault_id']
            for d in walk(data):
                if d.get('fault_id') == other:
                    d['fault_id'] = original
        self.reject('body-and-two-cleanup-faults', change)

    def test_propagated_fault_keeps_original_invocation(self):
        def change(data):
            parent_identity = copy.deepcopy(record(data, 'X')['identity'])
            case(data)['parent_outcome']['record']['faults'][0]['identity'] = parent_identity
        self.reject('propagated-cleanup-fault', change)

    def test_repeated_observation_retains_handle_body_and_drain_channels(self):
        def change(data):
            for r in all_records(data, 'X'):
                for fault in r['faults']:
                    fault['reported_via'] = [v for v in fault['reported_via'] if not v.endswith('/body.catch')]
        self.reject('propagated-cleanup-fault', change)

    def test_parent_cancellation_is_observed_by_actual_task(self):
        def change(data):
            for e in case(data)['events']:
                if (e.get('identity') or {}).get('scope_id') == 'X':
                    e['cancelled'] = False
        self.reject('cancel-parent-failed-sibling', change)

    def test_parent_flag_cannot_falsely_cancel_detached_child(self):
        self.reject('cancel-parent-failed-sibling', lambda d:
                    event(d, 'scope.run_returned', 'Y').__setitem__('cancelled', True))

    def test_cancel_request_must_have_actual_target_event(self):
        self.reject('cancel-parent-failed-sibling', lambda d:
                    event(d, 'case.cancel_requested')['details'].__setitem__('target', 'child-Y'))

    def test_failed_y_does_not_discard_pending_z(self):
        def change(data):
            cp = next(p for p in case(data)['checkpoints'] if p['label'] == 'Y_failed_Z_still_pending')
            z = next(o for o in cp['owners'] if o['owner_id'].startswith('Z/'))
            z['phase'] = 'idle'
            z['active'] = None
        self.reject('cancel-parent-failed-sibling', change)

    def test_checkpoint_references_exact_observation_event(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    case(d)['checkpoints'][0].__setitem__('event_seq', case(d)['checkpoints'][0]['event_seq'] + 1))

    def test_multi_owner_checkpoint_does_not_claim_global_atomicity(self):
        self.reject('body-and-two-cleanup-faults', lambda d:
                    case(d)['checkpoints'][0].__setitem__('atomic_across_owners', True))

    def test_reuse_requires_next_epoch(self):
        def change(data):
            for r in all_records(data, 'Y/B', 2):
                r['identity']['epoch'] = 1
        self.reject('registered-child-cleanup-fault', change)

    def test_reuse_requires_same_actual_allocation(self):
        def change(data):
            for r in all_records(data, 'Y/B', 2):
                r['identity']['allocation_id'] = 'different-allocation'
        self.reject('registered-child-cleanup-fault', change)

    def test_reuse_write_cannot_precede_prior_return(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    move_before(d, 'producer.access_started', 'owner.permit_returned', 'Y/B', 'Y'))

    def test_busy_probe_is_not_unknown_completion(self):
        self.reject('registered-child-cleanup-fault', lambda d:
                    case(d)['busy_probe']['rejection'].__setitem__('classification', 'unknownCompletion'))

    def test_no_unknown_ledger_execution_is_invented(self):
        self.reject('direct-await-normal', lambda d: d['ledger_tests'].append('fabricated-device-unknown'))

    def test_incomplete_trace_is_not_complete_success(self):
        self.reject('direct-await-normal', lambda d: d.__setitem__('completed', False))

    def test_unknown_extra_error_is_not_expected_failure(self):
        self.reject('registered-child-cleanup-fault', lambda d: d['errors'].append('unexpected runtime failure'))


class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_actual_evidence()
        cls.expected_manifest = audit.identity(ATTEMPT / 'manifest.json')['sha256']
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.attempt, cls.outer = root / 'attempt', root / 'outer'
        for src, dest in ((ATTEMPT, cls.attempt), (OUTER, cls.outer)):
            shutil.copytree(src, dest)
            dest.chmod(dest.stat().st_mode | 0o700)
            for path in dest.rglob('*'):
                path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o200))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.changed = {}

    def tearDown(self):
        for path, original in self.changed.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)

    def write_bytes(self, path, value):
        self.changed.setdefault(path, path.read_bytes() if path.exists() else None)
        path.write_bytes(value)

    def write(self, path, value):
        self.write_bytes(path, (json.dumps(value, indent=2) + '\n').encode())

    def report(self):
        return audit.load(self.attempt / 'report.json')

    def refresh(self, report):
        self.write(self.attempt / 'report.json', report)
        self.write(self.outer / 'stdout.json', report)
        outer = audit.load(self.outer / 'run.json')
        outer['files']['stdout.json'] = audit.identity(self.outer / 'stdout.json')
        self.write(self.outer / 'run.json', outer)

    def stage(self, report, name):
        return next(s for s in report['stages'] if s['name'] == name)

    def save_stage(self, row, update_started=False):
        self.write(self.attempt / row['name'] / 'run.json', {k: v for k, v in row.items() if k != 'name'})
        if update_started:
            path = self.attempt / row['name'] / 'started.json'
            started = audit.load(path)
            started['command'] = row['command']
            started['inputs_before'] = row['inputs_before']
            self.write(path, started)

    def reject(self):
        with self.assertRaises(REJECTIONS):
            audit.artifact_audit(self.attempt, self.outer, self.expected_manifest)

    def test_complete_actual_artifact_chain_and_relocated_payload(self):
        self.assertTrue(audit.artifact_audit(self.attempt, self.outer, self.expected_manifest)['passed'])

    def test_external_manifest_pin_cannot_be_replaced(self):
        with self.assertRaises(REJECTIONS):
            audit.artifact_audit(self.attempt, self.outer, '0' * 64)

    def test_frozen_source_bytes_are_rechecked(self):
        path = self.attempt / 'source/AccessOwner.swift'
        self.write_bytes(path, path.read_bytes() + b'\n// changed after execution\n')
        self.reject()

    def test_derived_source_bytes_are_rechecked(self):
        path = self.attempt / 'derived/c2/Support.swift'
        self.write_bytes(path, path.read_bytes() + b'\n// changed provenance\n')
        self.reject()

    def test_unmanifested_source_is_rejected(self):
        self.write_bytes(self.attempt / 'source/Unexpected.swift', b'// unbound source\n')
        self.reject()

    def test_required_compile_input_cannot_disappear_consistently(self):
        report = self.report()
        row = self.stage(report, 'compile')
        row['inputs_before'].pop('source/Support.swift')
        self.save_stage(row, update_started=True)
        self.refresh(report)
        self.reject()

    def test_compiler_driver_invocation_cannot_change_to_frontend(self):
        report = self.report()
        row = self.stage(report, 'compile')
        row['command'][0] = str(Path(row['command'][0]).with_name('swift-frontend'))
        self.save_stage(row, update_started=True)
        self.refresh(report)
        self.reject()

    def test_compiler_identity_is_bound_to_actual_execution_pin(self):
        pin = audit.load(self.attempt / 'execution-pin.json')
        pin['compiler_identity']['sha256'] = '0' * 64
        self.write(self.attempt / 'execution-pin.json', pin)
        report = self.report()
        report['provenance'] = pin
        self.refresh(report)
        self.reject()

    def test_binary_bytes_are_rechecked(self):
        path = self.attempt / 'build/nested-access'
        self.write_bytes(path, path.read_bytes() + b'changed')
        self.reject()

    def test_case_input_must_equal_compiler_output_identity(self):
        report = self.report()
        row = self.stage(report, 'cases/' + CASES[0])
        row['inputs_before']['build/nested-access']['sha256'] = '0' * 64
        self.save_stage(row, update_started=True)
        self.refresh(report)
        self.reject()

    def test_raw_log_bytes_are_rechecked(self):
        path = self.attempt / 'cases' / CASES[1] / 'stdout.log'
        self.write_bytes(path, path.read_bytes() + b' ')
        self.reject()

    def test_consistently_rehashed_raw_still_requires_semantic_replay(self):
        report = self.report()
        row = self.stage(report, 'cases/' + CASES[1])
        path = self.attempt / row['name'] / 'stdout.log'
        data = audit.load(path)
        data['cases'][0]['parent_outcome']['record']['execution_succeeded'] = True
        self.write(path, data)
        row['logs']['stdout.log'] = audit.identity(path)
        self.save_stage(row)
        self.refresh(report)
        self.reject()

    def test_original_failed_case_summary_is_not_promoted(self):
        report = self.report()
        next(c for c in report['cases'] if c['case_id'] == CASES[1])['original_execution_succeeded'] = True
        self.refresh(report)
        self.reject()

    def test_compile_stderr_cannot_be_ignored_after_hash_refresh(self):
        report = self.report()
        row = self.stage(report, 'compile')
        path = self.attempt / 'compile/stderr.log'
        self.write_bytes(path, b'unknown compiler diagnostic\n')
        row['logs']['stderr.log'] = audit.identity(path)
        self.save_stage(row)
        self.refresh(report)
        self.reject()

    def test_compiler_version_stderr_allows_only_recorded_driver_banner(self):
        report = self.report()
        row = self.stage(report, 'compiler-version')
        path = self.attempt / 'compiler-version/stderr.log'
        self.write_bytes(path, path.read_bytes() + b'unknown extra diagnostic\n')
        row['logs']['stderr.log'] = audit.identity(path)
        self.save_stage(row)
        self.refresh(report)
        self.reject()

    def test_case_nonzero_exit_is_not_expected_business_failure(self):
        report = self.report()
        row = self.stage(report, 'cases/' + CASES[1])
        row['returncode'] = 1
        self.save_stage(row)
        self.refresh(report)
        self.reject()

    def test_outer_timeout_is_not_successful_collection(self):
        row = audit.load(self.outer / 'run.json')
        row['timed_out'] = True
        self.write(self.outer / 'run.json', row)
        self.reject()

    def test_outer_argv_must_execute_frozen_prepared_driver(self):
        row = audit.load(self.outer / 'run.json')
        row['command'][1] = '/tmp/unfrozen-driver.py'
        self.write(self.outer / 'run.json', row)
        self.reject()

    def test_post_execution_binding_is_required(self):
        report = self.report()
        report['post_binding_passed'] = False
        self.refresh(report)
        self.reject()

    def test_cli_refuses_output_overwrite(self):
        path = Path(self.temp.name) / 'existing-analysis.json'
        path.write_text('keep')
        try:
            argv = ['audit', '--attempt', str(self.attempt), '--outer', str(self.outer),
                    '--manifest-sha256', self.expected_manifest, '--output', str(path)]
            with patch.object(sys, 'argv', argv):
                with self.assertRaises(FileExistsError):
                    audit.main()
            self.assertEqual(path.read_text(), 'keep')
        finally:
            path.unlink()


if __name__ == '__main__':
    unittest.main()
