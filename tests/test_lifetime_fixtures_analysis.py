"""CPU-only mutations of C4b's real frozen evidence; never invokes Swift/MLX."""
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
import analyze_lifetime_fixtures as check

BASE=Path(os.environ.get('LIFETIME_FIXTURES_ROOT',Path(__file__).resolve().parents[1]/'experiments/swift-lifetime-fixtures')).resolve()
ATTEMPT=BASE/'attempts/attempt-2'
OUTER=BASE/'verification/execute-attempt-2'
PROBE=BASE/'probes/array-explicit-1'
PROBE_OUTER=BASE/'verification/execute-array-explicit-1'

def dump(path,value):path.write_text(json.dumps(value,indent=2)+'\n')
def runtime(name):return check.read(ATTEMPT/'fixtures'/name/'execution/stdout')
def event(data,name):return next(e for e in data['events'] if e['name']==name)
def restamp(data):
    for i,e in enumerate(data['events']):e['t_ns']=i

class NumericAndProtocolTests(unittest.TestCase):
    def reject_b1(self,name,mutate):
        data=runtime(name);mutate(data)
        with self.assertRaises((check.InvalidEvidence,KeyError,ValueError)):check.validate_b1(name,data)

    def reject_b2(self,name,mutate):
        data=runtime(name);mutate(data)
        with self.assertRaises((check.InvalidEvidence,KeyError,ValueError)):check.validate_b2(name,data)

    def test_all_eight_actual_runtime_records(self):
        for name in check.RUN_B1:
            with self.subTest(name=name):check.validate_b1(name,runtime(name))
        for name in check.RUN_B2:
            with self.subTest(name=name):check.validate_b2(name,runtime(name))

    def test_full_element_damage_even_with_unchanged_checksum(self):
        self.reject_b1('dynamic_two_readers',lambda d:d['owner']['results'][0]['passes'][1]['actual_values'].__setitem__(57,0))

    def test_wrong_producer_generation(self):
        self.reject_b1('dynamic_two_readers',lambda d:d['owner']['producer_results'][1].__setitem__('generation',1))

    def test_duplicate_result_delivery(self):
        self.reject_b1('dynamic_two_readers',lambda d:d['values'].__setitem__(1,copy.deepcopy(d['values'][0])))

    def test_cross_owner_event(self):
        self.reject_b1('dynamic_two_readers',lambda d:event(d,'scan.access_started')['details'].__setitem__('owner_id','live-owner-other'))

    def test_cross_lease_scan(self):
        def change(d):
            e=event(d,'scan.pass_completed');e['details']['lease_id']='1/C2' if e['details']['lease_id']=='1/C1' else '1/C1'
        self.reject_b1('dynamic_two_readers',change)

    def test_missing_registered_scan_drain(self):
        self.reject_b1('dynamic_two_readers',lambda d:event(d,'owner.draining')['details'].__setitem__('scan_ids',''))

    def test_scan_not_completed_before_permit_return(self):
        self.reject_b1('dynamic_two_readers',lambda d:d.__setitem__('events',[e for e in d['events'] if e['name']!='scan.done']))

    def test_lease_close_with_active_access(self):
        def change(d):
            closed=event(d,'lease.closed');d['events'].remove(closed)
            d['events'].insert(d['events'].index(event(d,'scan.access_started'))+1,closed);restamp(d)
        self.reject_b1('dynamic_close_with_active_scan',change)

    def test_duplicate_producer_registration(self):
        def change(d):
            e=event(d,'producer.registered');d['events'].insert(d['events'].index(e)+1,copy.deepcopy(e));restamp(d)
        self.reject_b1('dynamic_two_readers',change)

    def test_new_unknown_event(self):
        def change(d):
            e=copy.deepcopy(event(d,'producer.done'));e['name']='producer.unknown';d['events'].insert(d['events'].index(event(d,'producer.done'))+1,e);restamp(d)
        self.reject_b1('dynamic_two_readers',change)

    def test_final_active_access_not_zero(self):
        self.reject_b1('dynamic_two_readers',lambda d:d['owner']['ledger'].__setitem__('active_accesses',1))

    def test_busy_rejection_is_actual_draining_phase(self):
        self.reject_b1('dynamic_close_with_active_scan',lambda d:event(d,'owner.busy_rejected')['details'].__setitem__('phase','idle'))

    def test_stale_generation_rejection_not_relabelled_closed(self):
        def change(d):
            e=[e for e in d['events'] if e['name']=='scan.rejected'][-1];e['details']['reason']='closing'
        self.reject_b1('dynamic_escaped_lease',change)

    def test_b2_full_element_damage(self):
        self.reject_b2('value_local_read',lambda d:d['scans'][0]['actual_passes'][1].__setitem__(82,0))

    def test_b2_raw_pass_counter(self):
        self.reject_b2('value_local_read',lambda d:event(d,'span.pass_completed')['details'].__setitem__('visits','0'))

    def test_b2_event_request_identity(self):
        self.reject_b2('value_local_read',lambda d:event(d,'span.scan_ended').__setitem__('request_id','wrong-owner'))

    def test_await_bracketing_missing(self):
        self.reject_b2('value_across_await',lambda d:d.__setitem__('events',[e for e in d['events'] if e['name']!='value.after_await']))

    def test_b2_overwrite_before_read_finished(self):
        def change(d):
            e=event(d,'value.overwrite_started');d['events'].remove(e);d['events'].insert(1,e);restamp(d)
        self.reject_b2('value_summary_then_overwrite',change)

    def test_diagnostic_duplicates_and_notes_not_conflated(self):
        text=(ATTEMPT/'fixtures/value_callback_local_dependence/compile/stderr').read_text()
        self.assertEqual(check.diagnostic_messages(text),['lifetime-dependent value escapes its scope']*2)
        self.assertIn('closure capture; this is not yet supported',text)

