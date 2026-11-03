"""Mutate retained evidence only. Never compile or execute the Swift experiment."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('composition_audit',ROOT/'scripts/analyze_owner_composition.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)
ATTEMPT=Path(os.environ.get('COMPOSITION_ATTEMPT',ROOT/'experiments/swift-owner-composition/attempts/attempt-1'))
RECORD=Path(os.environ.get('COMPOSITION_RECORD',ROOT/'experiments/swift-owner-composition/verification/correctness-attempt-1'))
class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.raw=json.loads((RECORD/'stdout.json').read_bytes())
    def changed(self):return copy.deepcopy(self.raw)
    def case(self,d,name='live-readers-normal'):return next(c for c in d['cases'] if c['case_id']==name)
    def event(self,c,name,request=None):return next(e for e in c['events'] if e['name']==name and (request is None or e['request_id']==request))
    def reject(self,d,pattern=None):
        with self.assertRaisesRegex(audit.Invalid,pattern or '.'):
            audit.check_data(d)
    def move_before(self,c,name,target,request=None):
        event=self.event(c,name,request);c['events'].remove(event);i=c['events'].index(self.event(c,target));c['events'].insert(i,event)
        for e in c['events']:e['t_ns']=1
    def test_all_actual_cases_and_artifacts(self):
        out=audit.analyze(ATTEMPT,RECORD);self.assertTrue(out['passed']);self.assertEqual(len(out['cases']),11)
        self.assertEqual(sum(x['expected_request_failure'] for x in out['cases']),3)
    def test_oracle_fixed_vectors_and_iteration_control(self):
        for request,seed in audit.SEEDS.items():
            iterative=[]
            for i in range(128):
                x=i^seed
                for _ in range(31):x=(x*1664525+1013904223)%(1<<32)
                iterative.append(x)
            self.assertEqual(audit.values(request),iterative)
        self.assertEqual(audit.result('A')['checksum'],13105435158435203785)
        self.assertNotEqual(audit.values('A'),audit.values('B'))
    def test_missing_case_rejected(self):
        d=self.changed();d['cases'].pop();self.reject(d,'eleven')
    def test_ledger_selftest_failure_not_hidden(self):
        d=self.changed();d['protocol_tests'][0]['passed']=False;self.reject(d,'ledger self-test')
    def test_case_unknown_error_event(self):
        d=self.changed();c=self.case(d);c['events'][-1]['name']='case.unexpected_error';self.reject(d)
    def test_equal_timestamps_legal_order(self):
        d=self.changed()
        for c in d['cases']:
            for e in c['events']:e['t_ns']=1
        self.assertEqual(len(audit.check_data(d)),11)
    def test_invalid_clock(self):
        d=self.changed();self.case(d)['events'][0]['t_ns']=float('nan');self.reject(d,'clock')
    def test_c1_whole_array_tamper(self):
        d=self.changed();self.case(d,'shared-result-normal')['observations']['actual_reads']['A'][71]^=1;self.reject(d,'array oracle')
    def test_c1_submitted_generation_must_match(self):
        d=self.changed();c=self.case(d,'shared-result-normal');self.event(c,'work.submitted','A')['generation']=2;self.reject(d,'submitted generation')
    def test_c1_reserved_generation_must_match(self):
        d=self.changed();c=self.case(d,'shared-result-normal');self.event(c,'resource.reserved')['generation']=2;self.reject(d,'reservation generation')
    def test_c1_summary_tamper(self):
        d=self.changed();self.case(d,'shared-result-normal')['observations']['first_waiter']['result']['checksum']+=1;self.reject(d,'waiter result')
    def test_c1_release_before_read_then_done_final_unchanged(self):
        d=self.changed();c=self.case(d,'shared-result-normal');self.move_before(c,'resource.released','backend.read_ended','A');self.reject(d,'causal chain')
    def test_c1_owner_release_before_done(self):
        d=self.changed();c=self.case(d,'shared-result-normal');self.move_before(c,'owner.permit_returned','producer.done');self.reject(d,'premature')
    def test_c1_lost_registry_despite_terminal_zero(self):
        d=self.changed();c=self.case(d,'shared-result-normal');self.event(c,'drain.started')['details']['obligations']='0';self.reject(d,'registry')
    def test_c1_waiter_cancel_cannot_cancel_producer(self):
        d=self.changed();c=self.case(d,'shared-result-cancel-waiter');self.event(c,'cleanup.started','A')['cancelled']=True;self.reject(d,'cancellation target')
    def test_c1_producer_cancel_does_not_cancel_waiter(self):
        d=self.changed();c=self.case(d,'shared-result-cancel-producer');self.event(c,'consumer.wait_returned','C2')['cancelled']=True;self.reject(d,'waiter cancellation')
    def test_c1_expected_error_not_unknown(self):
        d=self.changed();c=self.case(d,'shared-result-body-and-producer-error');self.event(c,'work.failed','A')['details']['error']='unknown';self.reject(d,'failure identity')
    def test_c1_drop_cannot_skip_read(self):
        d=self.changed();c=self.case(d,'shared-result-drop-cancel-caller');c['events'].remove(self.event(c,'backend.read_ended','A'));self.reject(d,'multiplicity')
    def test_c1_wrong_old_epoch_rejection(self):
        d=self.changed();c=self.case(d,'shared-result-normal');self.event(c,'owner.expired_rejected','stale-next')['details']['permit_epoch']='2';self.reject(d,'stale identity')
    def test_c2_each_pass_full_array_tamper(self):
        d=self.changed();self.case(d)['observations']['owner']['results'][0]['passes'][1]['actual_values'][32]^=1;self.reject(d,'numeric arrays')
    def test_c2_pass_checksum_tamper(self):
        d=self.changed();c=self.case(d);self.event(c,'scan.pass_completed')['details']['checksum']='0';self.reject(d,'pass numeric')
    def test_c2_partial_error_is_not_success(self):
        d=self.changed();c=self.case(d,'live-readers-body-and-scan-error');e=next(e for e in c['events'] if e['name']=='scan.access_ended' and e['details']['lease_id']=='1/C1');e['details']['outcome']='success';self.reject(d,'failure classification')
    def test_c2_error_cause_wrong_obligation(self):
        d=self.changed();c=self.case(d,'live-readers-body-and-scan-error');self.event(c,'reader.body_returned','1/C1')['details']['caused_by']='scan-999';self.reject(d,'failure identity')
    def test_c2_original_body_error_not_discarded(self):
        d=self.changed();c=self.case(d,'live-readers-body-and-scan-error');del c['observations']['scope_outcome']['failure']['body'];self.reject(d,'failure/scope')
    def test_c2_early_lease_close_even_if_final_legal(self):
        d=self.changed();c=self.case(d,'live-reader-body-drops-scan-caller');self.move_before(c,'lease.closed','scan.access_ended');self.reject(d,'active read')
    def test_c2_early_permit_return_even_if_final_legal(self):
        d=self.changed();c=self.case(d,'live-reader-body-drops-scan-caller');self.move_before(c,'owner.permit_returned','scan.access_ended');self.reject(d,'premature resource')
    def test_c2_close_cannot_erase_access_identity(self):
        d=self.changed();c=self.case(d,'live-reader-body-drops-scan-caller');self.event(c,'lease.closing')['details']['active_accesses']='0';self.reject(d,'obligation lost')
    def test_c2_scan_done_before_access_ended(self):
        d=self.changed();c=self.case(d);self.move_before(c,'scan.done','scan.access_ended');self.reject(d,'task ended before access')
    def test_c2_scan_must_be_registered(self):
        d=self.changed();c=self.case(d);c['events'].remove(self.event(c,'scan.registered'));self.reject(d,'identity')
    def test_c2_new_scan_after_scope_close(self):
        d=self.changed();c=self.case(d);e=self.event(c,'scan.registered');c['events'].remove(e);i=c['events'].index(self.event(c,'owner.draining'));c['events'].insert(i+1,e)
        for e in c['events']:e['t_ns']=1
        self.reject(d)
    def test_c2_frozen_registry_cannot_forget_scan(self):
        d=self.changed();c=self.case(d);self.event(c,'owner.draining')['details']['scan_ids']='';self.reject(d,'scan registry')
    def test_c2_owner_identity_checked(self):
        d=self.changed();c=self.case(d);self.event(c,'scan.access_started')['details']['owner_id']='another-owner';self.reject(d,'owner mutation')
    def test_c2_generation_checked(self):
        d=self.changed();c=self.case(d);self.event(c,'scan.pass_completed')['details']['generation']='2';self.reject(d,'epoch/generation')
    def test_c2_allocation_must_stay_same(self):
        d=self.changed();c=self.case(d);self.event(c,'producer.access_started','B')['details']['resource_id']='new-buffer';self.reject(d,'same allocation')
    def test_c2_foreign_probe_must_not_register_real_scan(self):
        d=self.changed();c=self.case(d);c['observations']['foreign_owner']['registered_scan_tasks']=1;self.reject(d,'foreign owner')
    def test_c2_closing_rejection_wrong_reason(self):
        d=self.changed();c=self.case(d);e=next(e for e in c['events'] if e['name']=='scan.rejected' and e['details']['reason']=='closing');e['details']['reason']='closedLease';self.reject(d,'rejection reason')
    def test_c2_stale_rejection_cannot_disappear_with_snapshot(self):
        d=self.changed();c=self.case(d);c['events'].pop();c['observations']['owner']['rejections'].pop();self.reject(d,'actual rejection chain')
    def test_c2_anchor_snapshot_cannot_hide_reader_access(self):
        d=self.changed();c=self.case(d);c['observations']['phases']['owner_draining']['ledger']['active_accesses']=0;self.reject(d,'phase snapshots')
    def test_c2_cancel_reader_does_not_cancel_scan(self):
        d=self.changed();c=self.case(d,'live-readers-cancel-reader');self.event(c,'scan.access_ended')['cancelled']=True;self.reject(d,'leaked')
    def test_c2_cancel_scope_does_not_skip_drain(self):
        d=self.changed();c=self.case(d,'live-readers-cancel-scope');self.event(c,'owner.permit_returned','A')['cancelled']=False;self.reject(d,'scope cancellation')
    def test_c2_failed_producer_cannot_claim_ready(self):
        d=self.changed();c=self.case(d,'live-producer-failure-after-access');self.event(c,'producer.failed_safe')['name']='producer.ready';self.reject(d,'before cleanup')
    def test_c2_producer_failure_must_have_ended_write(self):
        d=self.changed();c=self.case(d,'live-producer-failure-after-access');self.move_before(c,'producer.failed_safe','producer.write_ended');self.reject(d,'before ended write')
    def test_c2_detached_wrapper_must_finish_before_observation(self):
        d=self.changed();c=self.case(d,'live-reader-body-drops-scan-caller');self.move_before(c,'observer.wrapper_observed_done','detached_call_wrapper.done');self.reject(d,'wrapper observation')
    def artifact_copy(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);root=Path(t.name);a=root/'attempt';r=root/'record';shutil.copytree(ATTEMPT,a);shutil.copytree(RECORD,r)
        for p in root.rglob('*'):p.chmod(0o755 if p.is_dir() else 0o644)
        return a,r
    def test_portable_copies_do_not_read_historical_paths(self):
        a,r=self.artifact_copy();self.assertTrue(audit.analyze(a,r)['passed'])
    def test_frozen_source_tamper(self):
        a,r=self.artifact_copy();(a/'source/LiveOwner.swift').write_text('// altered')
        with self.assertRaisesRegex(audit.Invalid,'frozen'):audit.analyze(a,r)
    def test_frozen_dependency_tamper(self):
        a,r=self.artifact_copy();(a/'dependencies/CPUOwner.swift').write_text('// altered')
        with self.assertRaisesRegex(audit.Invalid,'frozen'):audit.analyze(a,r)
    def test_wrong_binary_hash(self):
        a,r=self.artifact_copy();(a/'build/swift-owner-composition').write_bytes(b'wrong')
        with self.assertRaisesRegex(audit.Invalid,'binary hash'):audit.analyze(a,r)
    def test_run_command_must_bind_snapshot(self):
        a,r=self.artifact_copy();p=r/'run.json';x=json.loads(p.read_text());x['command'][0]='/live/unfrozen';p.write_text(json.dumps(x))
        with self.assertRaisesRegex(audit.Invalid,'binary command'):audit.analyze(a,r)
    def test_run_timeout_not_success(self):
        a,r=self.artifact_copy();p=r/'run.json';x=json.loads(p.read_text());x['timed_out']=True;p.write_text(json.dumps(x))
        with self.assertRaisesRegex(audit.Invalid,'timeout'):audit.analyze(a,r)
    def test_run_output_hash_tamper(self):
        a,r=self.artifact_copy();p=r/'stdout.json';p.write_text(p.read_text()+'\n')
        with self.assertRaisesRegex(audit.Invalid,'output hash'):audit.analyze(a,r)
if __name__=='__main__':unittest.main()
