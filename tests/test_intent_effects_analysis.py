"""Adversarial evidence checks; never import/execute the effect driver or tools."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('intent_audit', ROOT/'scripts/analyze_intent_effects.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
ATTEMPT = Path(os.environ.get('INTENT_EFFECTS_ATTEMPT', ROOT/'experiments/tiny-intent-effects/attempts/smoke-v1'))


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads((ATTEMPT/'execution/stdout.json').read_bytes())

    def changed(self):
        return copy.deepcopy(self.raw)

    def case(self, data, name='E2'):
        return next(c for c in data['cases'] if c['case'] == name)

    def event(self, data, name, case='E2'):
        return next(e for e in self.case(data, case)['events'] if e['name'] == name)

    def rejects(self, data, pattern):
        with self.assertRaisesRegex(audit.Invalid, pattern):
            audit.check_data(data)

    def test_all_six_actual_traces_and_files(self):
        result = audit.analyze(ATTEMPT)
        self.assertTrue(result['passed'])
        self.assertEqual(sum(c['counts']['commits'] for c in result['cases']), 4)
        self.assertEqual(sum(c['counts']['receipt_appends'] for c in result['cases']), 3)
        self.assertFalse(result['cases'][-1]['execution_succeeded'])

    def test_missing_case(self):
        d = self.changed(); d['cases'].pop()
        self.rejects(d, 'missing/duplicate cases')

    def test_failure_cannot_be_renamed_success(self):
        d = self.changed(); self.case(d, 'F')['execution_succeeded'] = True
        self.rejects(d, 'failure disguised')

    def test_top_cannot_claim_all_executions_succeeded(self):
        d = self.changed(); d['all_executions_succeeded'] = True
        self.rejects(d, 'failure semantics')

    def test_equal_timestamp_does_not_hide_reordered_start(self):
        d = self.changed(); c = self.case(d)
        for e in c['events']: e['t_ms'] = 1.0
        audit.check_data(d)
        c['events'][6], c['events'][11] = c['events'][11], c['events'][6]
        for i,e in enumerate(c['events']): e['seq'] = i
        self.rejects(d, 'event schedule')

    def test_sequence_identity(self):
        d = self.changed(); self.event(d, 'intent.advanced')['seq'] += 1
        self.rejects(d, 'sequence identity')

    def test_invalid_time(self):
        d = self.changed(); self.event(d, 'intent.advanced')['t_ms'] = float('nan')
        self.rejects(d, 'invalid host time')

    def test_wrong_thread(self):
        d = self.changed(); self.event(d, 'file.commit_returned')['thread'] = 'MainThread'
        self.rejects(d, 'wrong event thread')

    def test_stale_start_epoch_binding(self):
        d = self.changed(); self.event(d, 'action.start_claimed')['details']['current_epoch'] = 1
        self.rejects(d, 'start binding')

    def test_action_mutation(self):
        d = self.changed(); self.event(d, 'proposal.created')['details']['canonical_action'] = '{}'
        self.rejects(d, 'immutable action')

    def test_wrong_call_receipt(self):
        d = self.changed(); self.event(d, 'receipt.append_attempted')['details']['receipt']['tool_call_id'] = 'another-call'
        self.rejects(d, 'receipt not bound')

    def test_forged_receipt_digest(self):
        d = self.changed(); self.event(d, 'receipt.recorded')['details']['receipt']['receipt_id'] = '0'*64
        self.rejects(d, 'receipt append evidence')

    def test_commit_content_hash(self):
        d = self.changed(); self.event(d, 'file.commit_returned')['details']['after']['sha256'] = '0'*64
        self.rejects(d, 'file fact')

    def test_late_observation_cannot_satisfy_current_intent(self):
        d = self.changed(); self.event(d, 'observation.consumed')['details']['satisfies_current_intent'] = True
        self.rejects(d, 'wrong intent')

    def test_late_observation_cannot_be_current(self):
        d = self.changed(); self.event(d, 'observation.consumed', 'E3')['details']['classification'] = 'current_effect'
        self.rejects(d, 'wrong intent')

    def test_dropped_completion_obligation_snapshot(self):
        d = self.changed(); self.case(d)['controller']['tickets'] = {}
        self.rejects(d, 'snapshot disagrees')

    def test_no_double_effect_hidden_by_counter(self):
        d = self.changed(); c = self.case(d); c['events'].insert(15, copy.deepcopy(c['events'][14]))
        for i,e in enumerate(c['events']): e['seq'] = i
        self.rejects(d, 'schedule/multiplicity')

    def test_counts_recomputed(self):
        d = self.changed(); self.case(d)['counts']['commits'] = 0
        self.rejects(d, 'actual effect/read counts')

    def test_unjoined_worker(self):
        d = self.changed(); self.case(d)['worker']['joined'] = False
        self.rejects(d, 'worker not joined')

    def test_F_requires_original_append_error_chain(self):
        d = self.changed(); self.event(d, 'receipt.append_failed', 'F')['details']['traceback'] = 'simulated failure'
        self.rejects(d, 'actual missing-parent append')

    def test_F_cannot_hide_a_later_unknown_exception(self):
        d = self.changed(); c = self.case(d, 'F')
        event = self.event(d, 'receipt.append_failed', 'F')
        bad = event['details']['traceback']+'\nTraceback (most recent call last):\nRuntimeError: unexpected failure\n'
        event['details']['traceback'] = c['receipt_errors'][0]['traceback'] = bad
        self.rejects(d, 'unknown exception')

    def test_F_must_have_exact_declared_cause(self):
        d = self.changed(); c = self.case(d, 'F')
        event = self.event(d, 'receipt.append_failed', 'F')
        bad = event['details']['traceback'].replace('[Errno 2]', '[Errno 13]')
        event['details']['traceback'] = c['receipt_errors'][0]['traceback'] = bad
        self.rejects(d, 'unknown exception')

    def test_worker_cleanup_cannot_replace_normal_join(self):
        d = self.changed(); self.event(d, 'worker.joined')['details']['cleanup'] = True
        self.rejects(d, 'unexpected cleanup path')

    def test_resume_api_binding(self):
        d = self.changed(); self.event(d, 'checkpoint.resume_started', 'R')['details']['api'] = 'unverified resume'
        self.rejects(d, 'wrong checkpoint/API')

    def test_F_file_effect_cannot_be_erased(self):
        d = self.changed(); self.case(d, 'F')['observations']['final_file']['utf8'] = audit.BEFORE
        self.rejects(d, 'file fact')

    def test_rejected_proposal_cannot_get_ticket(self):
        d = self.changed(); self.case(d, 'E0')['controller']['tickets'] = {'ticket-P0': {}}
        self.rejects(d, 'snapshot disagrees')

    def test_resume_cannot_lose_prior_observation(self):
        d = self.changed(); self.case(d, 'R')['observations']['generator_calls_after'][0][5]['content'] = 'lost observation'
        self.rejects(d, 'resume prefix')

    def test_resume_cannot_repeat_edit(self):
        d = self.changed(); self.case(d, 'R')['observations']['resumed_response_index'] = 2
        self.rejects(d, 'prescribed script')

    def test_checkpoint_canonical_digest(self):
        d = self.changed(); o = self.case(d, 'R')['observations']
        o['checkpoint']['checkpoint_id'] = o['checkpoint_after']['checkpoint_id'] = '0'*64
        self.rejects(d, 'canonical digest')

    def test_checkpoint_must_end_in_complete_observation(self):
        d = self.changed(); o = self.case(d, 'R')['observations']
        o['checkpoint']['messages'].pop(); o['checkpoint_after']['messages'].pop()
        self.rejects(d, 'complete observation shape')

    def test_no_implicit_network_or_command_scope(self):
        d = self.changed(); self.case(d)['observations']['tool_policy']['allowed_commands'] = [['echo', 'test']]
        self.rejects(d, 'authorization scope')

    def artifact_copy(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        out = Path(temp.name)/'copy'
        shutil.copytree(ATTEMPT, out)
        for p in out.rglob('*'):
            p.chmod(0o755 if p.is_dir() else 0o644)
        return out

    def test_moved_copy_uses_local_artifacts_with_historical_paths(self):
        self.assertTrue(audit.analyze(self.artifact_copy())['passed'])

    def test_actual_file_tamper(self):
        p = self.artifact_copy(); (p/'effects/E2/workspace/app.py').write_text(audit.BEFORE)
        with self.assertRaisesRegex(audit.Invalid, 'actual final file'): audit.analyze(p)

    def test_actual_receipt_tamper(self):
        p = self.artifact_copy(); (p/'effects/E3/audit/receipts.jsonl').write_text('{}\n')
        with self.assertRaisesRegex(audit.Invalid, 'actual receipt JSONL'): audit.analyze(p)

    def test_missing_required_source(self):
        p = self.artifact_copy(); (p/'payload/reference/workspace.py').unlink()
        with self.assertRaisesRegex(audit.Invalid, 'source set'): audit.analyze(p)

    def test_changed_source_hash(self):
        p = self.artifact_copy(); (p/'payload/harness/run.py').write_text('# changed\n')
        with self.assertRaisesRegex(audit.Invalid, 'payload bytes/hash'): audit.analyze(p)

    def test_recorded_command_must_use_snapshot(self):
        p = self.artifact_copy(); path = p/'execution/run.json'; r = json.loads(path.read_text())
        r['command'][1] = '/some/live/run.py'; path.write_text(json.dumps(r))
        with self.assertRaisesRegex(audit.Invalid, 'actual command'): audit.analyze(p)

    def test_input_must_be_recorded_stdout(self):
        p = self.artifact_copy()
        with self.assertRaisesRegex(audit.Invalid, 'not recorded stdout'):
            audit.check_artifacts(p, self.raw, p/'some-other-raw.json')

    def test_unknown_error_is_not_expected_F(self):
        d = self.changed(); self.case(d, 'F')['errors'].append({'type': 'RuntimeError'})
        self.rejects(d, 'unknown execution failure')


if __name__ == '__main__':
    unittest.main()