class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        cls.attempt=cls.root/'attempt';cls.outer=cls.root/'outer';cls.probe=cls.root/'probe';cls.probe_outer=cls.root/'probe-outer'
        for source,dest in [(ATTEMPT,cls.attempt),(OUTER,cls.outer),(PROBE,cls.probe),(PROBE_OUTER,cls.probe_outer)]:
            shutil.copytree(source,dest)
            # Only disposable copies are writable; frozen originals stay intact.
            for p in dest.rglob('*'):
                if p.is_file():p.chmod(p.stat().st_mode|0o200)

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def setUp(self):self.saved={}
    def tearDown(self):
        for p,value in self.saved.items():p.write_bytes(value)

    def replace(self,path,value):
        self.saved.setdefault(path,path.read_bytes());dump(path,value)

    def refresh_report(self,report,probe=False):
        root=self.probe if probe else self.attempt;outer=self.probe_outer if probe else self.outer
        self.replace(root/'report.json',report);self.replace(outer/'stdout.json',report)
        run=check.read(outer/'run.json');run['files']['stdout.json']=check.ident(outer/'stdout.json');self.replace(outer/'run.json',run)

    def reject_attempt(self):
        with self.assertRaises((check.InvalidEvidence,KeyError,ValueError)):check.audit_attempt(self.attempt,self.outer)

    def reject_probe(self):
        with self.assertRaises((check.InvalidEvidence,KeyError,ValueError)):check.audit_array_probe(self.probe,self.probe_outer,self.attempt)

    def mutate_stage(self,name,mutate):
        report=check.read(self.attempt/'report.json');stage=next(s for s in report['stages'] if s['name']==name);mutate(stage)
        # Keep all redundant writer records consistent; the checker must reject
        # the substantive contract violation, not merely a stale writer copy.
        self.replace(self.attempt/name/'run.json',stage)
        self.replace(self.attempt/name/'started.json',{'command':stage['command'],'inputs_before':stage['inputs_before']})
        for f in report['fixtures']:
            for key in ('compile','execution'):
                if f.get(key,{}).get('name')==name:f[key]=copy.deepcopy(stage)
        self.refresh_report(report)

    def test_actual_attempt_and_probe_preserve_failed_control(self):
        a=check.audit_attempt(self.attempt,self.outer);p=check.audit_array_probe(self.probe,self.probe_outer,self.attempt)
        self.assertTrue(a['evidence_integrity_passed']);self.assertFalse(a['passed'])
        self.assertEqual(a['original_exit_code'],1);self.assertEqual(a['required_control_mismatches'],['value_store_in_array'])
        self.assertTrue(p['evidence_integrity_passed']);self.assertFalse(p['compiled']);self.assertFalse(p['client_executed'])

    def test_cannot_rewrite_original_control_success(self):
        report=check.read(self.attempt/'report.json');report['required_controls_passed']=True;self.refresh_report(report);self.reject_attempt()

    def test_cannot_rewrite_outer_exit1(self):
        run=check.read(self.outer/'run.json');run['returncode']=0;self.replace(self.outer/'run.json',run);self.reject_attempt()

    def test_required_module_input_cannot_be_omitted_consistently(self):
        self.mutate_stage('fixtures/value_local_read/compile',lambda s:s['inputs_before'].pop('build/LifetimeValues.swiftmodule'));self.reject_attempt()

    def test_feature_flag_cannot_be_removed_consistently(self):
        def change(s):
            pos=s['command'].index('-enable-experimental-feature');del s['command'][pos:pos+2]
        self.mutate_stage('fixtures/value_local_read/compile',change);self.reject_attempt()

    def test_missing_outer_log_inventory(self):
        run=check.read(self.outer/'run.json');run['files'].pop('stderr.log');self.replace(self.outer/'run.json',run);self.reject_attempt()

    def test_unexpected_error_not_hidden_by_report(self):
        name='fixtures/value_store_in_array/compile';path=self.attempt/name/'stderr'
        self.saved[path]=path.read_bytes();path.write_text(path.read_text()+'\n/tmp/client.swift:9:2: error: unknown compiler failure\n')
        self.mutate_stage(name,lambda s:s['files'].__setitem__('stderr',check.ident(path)));self.reject_attempt()

    def test_probe_cannot_claim_runtime(self):
        report=check.read(self.probe/'report.json');report['swift_client_executed']=True;self.refresh_report(report,True);self.reject_probe()

    def test_probe_extra_unknown_error(self):
        path=self.probe/'stderr';self.saved[path]=path.read_bytes();path.write_text(path.read_text()+'\n/tmp/client.swift:9:2: error: unrelated failure\n')
        stage=check.read(self.probe/'compile.json');stage['files']['stderr']=check.ident(path);self.replace(self.probe/'compile.json',stage)
        report=check.read(self.probe/'report.json');report['compile']=stage;self.refresh_report(report,True);self.reject_probe()

    def test_probe_origin_record_mismatch(self):
        manifest=check.read(self.probe/'manifest.json');manifest['files']['modules/LifetimeValues.swiftmodule']['original_stage_record_sha256']='0'*64
        # The top manifest hash is updated too; the originating stage remains
        # the independently checked source of the module artifact.
        self.replace(self.probe/'manifest.json',manifest);digest=check.sha(self.probe/'manifest.json')
        prepared=check.read(self.probe/'prepared.json');prepared['manifest_sha256']=digest;prepared['command'][3]=digest;self.replace(self.probe/'prepared.json',prepared)
        pin=check.read(self.probe/'execution-pin.json');pin['manifest_sha256']=digest;pin['argv'][2]=digest
        pin['used_module_inputs']['modules/LifetimeValues.swiftmodule']=manifest['files']['modules/LifetimeValues.swiftmodule'];self.replace(self.probe/'execution-pin.json',pin)
        report=check.read(self.probe/'report.json');report['provenance']=pin;self.refresh_report(report,True);self.reject_probe()

    def test_output_refuses_overwrite(self):
        p=self.root/'existing-output';p.write_text('keep')
        try:
            with patch.object(sys,'argv',['check','--attempt',str(self.attempt),'--outer',str(self.outer),'--output',str(p)]):
                with self.assertRaises(FileExistsError):check.main()
            self.assertEqual(p.read_text(),'keep')
        finally:p.unlink()

if __name__=='__main__':unittest.main()
