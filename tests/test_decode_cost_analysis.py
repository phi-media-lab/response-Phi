"""Mutations of recorded real smoke; no runner/protocol/MLX/model imports."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('independent_decode_cost', ROOT/'scripts/analyze_decode_cost.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
FIXTURES = Path(os.environ.get('DECODE_COST_FIXTURE_ROOT', str(ROOT/'results/batch5/decode-cost-smoke-v5')))
ATTEMPT_MAP = Path(os.environ['DECODE_COST_ATTEMPT_MAP']) if os.environ.get('DECODE_COST_ATTEMPT_MAP') else None


def attempt_for(policy):
    if ATTEMPT_MAP is None: return FIXTURES/policy
    mapping=json.loads(ATTEMPT_MAP.read_text());path=Path(mapping[policy])
    return path if path.is_absolute() else ATTEMPT_MAP.parent/path


class CostArithmeticTests(unittest.TestCase):
    def test_integer_predictions_candidates_and_nominal_budget(self):
        cases = [(None,1024,32,'bootstrap'),(1,1024,512,'largest_predicted_fit'),
                 (1_000_000,1024,128,'largest_predicted_fit'),(2_000_000,1024,64,'largest_predicted_fit'),
                 (6_000_000,1024,32,'minimum_progress_fallback'),(6_000_000,7,7,'largest_predicted_fit')]
        for rho,remaining,wanted,reason in cases:
            with self.subTest(rho=rho,remaining=remaining):
                row=audit.decision_for('cost-200',remaining,rho,3,19)
                self.assertEqual((row['selected_tokens'],row['reason']),(wanted,reason))
                if rho is not None:
                    for prediction in row['candidate_predictions']:
                        numerator=5*rho*prediction['tokens']
                        self.assertEqual(prediction['predicted_ns'],numerator//4+int(numerator%4!=0))

    def test_selected_tail_is_deduplicated_not_dropped(self):
        row=audit.decision_for('cost-200',33,1,1,4)
        self.assertEqual([p['tokens'] for p in row['candidate_predictions']],[32,33])
        self.assertEqual(row['selected_tokens'],33)

    def test_boolean_is_not_an_integer_counter(self):
        self.assertFalse(audit.integer(True));self.assertFalse(audit.exact(True,1))


class GeneratedSourceTests(unittest.TestCase):
    """Static installed/captured source bytes only; never import Torch."""
    @classmethod
    def setUpClass(cls):
        sources=attempt_for('cost-200')/'payload/generated-runtime/sources'
        if sources.is_dir():
            generator,template=sources/'generator.py',sources/'template.py'
        else:
            installed=Path('/Users/fbsh/tiny-llm/.venv/lib/python3.12/site-packages/torch/distributed/nn/jit')
            generator,template=installed/'instantiator.py',installed/'templates/remote_module_template.py'
        if not generator.is_file() or not template.is_file(): raise unittest.SkipTest('captured generation source not available')
        if audit.sha(generator)!='567d1314ee27ff0b3bd22e7c4d1157246469de25e7a3183d96debe167b193615' or audit.sha(template)!='0ff1856bbd031b5298d46c06c0502abc20bd804f42c1949ed4127e8c773660cc':
            raise ValueError('static generator fixture revision changed; independent review required')
        cls.generator,cls.template=generator.read_text(),template.read_text()

    def test_actual_literal_reconstruction_has_known_bytes(self):
        import hashlib
        data=audit.torch_generated_source(self.generator,self.template)
        self.assertEqual(len(data),2355)
        self.assertEqual(hashlib.sha256(data).hexdigest(),'8205b16956fb264841ecd8644784a0d157f87df79b17c16825dc1163433ce5d8')

    def test_false_template_selector_is_not_same_source(self):
        source=self.generator.replace('generated_module_name, str_dict, True)','generated_module_name, str_dict, False)')
        with self.assertRaises(ValueError):audit.torch_generated_source(source,self.template)

    def test_nonliteral_argument_is_never_executed(self):
        source=self.generator.replace('args="*args"','args=print("must never execute")')
        with self.assertRaises(ValueError):audit.torch_generated_source(source,self.template)

    def test_other_generated_module_name_rejected(self):
        source=self.generator.replace('f"{_FILE_PREFIX}non_scriptable"','f"{_FILE_PREFIX}other"')
        with self.assertRaises(ValueError):audit.torch_generated_source(source,self.template)

    def test_duplicate_literal_template_assignment_rejected(self):
        with self.assertRaises(ValueError):audit.torch_generated_source(self.generator,self.template+'\n_TEMPLATE_PREFIX = "different"\n')

    def test_extra_generation_statement_rejected(self):
        source=self.generator.replace('    generated_module_name = f"{_FILE_PREFIX}non_scriptable"','    unexpected = 1\n    generated_module_name = f"{_FILE_PREFIX}non_scriptable"')
        with self.assertRaises(ValueError):audit.torch_generated_source(source,self.template)

    def test_changed_render_expression_rejected(self):
        source=self.generator.replace('get_remote_module_template(\n        enable_moving_cpu_tensors_to_cuda\n    ).format(**str_dict)','"unbound code"')
        self.assertNotEqual(source,self.generator)
        with self.assertRaises(ValueError):audit.torch_generated_source(source,self.template)


class ReferencePathTests(unittest.TestCase):
    def setUp(self):
        self.payload=Path('/captured/attempt/payload');self.cwd=str(self.payload/'experiments/tiny-decode-cost')
        self.origin='/reference/tiny';self.venv='/reference/tiny/.venv/lib/python3.12/site-packages'
        self.files={'tiny-source/model_names.py':{'sha256':'a'*64},'tiny-source/src/tiny_llm_ref/__init__.py':{'sha256':'b'*64}}
        root=self.payload/'tiny-source'
        self.record=dict(passed=True,before=['','../../tiny-source/src','../../tiny-source','/reference/tiny/src',self.venv],
            removed=[dict(entry='/reference/tiny/src',resolved='/reference/tiny/src',reason='original reference root or source tree')],
            after=[str(root/'src'),str(root),'',self.venv],preloaded=[],
            resolutions={
                'model_names':dict(origin=str(root/'model_names.py'),namespace_paths=[],sha256='a'*64),
                'tiny_llm_ref':dict(origin=str(root/'src/tiny_llm_ref/__init__.py'),namespace_paths=[str(root/'src/tiny_llm_ref')],sha256='b'*64),
                'extensions_ref':dict(origin=None,namespace_paths=[str(root/'src/extensions_ref')])},
            scope='top-level spec discovery only; no reference source execution')

    def check(self):return audit.reference_paths_checked(self.record,self.origin,self.payload,self.cwd,self.files)

    def test_relative_and_empty_paths_use_child_cwd(self):
        self.assertTrue(self.check()['passed'])

    def test_preserve_legitimate_venv_under_reference_root(self):
        self.record['after'].remove(self.venv)
        self.record['removed'].append(dict(entry=self.venv,resolved=self.venv,reason='original reference root or source tree'))
        with self.assertRaises(ValueError):self.check()

    def test_original_namespace_location_is_not_allowed(self):
        self.record['resolutions']['extensions_ref']['namespace_paths'].append('/reference/tiny/src/extensions_ref')
        with self.assertRaises(ValueError):self.check()

    def test_preloaded_reference_is_not_replaced(self):
        self.record['preloaded']=['extensions_ref']
        with self.assertRaises(ValueError):self.check()

    def test_entrypoint_hash_must_match_captured_payload(self):
        self.record['resolutions']['model_names']['sha256']='0'*64
        with self.assertRaises(ValueError):self.check()


class RealRawMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs={}
        for policy in audit.POLICIES:
            path=attempt_for(policy)/'execution/stdout.json'
            if not path.is_file(): raise unittest.SkipTest('six real successful raw fixtures are not yet present: '+str(FIXTURES))
            cls.docs[policy]=json.loads(path.read_text())
        for doc in cls.docs.values(): audit.analyze_document(doc)

    def fresh(self,policy='cost-200'): return copy.deepcopy(self.docs[policy])

    def rejected(self,doc):
        with self.assertRaises((ValueError,KeyError,TypeError,IndexError,ZeroDivisionError)):
            audit.analyze_document(doc)

    def event(self,doc,name,occurrence=0):
        return [e for e in doc['trials'][0]['events'] if e['name']==name][occurrence]

    def test_all_six_real_raw_and_distinct_work_numerators(self):
        for policy,doc in self.docs.items():
            result=audit.analyze_document(doc);self.assertTrue(result['passed'])
            expected=doc['config']['a_prompt_length']+doc['config']['a_decode_steps']
            if policy!='no-b': expected+=doc['config']['b_prompt_length']
            self.assertEqual(result['trials'][0]['metrics']['total_model_input_tokens'],expected)

    def test_top_failure_cannot_be_erased_by_green_trial_checks(self):
        doc=self.fresh();doc['passed']=False;self.rejected(doc)

    def test_unknown_error_cannot_be_erased_by_passed_flag(self):
        doc=self.fresh();doc['error']='unknown completion';self.rejected(doc)

    def test_missing_trial(self):
        doc=self.fresh();doc['trials']=[];self.rejected(doc)

    def test_wrong_A_input_with_original_summary(self):
        doc=self.fresh();self.event(doc,'step.started')['details']['input_token_ids'][0]+=1;self.rejected(doc)

    def test_wrong_autoregressive_input(self):
        doc=self.fresh();self.event(doc,'step.started',1)['details']['input_token_ids'][0]+=1;self.rejected(doc)

    def test_B_overlap_or_skipped_input_offset(self):
        doc=self.fresh();events=[e for e in doc['trials'][0]['events'] if e['name']=='step.started' and e['request_id']=='B']
        events[-1]['details']['offset']-=1;self.rejected(doc)

    def test_no_B_business_event_rejected(self):
        doc=self.fresh('no-b');e=self.event(doc,'anchor.reached');e['name']='b.requested';e['request_id']='B';self.rejected(doc)

    def test_nonfinal_B_head_cannot_be_evaluated(self):
        doc=self.fresh();e=next(e for e in doc['trials'][0]['events'] if e['name']=='step.started' and e['request_id']=='B')
        e['details']['head_evaluated']=True;self.rejected(doc)

    def test_future_observation_source_rejected(self):
        doc=self.fresh();obs=self.event(doc,'policy.observed');obs['details']['source_yield_seq']=doc['trials'][0]['events'][-1]['seq'];self.rejected(doc)

    def test_rho_cannot_start_with_warmup_or_reference_state(self):
        doc=self.fresh();self.event(doc,'policy.decision_finished')['details']['rho_before_ns_per_token']=1;self.rejected(doc)

    def test_integer_rate_rounding_mutation_rejected(self):
        doc=self.fresh();self.event(doc,'policy.observed')['details']['sample_ns_per_token']+=1;self.rejected(doc)

    def test_candidate_prediction_mutation_rejected(self):
        doc=self.fresh();self.event(doc,'policy.decision_finished',1)['details']['candidate_predictions'][0]['predicted_ns']+=1;self.rejected(doc)

    def test_same_timestamp_event_order_still_enforced(self):
        doc=self.fresh();events=doc['trials'][0]['events'];i=next(i for i,e in enumerate(events) if e['name']=='step.eval_started')
        events[i+1]['t_ns']=events[i]['t_ns'];events[i+1]['t_ms']=events[i]['t_ms']
        events[i],events[i+1]=events[i+1],events[i]
        events[i]['seq']=i;events[i+1]['seq']=i+1;self.rejected(doc)

    def test_unknown_completion_not_drain(self):
        doc=self.fresh();self.event(doc,'cleanup.synchronize_completed')['name']='cleanup.quarantined';self.rejected(doc)

    def test_pending_step_not_hidden_by_zero_resources(self):
        doc=self.fresh();doc['trials'][0]['observations']['pending_step_id']=0;self.rejected(doc)

    def test_diagnostic_failure_preserved_as_failure(self):
        doc=self.fresh();doc['trials'][0]['observations']['diagnostic_errors']=['snapshot failed'];self.rejected(doc)

    def test_private_pages_cannot_alias_between_A_B(self):
        doc=self.fresh();snap=next(s for s in doc['trials'][0]['observations']['snapshots'] if set(s['owners'])=={'A','B'} and s['owners']['B'][0]['page_ids'])
        snap['owners']['B'][0]['page_ids'][0]=snap['owners']['A'][0]['page_ids'][0];self.rejected(doc)

    def test_every_layer_offset_is_checked(self):
        doc=self.fresh();doc['trials'][0]['observations']['snapshots'][0]['owners']['A'][-1]['offset']+=1;self.rejected(doc)

    def test_reservation_cannot_disappear_while_owner_live(self):
        doc=self.fresh();doc['trials'][0]['observations']['snapshots'][0]['reservations']['owners'].clear();self.rejected(doc)

    def test_growth_copy_cost_not_accepted_from_summary(self):
        doc=self.fresh();doc['trials'][0]['observations']['snapshots'][-1]['pools'][0]['copied_bytes_on_growth']+=1;self.rejected(doc)

    def test_snapshot_bound_to_exact_event(self):
        doc=self.fresh();doc['trials'][0]['observations']['snapshots'][0]['event_seq']-=1;self.rejected(doc)

    def test_A_actual_output_matches_all_reference_tokens(self):
        doc=self.fresh();doc['reference']['A']['output_token_ids'][-1]+=1;self.rejected(doc)

    def test_partition_reference_not_replaced_by_whole_reference(self):
        doc=self.fresh();doc['partition_references'][0]['partition']=[doc['config']['b_prompt_length']];self.rejected(doc)

    def test_partition_output_difference_not_accepted(self):
        doc=self.fresh();doc['partition_references'][0]['output_token_ids'][0]+=1;self.rejected(doc)

    def test_reference_replay_cannot_precede_timed_work(self):
        doc=self.fresh();ref=doc['partition_references'][0];ref['clock_origin_ns']=doc['reference']['A']['clock_origin_ns'];self.rejected(doc)

    def test_metric_ITL_decode_and_global_intervals_are_separate(self):
        for key in ('a_user_itl_ms','a_decode_call_ms','global_step_completion_intervals_ms','global_token_delivery_intervals_ms','policy_decision_ms'):
            with self.subTest(metric=key):
                doc=self.fresh();doc['trials'][0]['metrics'][key][0]+=0.1;self.rejected(doc)

    def test_missing_check_or_duplicate_green_check_is_rejected(self):
        doc=self.fresh();doc['trials'][0]['checks'][-1]=copy.deepcopy(doc['trials'][0]['checks'][0]);self.rejected(doc)

    def test_relative_timestamp_cannot_ignore_trace_origins(self):
        doc=self.fresh();doc['trials'][0]['clock_origin_ns']=doc['process_clock_origin_ns'];self.rejected(doc)


class ArtifactMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original=attempt_for('cost-200').resolve()
        if not (cls.original/'execution/stdout.json').is_file():
            raise unittest.SkipTest('real successful attempt not present')
        audit.analyze_document(json.loads((cls.original/'execution/stdout.json').read_text()))
        cls.temp=tempfile.TemporaryDirectory(prefix='decode-cost-audit-')
        cls.attempt=Path(cls.temp.name)/'attempt'
        shutil.copytree(cls.original,cls.attempt)
        cls.raw=cls.attempt/'execution/stdout.json'

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls,'temp'):
            for path in cls.attempt.rglob('*'):
                if not path.is_symlink(): path.chmod(0o755 if path.is_dir() else 0o644)
            cls.attempt.chmod(0o755);cls.temp.cleanup()

    def setUp(self): self.changed={}

    def tearDown(self):
        for path,data in self.changed.items(): path.write_bytes(data)

    def write(self,path,data):
        if path not in self.changed: self.changed[path]=path.read_bytes()
        path.chmod(0o644);path.write_bytes(data)

    def rewrite_raw(self,mutate):
        doc=json.loads(self.raw.read_text());mutate(doc)
        self.write(self.raw,(json.dumps(doc,indent=2)+'\n').encode())
        path=self.raw.with_name('run.json');run=json.loads(path.read_text())
        run['files']['stdout.json']={'bytes':self.raw.stat().st_size,'sha256':audit.sha(self.raw)}
        self.write(path,(json.dumps(run,indent=2)+'\n').encode())

    def rejected(self):
        with self.assertRaises((ValueError,KeyError,TypeError,IndexError,OSError)):
            audit.artifact_audit(self.raw,self.attempt)

    def test_portable_recheck_never_reads_original_attempt(self):
        original_open=Path.open
        def guarded(path,*args,**kwargs):
            if path.resolve().is_relative_to(self.original): raise AssertionError('checker read original attempt instead of copied bytes')
            return original_open(path,*args,**kwargs)
        with patch.object(Path,'open',guarded): result=audit.artifact_audit(self.raw,self.attempt)
        self.assertTrue(result['passed']);self.assertFalse(result['external_full_hashes_verified'])

    def test_pre_execution_only_does_not_prove_after_bytes(self):
        self.rewrite_raw(lambda d:d['provenance']['post_execution_binding'].update(external_full_bytes_checked=False))
        self.rejected()

    def test_actual_import_outside_frozen_payload_is_rejected(self):
        def mutate(doc):
            doc['provenance']['post_execution_binding']['imported_binding']['tiny_llm_ref.qwen3_week3']['path']='/tmp/unbound/qwen3_week3.py'
        self.rewrite_raw(mutate);self.rejected()

    def test_unpinned_third_party_import_is_rejected(self):
        def mutate(doc):
            doc['provenance']['post_execution_binding']['imported_binding']['external_sidecar']={'kind':'external-runtime','path':'/tmp/unbound/extra.py','sha256':'0'*64}
        self.rewrite_raw(mutate);self.rejected()

    def test_actual_argv_changed_policy_rejected(self):
        path=self.raw.with_name('run.json');run=json.loads(path.read_text());i=run['command'].index('--policy')
        run['command'][i+1]='no-b';self.write(path,(json.dumps(run)+'\n').encode());self.rejected()

    def test_actual_run_timeout_cannot_be_green(self):
        path=self.raw.with_name('run.json');run=json.loads(path.read_text());run['timed_out']=True
        self.write(path,(json.dumps(run)+'\n').encode());self.rejected()

    def test_payload_changed_after_execution_rejected(self):
        path=self.attempt/'payload/experiments/tiny-decode-cost/fixture.json'
        self.write(path,path.read_bytes()+b'\n');self.rejected()

    def test_raw_log_hash_must_match_actual_bytes(self):
        self.write(self.raw,self.raw.read_bytes()+b'\n');self.rejected()

    def test_raw_fixture_ids_cannot_drift_from_snapshot(self):
        self.rewrite_raw(lambda d:d['inputs']['a_prompt_token_ids'].__setitem__(0,d['inputs']['a_prompt_token_ids'][0]+1));self.rejected()

    def test_generated_file_wrong_basename_rejected(self):
        def mutate(doc):
            row=doc['provenance']['post_execution_binding']['imported_binding'][audit.GENERATED_MODULE]
            row['path']=str(Path(row['generation_directory'])/'another_generated.py')
        self.rewrite_raw(mutate);self.rejected()

    def test_generated_file_wrong_directory_rejected(self):
        def mutate(doc):
            row=doc['provenance']['post_execution_binding']['imported_binding'][audit.GENERATED_MODULE]
            row['generation_directory']='/tmp/unrelated-directory'
        self.rewrite_raw(mutate);self.rejected()

    def test_arbitrary_temporary_module_is_not_whitelisted(self):
        def mutate(doc):
            bindings=doc['provenance']['post_execution_binding']['imported_binding']
            row=copy.deepcopy(bindings[audit.GENERATED_MODULE]);row['module_name']='other_generated_module'
            bindings['other_generated_module']=row
        self.rewrite_raw(mutate);self.rejected()

    def test_generated_actual_byte_digest_must_match_template(self):
        def mutate(doc):
            doc['provenance']['post_execution_binding']['imported_binding'][audit.GENERATED_MODULE]['sha256']='0'*64
        self.rewrite_raw(mutate);self.rejected()

    def test_generated_generator_identity_cannot_drift(self):
        def mutate(doc):
            doc['provenance']['post_execution_binding']['imported_binding'][audit.GENERATED_MODULE]['generator']['path']='/tmp/other_generator.py'
        self.rewrite_raw(mutate);self.rejected()

    def test_generated_template_actual_import_required(self):
        def mutate(doc):
            del doc['provenance']['post_execution_binding']['imported_binding']['torch.distributed.nn.jit.templates.remote_module_template']
        self.rewrite_raw(mutate);self.rejected()

    def test_self_consistent_wrong_expected_file_fails_independent_AST(self):
        import hashlib
        manifest_path=self.attempt/'manifest.json';manifest=json.loads(manifest_path.read_text())
        entry=manifest['generated_runtime_templates'][0];relative=entry['expected_payload'];path=self.attempt/'payload'/relative
        data=path.read_bytes()+b'\n# extra bytes not derived from the generator\n';self.write(path,data)
        digest=hashlib.sha256(data).hexdigest()
        manifest['payload_files'][relative].update(sha256=digest,bytes=len(data));entry.update(sha256=digest,bytes=len(data))
        self.write(manifest_path,(json.dumps(manifest,indent=2)+'\n').encode())
        prepared_path=self.attempt/'prepared.json';prepared=json.loads(prepared_path.read_text());prepared['manifest_sha256']=audit.sha(manifest_path)
        self.write(prepared_path,(json.dumps(prepared)+'\n').encode())
        with self.assertRaisesRegex(ValueError,'generated bytes differ from independent literal reconstruction'):
            audit.attempt_audit(self.attempt)


class AttemptSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if ATTEMPT_MAP is None or not ATTEMPT_MAP.is_file(): raise unittest.SkipTest('explicit real six-attempt map required')

    def test_all_six_share_complete_payload_external_inputs_and_targets(self):
        result=audit.analyze_attempt_set(ATTEMPT_MAP)
        self.assertTrue(result['passed']);self.assertEqual(result['total_trials'],6)
        self.assertEqual(result['common']['payload_files'],74)
        self.assertEqual(sum(r['derivation']['kind']=='verified-batch-clone' for r in result['documents']),5)
        self.assertNotEqual(attempt_for('cost-200'),attempt_for('no-b').parent/'cost-200')
        self.assertFalse((attempt_for('no-b').parent/'cost-200/execution').exists())

    def test_same_attempt_cannot_count_as_two_conditions(self):
        mapping=json.loads(ATTEMPT_MAP.read_text());mapping['whole']=mapping['no-b']
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'map.json';path.write_text(json.dumps(mapping))
            with self.assertRaisesRegex(ValueError,'one attempt cannot count'):
                audit.analyze_attempt_set(path)

    def test_unexecuted_cost_clone_is_not_an_extra_sample(self):
        mapping=json.loads(ATTEMPT_MAP.read_text());mapping['cost-200']=str(attempt_for('no-b').parent/'cost-200')
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'map.json';path.write_text(json.dumps(mapping))
            with self.assertRaises(FileNotFoundError):audit.analyze_attempt_set(path)


class CloneDerivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source=attempt_for('no-b');parent=cls.source.parent
        if not (parent/'template-manifest.json').is_file(): raise unittest.SkipTest('real derived batch not available')
        cls.parent=parent

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='decode-cost-lineage-');self.batch=Path(self.temp.name)/'batch';self.batch.mkdir()
        self.cell=self.batch/'no-b';self.cell.mkdir()
        for name in ('plan.json','template-manifest.json','template-prepared.json','prepare-driver.py','prepared-index.json'):
            shutil.copyfile(self.parent/name,self.batch/name)
        shutil.copyfile(self.source/'manifest.json',self.cell/'manifest.json')
        self.manifest=json.loads((self.cell/'manifest.json').read_text())

    def tearDown(self): self.temp.cleanup()

    def test_archived_parent_proves_derivation_without_original_paths(self):
        original_open=Path.open
        forbidden=[self.parent.resolve(),attempt_for('cost-200').resolve()]
        def guarded(path,*args,**kwargs):
            if any(path.resolve().is_relative_to(root) for root in forbidden):raise AssertionError('lineage recheck read original attempt')
            return original_open(path,*args,**kwargs)
        with patch.object(Path,'open',guarded):result=audit.clone_derivation_checked(self.cell,self.manifest)
        self.assertTrue(result['archived_template_bytes_verified'])

    def test_changed_archived_parent_bytes_rejected(self):
        path=self.batch/'template-manifest.json';path.write_bytes(path.read_bytes()+b'\n')
        with self.assertRaises(ValueError):audit.clone_derivation_checked(self.cell,self.manifest)

    def test_changed_plan_order_rejected(self):
        path=self.batch/'plan.json';plan=json.loads(path.read_text());plan['cells'][0],plan['cells'][1]=plan['cells'][1],plan['cells'][0]
        path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError,'batch order'):audit.clone_derivation_checked(self.cell,self.manifest)

    def test_clone_cannot_change_runtime_or_claim_rehash(self):
        for part in ('runtime','batch_derivation'):
            with self.subTest(part=part):
                changed=copy.deepcopy(self.manifest)
                if part=='runtime':changed[part]['executable']='/tmp/other-python'
                else:changed[part]['external_bytes_rehashed_during_clone']=True
                with self.assertRaisesRegex(ValueError,'clone changed fields'):audit.clone_derivation_checked(self.cell,changed)


if __name__=='__main__': unittest.main()
