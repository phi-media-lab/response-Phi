"""Mutate only disposable copies of real C3 CPU evidence; no Swift/GPU work."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import analyze_nested_obligations as audit

BASE=Path(os.environ.get('NESTED_OBLIGATIONS_ROOT',Path(__file__).resolve().parents[1]/'experiments/swift-nested-obligations'))
ATTEMPT=BASE/'attempts/attempt-2';OUTER=BASE/'verification/execute-attempt-2'
def case(name):return audit.load(ATTEMPT/'cases'/name/'stdout')
def find(data,name):return next(e for e in data['composition_events'] if e['name']==name)
def restamp(data):
    for index,event in enumerate(data['composition_events']):event['index']=index;event['t_ns']=index

class ProtocolTests(unittest.TestCase):
    def reject(self,name,change):
        data=case(name);change(data)
        with self.assertRaises((ValueError,KeyError,IndexError)):audit.audit_case(data)

    def test_all_thirteen_actual_cases(self):
        for name in audit.CASES:
            with self.subTest(name=name):self.assertTrue(audit.audit_case(case(name))['passed'])

    def test_full_array_damage_despite_same_checksum(self):
        def change(d):d['owners'][0]['copied_reads']['X.A'][77]^=1
        self.reject('drop-child',change)

    def test_scan_visits_not_self_passed(self):
        def change(d):d['owners'][0]['snapshot']['sessions'][0]['final_session']['results'][0]['visits']=128
        self.reject('drop-child',change)

    def test_business_failure_cannot_be_promoted(self):
        self.reject('five-faults',lambda d:d.__setitem__('execution_succeeded',True))

    def test_unknown_fault_cannot_be_expected_injection(self):
        self.reject('five-faults',lambda d:d['outcomes'][0]['faults'][0].__setitem__('kind','unknown_failure'))

    def test_same_label_wrong_original_invocation(self):
        def change(d):d['outcomes'][0]['faults'][1]['invocation_id']=d['outcomes'][0]['invocation_id']
        self.reject('five-faults',change)

    def test_original_producer_channels_both_retained(self):
        self.reject('local-awaited-producer-fault',lambda d:d['outcomes'][0]['faults'][0].__setitem__('reported_via',['DrainFailure.cleanupErrors[0]']))

    def test_cross_run_error_keeps_producer_identity(self):
        def change(d):d['outcomes'][1]['faults'][0]['invocation_id']=d['outcomes'][1]['invocation_id']
        self.reject('cross-run-local-fault',change)

    def test_cross_run_must_be_same_completion(self):
        self.reject('cross-run-local-fault',lambda d:find(d,'harness.completion_transferred')['details'].__setitem__('completion_id','other'))

    def test_failed_producer_has_no_legitimate_copied_result(self):
        self.reject('local-awaited-producer-fault',lambda d:d['owners'][0]['copied_reads'].__setitem__('X.A',[0]*128))

    def test_no_release_before_access_ended(self):
        def change(d):
            ev=d['owners'][0]['events'];release=next(e for e in ev if e['name']=='resource.released');ev.remove(release)
            ev.insert(next(i for i,e in enumerate(ev) if e['name']=='backend.physical_write_ended'),release)
            for i,e in enumerate(ev):e['t_ns']=i
        self.reject('drop-child',change)

    def test_child_join_cannot_be_omitted(self):
        def change(d):
            d['composition_events']=[e for e in d['composition_events'] if e['name']!='child.join_returned'];restamp(d)
        self.reject('drop-child',change)

    def test_start_gate_before_observable_child(self):
        def change(d):
            ev=d['composition_events'];e=find(d,'child.started');ev.remove(e);ev.insert(ev.index(find(d,'child.start_gate_opening')),e);restamp(d)
        self.reject('drop-child',change)

    def test_equal_count_cannot_replace_child_identity(self):
        self.reject('drop-child',lambda d:d['outcomes'][0]['registry'].__setitem__('joined_ids',['other-child']))

    def test_detached_not_silently_unstructured(self):
        self.reject('drop-child',lambda d:find(d,'child.registered')['details'].__setitem__('launch_mode','unstructured'))

    def test_child_not_cancelled_by_parent_flag(self):
        def change(d):
            next(e for e in d['composition_events'] if e['name']=='scope.owner_returned' and e['scope_id']=='Y')['cancelled']=True
        self.reject('cancel-parent',change)

    def test_callback_cannot_be_reported_as_actual_cancellation_sample(self):
        def change(d):
            next(e for o in d['owners'] for e in o['events'] if e['name']=='producer.escalated')['cancelled']=False
        self.reject('cancel-parent',change)

    def test_busy_rejection_is_not_unknown_access(self):
        self.reject('registered-child-busy',lambda d:d['outcomes'][0]['registry']['children'][0].__setitem__('phase','unresolved'))

    def test_busy_rejection_does_not_return_a_fictional_lease(self):
        self.reject('same-owner-busy',lambda d:d['outcomes'][0].__setitem__('owner_safely_returned',True))

    def test_reused_owner_uses_original_epoch_history(self):
        self.reject('return-observation-reuse',lambda d:d['outcomes'][0]['owner_return_evidence'].__setitem__('captured_epoch',2))

    def test_checkpoint_exact_event_not_last_racy_event(self):
        self.reject('drop-child',lambda d:d['checkpoints'][0].__setitem__('event_index',d['checkpoints'][0]['event_index']+1))

    def test_late_owner_must_have_zero_actual_work(self):
        def change(d):
            late=next(o for o in d['owners'] if o['owner_id']=='Late');late['snapshot']['leases_granted']=1
        self.reject('close-before-register',change)

    def test_failed_child_cannot_hide_pending_sibling(self):
        def change(d):
            p=next(c for c in d['checkpoints'] if c['label']=='Y-ended-Z-still-pending')
            next(o for o in p['owners'] if o['owner_id']=='Z')['snapshot']['leases_returned']=1
        self.reject('cancel-failed-sibling',change)

    def test_all_ledger_records_and_unknown_scope(self):
        result=audit.audit_ledger(audit.load(ATTEMPT/'ledger/stdout'))
        self.assertEqual(result['snapshot_transitions_independently_checked'],10)
        self.assertEqual(len(result['boolean_only_self_reports']),3)
        self.assertFalse(result['device_or_actual_owner_unknown_completion_tested'])

    def test_unknown_ledger_cannot_be_drained(self):
        d=audit.load(ATTEMPT/'ledger/stdout');next(c for c in d['checks'] if c['name']=='unknown-child-is-not-safe-owner')['after']['fully_drained']=True
        with self.assertRaises(ValueError):audit.audit_ledger(d)

    def test_ledger_failure_not_washed(self):
        d=audit.load(ATTEMPT/'ledger/stdout');d['checks'][0]['passed']=False
        with self.assertRaises(ValueError):audit.audit_ledger(d)

class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();root=Path(cls.temp.name);cls.attempt=root/'attempt';cls.outer=root/'outer'
        for src,dest in [(ATTEMPT,cls.attempt),(OUTER,cls.outer)]:
            shutil.copytree(src,dest)
            for p in dest.rglob('*'):
                if p.is_file():p.chmod(p.stat().st_mode|0o200)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def setUp(self):self.changed={}
    def tearDown(self):
        for p,b in self.changed.items():p.write_bytes(b)
    def write(self,path,data):
        self.changed.setdefault(path,path.read_bytes());path.write_text(json.dumps(data,indent=2)+'\n')
    def refresh(self,report):
        self.write(self.attempt/'report.json',report);self.write(self.outer/'stdout.json',report)
        r=audit.load(self.outer/'run.json');r['files']['stdout.json']=audit.identity(self.outer/'stdout.json');self.write(self.outer/'run.json',r)
    def reject(self):
        with self.assertRaises((ValueError,KeyError,IndexError)):audit.artifact_audit(self.attempt,self.outer)

    def test_complete_actual_artifact_chain(self):self.assertTrue(audit.artifact_audit(self.attempt,self.outer)['passed'])

    def test_missing_actual_input_rejected_consistently(self):
        r=audit.load(self.attempt/'report.json');s=next(s for s in r['stages'] if s['name']=='compile-client')
        s['inputs_before'].pop('build/ObligationCore.swiftmodule')
        self.write(self.attempt/'compile-client/run.json',{k:v for k,v in s.items() if k!='name'});self.write(self.attempt/'compile-client/started.json',dict(command=s['command'],inputs_before=s['inputs_before']));self.refresh(r);self.reject()

    def test_original_failed_case_summary_not_promoted(self):
        r=audit.load(self.attempt/'report.json');next(c for c in r['cases'] if c['name']=='five-faults')['execution_succeeded']=True
        self.refresh(r);self.reject()

    def test_timeout_not_accepted_as_collected(self):
        r=audit.load(self.outer/'run.json');r['timed_out']=True;self.write(self.outer/'run.json',r);self.reject()

    def test_raw_log_identity_checked(self):
        p=self.attempt/'cases/drop-child/stdout';self.changed[p]=p.read_bytes();p.write_bytes(p.read_bytes()+b' ');self.reject()

    def test_unknown_additional_compiler_diagnostic_rejected(self):
        p=self.attempt/'compiler-version/stderr';self.changed[p]=p.read_bytes();p.write_bytes(p.read_bytes()+b'\nunknown compiler error\n')
        r=audit.load(self.attempt/'report.json');s=r['stages'][0];s['logs']['stderr']=audit.identity(p)
        self.write(self.attempt/'compiler-version/run.json',{k:v for k,v in s.items() if k!='name'});self.refresh(r);self.reject()

    def test_no_output_overwrite(self):
        p=Path(self.temp.name)/'existing';p.write_text('keep')
        try:
            with patch.object(sys,'argv',['audit','--attempt',str(self.attempt),'--outer',str(self.outer),'--output',str(p)]):
                with self.assertRaises(FileExistsError):audit.main()
            self.assertEqual(p.read_text(),'keep')
        finally:p.unlink()

if __name__=='__main__':unittest.main()
