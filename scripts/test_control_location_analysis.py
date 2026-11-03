"""Adversarial audits against saved real Metal traces; never launches a device."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import hashlib
import shutil
from analyze_control_location import analyze_document, audit_artifacts, strict_json
from control_math import array_hash, reference, verify_reference_document

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT/'results/batch4/control-smoke'


class ControlAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = {mode: strict_json(FIXTURES/mode/'stdout.json') for mode in
                         ('gpu-step','gpu-preencoded','cpu-feedback','device-loop-global')}

    def document(self, mode='gpu-step'):
        return copy.deepcopy(self.documents[mode])

    def reject(self, mutation, mode='gpu-step'):
        doc = self.document(mode)
        mutation(doc)
        with self.assertRaises((ValueError, KeyError, TypeError, IndexError)):
            analyze_document(doc)

    def test_all_four_real_traces(self):
        for mode, doc in self.documents.items():
            with self.subTest(mode=mode):
                self.assertEqual(len(analyze_document(doc)), 1)
                self.assertTrue(audit_artifacts(FIXTURES/mode/'stdout.json',doc)['passed'])

    def test_full_array_corruption_even_with_matching_self_hashes(self):
        def mutate(d):
            t = d['trials'][0]; t['actual']['score_history'][57] += 1
            h = array_hash(t['actual']['score_history'])
            t['actual_sha256']['score_history'] = t['expected_sha256']['score_history'] = h
        self.reject(mutate)

    def test_wrong_input_with_matching_digest(self):
        def mutate(d):
            d['inputs']['features'][512] += 1
            d['inputs']['features_sha256'] = array_hash(d['inputs']['features'])
        self.reject(mutate)

    def test_boolean_array_rejected(self):
        self.reject(lambda d: d['trials'][0]['actual']['final_visits'].__setitem__(0, False))

    def test_failed_top_level(self):
        self.reject(lambda d: d.__setitem__('passed',False))

    def test_failed_trial(self):
        self.reject(lambda d: d['trials'][0].__setitem__('passed',False))

    def test_missing_trial(self):
        self.reject(lambda d: d.__setitem__('trials',[]))

    def test_false_self_check(self):
        self.reject(lambda d: d['trials'][0]['checks'][0].__setitem__('passed',False))

    def test_metric_inflation(self):
        self.reject(lambda d: d['trials'][0]['metrics'].__setitem__('work_ms',0.0))

    def test_nan_metric(self):
        self.reject(lambda d: d['trials'][0]['metrics'].__setitem__('work_ms',float('nan')))

    def test_missing_command(self):
        self.reject(lambda d: d['trials'][0]['commands'].pop())

    def test_wrong_per_dispatch_step(self):
        self.reject(lambda d: d['trials'][0]['commands'][0]['dispatches'][4]['parameters'].__setitem__('step',15), 'gpu-preencoded')

    def test_wrong_per_dispatch_penalty(self):
        self.reject(lambda d: d['trials'][0]['commands'][0]['dispatches'][0]['parameters'].__setitem__('penalty',31))

    def test_wrong_command_stage(self):
        self.reject(lambda d: d['trials'][0]['commands'][0].__setitem__('stage','loop'))

    def test_gpu_interval_outside_callback(self):
        def mutate(d):
            c = d['trials'][0]['commands'][0]
            c['gpu_end_seconds'] = c['callback_mach_seconds']+1
        self.reject(mutate)

    def test_observation_counter_wrong(self):
        self.reject(lambda d: d['trials'][0]['observations'].__setitem__('completed_dot_elements',1))

    def test_resource_still_owned(self):
        self.reject(lambda d: d['trials'][0]['observations'].__setitem__('final_owned',1))

    def test_release_before_validation(self):
        def mutate(d):
            es=d['trials'][0]['events']
            a=next(e for e in es if e['name']=='resource.released')
            b=next(e for e in es if e['name']=='validation.started')
            a['name'],b['name']=b['name'],a['name']
            a['details'],b['details']=b['details'],a['details']
        self.reject(mutate)

    def test_extra_event(self):
        def mutate(d):
            es=d['trials'][0]['events']; e=copy.deepcopy(es[-1]);e['name']='fabricated.gpu.stopped';es.append(e)
        self.reject(mutate)

    def test_duplicate_completion(self):
        def mutate(d):
            es=d['trials'][0]['events'];i=next(i for i,e in enumerate(es) if e['name']=='completion.observed');es.insert(i,copy.deepcopy(es[i]))
        self.reject(mutate)

    def test_host_selection_before_score_completed(self):
        def mutate(d):
            es=d['trials'][0]['events']
            e=next(e for e in es if e['name']=='host.selection_started');e['t_ms']=0
        self.reject(mutate,'cpu-feedback')

    def test_wrong_cpu_choice(self):
        def mutate(d):
            e=next(e for e in d['trials'][0]['events'] if e['name']=='host.selection_ended');e['details']['winner']='0'
        self.reject(mutate,'cpu-feedback')

    def test_fake_cancel_classification(self):
        self.reject(lambda d: d['trials'][0]['observations'].__setitem__('cancellation_phase_first_command','running'))

    def test_wrong_build_binding(self):
        d=self.document();d['provenance']['binary_sha256']='0'*64
        with self.assertRaises(ValueError):audit_artifacts(FIXTURES/'gpu-step/stdout.json',d)

    def test_wrong_reference_binding(self):
        d=self.document();d['reference_file_sha256']='0'*64
        with self.assertRaises(ValueError):audit_artifacts(FIXTURES/'gpu-step/stdout.json',d)

    def test_raw_output_tamper_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            for name in ('stdout.json','stderr.log','run.json'):
                (p/name).write_bytes((FIXTURES/'gpu-step'/name).read_bytes())
            with (p/'stdout.json').open('a') as f:f.write(' ')
            with self.assertRaises(ValueError):audit_artifacts(p/'stdout.json',self.documents['gpu-step'])

    def test_strict_json_duplicate_and_nan(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'bad.json'
            for text in ('{"a":1,"a":2}', '{"a":NaN}'):
                p.write_text(text)
                with self.assertRaises(ValueError):strict_json(p)

    def test_reference_all_rounds_required(self):
        ref=strict_json(ROOT/'experiments/metal-control-location/verification/reference-16/stdout.json')
        expected=reference(16,7,1)
        ref['reference']=dict(expected['actual'],sha256=expected['sha256'])
        ref['nondegenerate']=expected['nondegenerate']
        with self.assertRaises(ValueError):verify_reference_document(ref)

    def test_wrong_reference_schema(self):
        ref=strict_json(ROOT/'experiments/metal-control-location/verification/reference-16/stdout.json');ref['schema_version']=2
        with self.assertRaises(ValueError):verify_reference_document(ref)

    def cancelled(self, policy='after-first-commit', mode='gpu-step'):
        return strict_json(ROOT/'results/batch4/control-cancel-smoke'/policy/mode/'stdout.json')

    def test_all_eight_real_cancel_traces(self):
        for policy in ('before-submit','after-first-commit'):
            for mode in self.documents:
                with self.subTest(policy=policy,mode=mode):
                    self.assertTrue(analyze_document(self.cancelled(policy,mode))[0]['passed'])

    def test_cancel_phase_must_match_gpu_interval(self):
        doc=self.cancelled();doc['trials'][0]['observations']['cancellation_phase_first_command']='running'
        self.assertEqual(self.cancelled()['trials'][0]['observations']['cancellation_phase_first_command'],'queued')
        with self.assertRaises(ValueError):analyze_document(doc)

    def test_cancel_timestamp_must_share_clock(self):
        doc=self.cancelled();doc['trials'][0]['observations']['cancel_mach_seconds']+=1
        with self.assertRaises(ValueError):analyze_document(doc)

    def test_cancel_tail_cannot_be_overwritten(self):
        doc=self.cancelled();t=doc['trials'][0]
        t['actual']['context_history'][128]=0
        t['actual_sha256']['context_history']=t['expected_sha256']['context_history']=array_hash(t['actual']['context_history'])
        with self.assertRaises(ValueError):analyze_document(doc)

    def test_preencoded_cannot_claim_one_completed_round(self):
        doc=self.cancelled(mode='gpu-preencoded');t=doc['trials'][0]
        expected=reference(16,7,1)
        t['actual']=copy.deepcopy(expected['actual']);t['actual_sha256']=t['expected_sha256']=expected['sha256']
        with self.assertRaises(ValueError):analyze_document(doc)

    def test_no_submission_cannot_claim_a_command(self):
        doc=self.cancelled('before-submit')
        doc['trials'][0]['commands']=[copy.deepcopy(self.documents['gpu-step']['trials'][0]['commands'][0])]
        with self.assertRaises(ValueError):analyze_document(doc)

    def test_cancel_ack_missing(self):
        doc=self.cancelled();t=doc['trials'][0]
        t['events']=[e for e in t['events'] if e['name']!='cancel.accepted']
        with self.assertRaises(ValueError):analyze_document(doc)

    def test_required_source_cannot_disappear_from_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'build'
            doc=self.document()
            shutil.copytree(Path(doc['provenance']['source_root']).parent,target)
            build=strict_json(target/'build.json')
            del build['source_sha256']['Main.swift']
            (target/'source/Main.swift').write_text('// unbound tampered source\n')
            (target/'build.json').write_text(json.dumps(build))
            doc['provenance']['build_manifest_sha256']=hashlib.sha256((target/'build.json').read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError,'required frozen source missing'):
                audit_artifacts(FIXTURES/'gpu-step/stdout.json',doc,build_dir=target)

    def test_sibling_input_must_be_recorded_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)
            for name in ('stdout.json','stderr.log','run.json'):
                shutil.copy2(FIXTURES/'gpu-step'/name,target/name)
            doc=self.document()
            doc['provenance']['workload_scope']='unrecorded modification'
            (target/'unrecorded.json').write_text(json.dumps(doc))
            analyze_document(doc)
            with self.assertRaisesRegex(ValueError,'not recorded stdout'):
                audit_artifacts(target/'unrecorded.json',doc)

    def test_in_memory_document_must_match_input(self):
        doc=self.document()
        doc['provenance']['workload_scope']='in-memory modification'
        with self.assertRaisesRegex(ValueError,'document differs'):
            audit_artifacts(FIXTURES/'gpu-step/stdout.json',doc)


if __name__=='__main__':unittest.main()
