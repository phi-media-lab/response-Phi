"""Synthetic aggregation/control metadata tests, never GPU/model evidence."""
import copy
import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('matrix_audit',ROOT/'scripts/analyze_decode_cost_matrix.py')
matrix=importlib.util.module_from_spec(spec);spec.loader.exec_module(matrix)


def plan_fixture():
    return dict(schema_version=1,kind='tiny-decode-cost-batch',mode='matrix',schedule_seed=20261029,
        cells=matrix.expected_cells(),shape=dict(a_prompt_length=64,b_prompt_length=1024,a_decode_steps=40,arrival_after_a_decode=4),
        model_or_gpu_loaded=False,external_full_hashes_checked_at_batch_preparation=False)


def synthetic_rows():
    rows=[];shape=plan_fixture()['shape']
    names=set(matrix.B_METRICS)|set(matrix.WORK_RESOURCE_METRICS)|{'b_admission_ms','b_ready_wait_ms','b_chunks','b_tokens_while_A_runnable',
        'b_tokens_after_A_completed','b_chunks_while_A_runnable','b_chunks_after_A_completed','b_tail_service_ms','policy_decision_total_ms'}
    for index,cell in enumerate(matrix.expected_cells()):
        value=100+10*cell['block']+matrix.POLICIES.index(cell['policy'])
        scalars={key:float(value) for key in names}
        slices=[dict(step_id=5,tokens=32,reason='bootstrap',predicted_ns=None,actual_ns=350_000_000,
                     prediction_error_ns=None,budget_overshoot_ns=150_000_000,head_evaluated=False,
                     rho_before_ns_per_token=None,rho_after_ns_per_token=10_937_500),
                dict(step_id=7,tokens=32,reason='minimum_progress_fallback',predicted_ns=400_000_000,actual_ns=300_000_000,
                     prediction_error_ns=-100_000_000,budget_overshoot_ns=100_000_000,head_evaluated=True,
                     rho_before_ns_per_token=10_937_500,rho_after_ns_per_token=10_156_250)]
        start=datetime.datetime(2026,10,29,tzinfo=datetime.timezone.utc)+datetime.timedelta(seconds=index*2)
        rows.append(dict(cell,passed=True,status='synthetic-unit-fixture',
            main=dict(scalars=scalars,partition=[] if cell['policy']=='no-b' else [32,32],
                      a_delivery_ms_from_work_started=[float(value+i) for i in range(3)],
                      resources=dict(final=dict(storage_growths_across_layers=28,
                          copied_bytes_on_growth_across_layers=1000+cell['block'],reused_page_allocations_across_layers=0)),
                      cost_slices=slices if cell['policy']=='cost-200' else None),
            warmup=dict(scalars={key:1e99 for key in names}),
            recorded_run=dict(started_at_utc=start.isoformat(),elapsed_seconds=1.0),
            _common_identity=dict(payload_files={'source':{'sha256':'a'*64}},runtime={'python':'synthetic'},
                external_dependencies=[{'path':'synthetic','sha256':'b'*64}],generated_runtime_templates=[],reference_origin={},
                model='synthetic-not-loaded',inputs={'fixture_sha256':'c'*64},config=shape,numerical_targets={'A':[1,2],'B-whole':[3]})))
    return rows


class DesignTests(unittest.TestCase):
    def test_36_cells_have_six_blocks_and_every_ordered_pair(self):
        cells=matrix.validate_plan(plan_fixture());self.assertEqual(len(cells),36)
        pairs=[]
        for b in range(6):
            row=[c for c in cells if c['block']==b]
            self.assertEqual({c['policy'] for c in row},set(matrix.POLICIES))
            self.assertEqual([(matrix.POLICIES.index(c['policy'])-c['design_row'])%6 for c in row],[0,1,5,2,4,3])
            pairs.extend(zip([c['policy'] for c in row],[c['policy'] for c in row][1:]))
        self.assertEqual(len(set(pairs)),30)
        self.assertEqual({c['design_row'] for c in cells},set(range(6)))

    def test_same_policy_counts_but_changed_order_rejected(self):
        plan=plan_fixture();plan['cells'][0],plan['cells'][1]=plan['cells'][1],plan['cells'][0]
        with self.assertRaises(ValueError):matrix.validate_plan(plan)

    def test_missing_cell_or_changed_seed_rejected(self):
        for change in ('missing','seed'):
            plan=plan_fixture()
            if change=='missing':plan['cells'].pop()
            else:plan['schedule_seed']+=1
            with self.subTest(change=change),self.assertRaises(ValueError):matrix.validate_plan(plan)

    def test_warmup_is_exactly_one_per_cell(self):
        plan=plan_fixture();plan['cells'][0]['warmup']=0
        with self.assertRaises(ValueError):matrix.validate_plan(plan)


class PairingTests(unittest.TestCase):
    def test_pair_first_not_difference_or_ratio_of_marginal_medians(self):
        row=matrix.paired_values([10.0,100.0,30.0],[5.0,80.0,40.0])
        self.assertEqual(row['delta']['values'],[5.0,20.0,-10.0]);self.assertEqual(row['delta']['median'],5.0)
        self.assertEqual(row['ratio']['values'],[2.0,1.25,0.75]);self.assertEqual(row['ratio']['median'],1.25)
        self.assertNotEqual(row['delta']['median'],30.0-40.0)
        self.assertNotEqual(row['ratio']['median'],30.0/40.0)

    def test_zero_baseline_ratio_is_undefined_not_zero(self):
        row=matrix.paired_values([3.0,4.0],[0.0,2.0])
        self.assertIsNone(row['pairs'][0]['ratio']);self.assertFalse(row['pairs'][0]['ratio_defined'])
        self.assertEqual(row['ratio']['count'],1);self.assertEqual(row['delta']['count'],2)

    def test_nonfinite_pair_rejected(self):
        for value in (float('nan'),float('inf')):
            with self.assertRaises(ValueError):matrix.paired_values([value],[1.0])

    def test_no_B_throughput_not_in_cross_work_comparisons(self):
        result=matrix.aggregate_rows(synthetic_rows())
        self.assertNotIn('model_input_tokens_per_second',result['no_b_A_interference_baseline']['metrics'])
        for row in result['paired_A_vs_no_b']:
            self.assertEqual(set(row['metrics']),set(matrix.A_METRICS))
            self.assertNotIn('work_ms',row['metrics'])
        for row in result['paired_cost_vs_B_conditions']:
            self.assertNotEqual(row['right_policy'],'no-b')
            self.assertIn('model_input_tokens_per_second',row['metrics'])

    def test_warmup_extremes_never_enter_main_statistics(self):
        result=matrix.aggregate_rows(synthetic_rows())
        baseline=result['no_b_A_interference_baseline']
        self.assertEqual(baseline['metrics']['a_completion_ms']['values'],[100.,110.,120.,130.,140.,150.])
        self.assertEqual(baseline['main_trials'],6);self.assertEqual(baseline['warmups_excluded'],6)

    def test_resource_and_work_counts_retained_without_physical_traffic_claim(self):
        result=matrix.aggregate_rows(synthetic_rows())
        baseline=result['no_b_A_interference_baseline']
        self.assertTrue(set(matrix.WORK_RESOURCE_METRICS)<=set(baseline['metrics']))
        self.assertEqual(baseline['resource_counters']['storage_growths_across_layers']['values'],[28]*6)
        self.assertEqual(baseline['resource_counters']['copied_bytes_on_growth_across_layers']['values'],list(range(1000,1006)))
        self.assertIn('not physical traffic',baseline['resource_counter_scope'])

    def test_A_output_curves_are_paired_by_block_and_index(self):
        result=matrix.aggregate_rows(synthetic_rows())
        for row in result['paired_A_delivery_curves']:
            delta=float(matrix.POLICIES.index(row['policy']))
            self.assertEqual(len(row['per_block']),6)
            for pair in row['per_block']:self.assertEqual(pair['delta_ms'],[delta]*3)

    def test_incomplete_or_failed_cells_have_no_aggregation(self):
        rows=synthetic_rows()
        with self.assertRaises(ValueError):matrix.aggregate_rows(rows[:-1])
        rows[0]['passed']=False
        with self.assertRaises(ValueError):matrix.aggregate_rows(rows)


class ResourceObservationTests(unittest.TestCase):
    def fixture(self):
        pools=[dict(layer=i,used_page_ids=[0],backing_bytes=256,storage_growths=1,
                    copied_bytes_on_growth=64,reused_page_allocations=i) for i in range(2)]
        first=dict(label='after-step-0',event_seq=8,pools=pools,reservations=dict(used=3,high_watermark=3))
        final=copy.deepcopy(first);final.update(label='final-drained',event_seq=12)
        final['reservations']['used']=0
        for pool in final['pools']:pool['used_page_ids']=[]
        events=[dict(name='step.forward_returned',request_id='A',details={}),
                dict(name='step.completed',request_id='A',details=dict(input_tokens=64,head_evaluated=True)),
                dict(name='a.token_delivered',request_id='A',details={})]
        return dict(observations=dict(snapshots=[first,final]),events=events)

    def test_snapshot_identity_units_and_per_request_work_preserved(self):
        result=matrix.resource_observations(self.fixture())
        self.assertEqual(result['snapshot_count'],2)
        self.assertEqual(result['snapshots'][0]['event_seq'],8)
        self.assertEqual(result['snapshots'][0]['active_page_slots'],2)
        self.assertEqual(result['snapshots'][0]['reserved_pages_per_layer'],3)
        self.assertEqual(result['final']['active_page_slots'],0)
        self.assertEqual(result['final']['backing_bytes'],512)
        self.assertEqual(result['final']['copied_bytes_on_growth_across_layers'],128)
        self.assertEqual([p['layer'] for p in result['final']['per_layer_counters']],[0,1])
        self.assertEqual(result['work_by_request']['A'],dict(forward_calls=1,completed_steps=1,input_tokens=64,evaluated_token_heads=1,delivered_tokens=1))
        self.assertEqual(result['work_by_request']['B'],dict(forward_calls=0,completed_steps=0,input_tokens=0,evaluated_token_heads=0,delivered_tokens=0))
        self.assertIn('not continuous memory sampling',result['scope'])

    def test_missing_final_snapshot_is_not_reported_as_drained(self):
        trial=self.fixture();trial['observations']['snapshots'].pop()
        with self.assertRaises(ValueError):matrix.resource_observations(trial)


class TrialGroupTests(unittest.TestCase):
    def setUp(self):
        self.doc={'config':dict(policy='whole',trials=1,warmup=1)}
        self.checked={'trials':[dict(trial_id=0,warmup=False)],'warmups':[dict(trial_id=-1,warmup=True)]}

    def test_exact_groups_pass(self):
        main,warm=matrix.trial_groups_checked(self.doc,self.checked,'whole')
        self.assertEqual(main['trial_id'],0);self.assertEqual(warm['trial_id'],-1)

    def test_smoke_cannot_substitute_for_warm_plus_main(self):
        self.doc['config']['warmup']=0
        with self.assertRaises(ValueError):matrix.trial_groups_checked(self.doc,self.checked,'whole')

    def test_missing_warmup_or_swapped_identity_rejected(self):
        self.checked['warmups']=[]
        with self.assertRaises(ValueError):matrix.trial_groups_checked(self.doc,self.checked,'whole')
        self.checked['warmups']=[dict(trial_id=0,warmup=True)]
        with self.assertRaises(ValueError):matrix.trial_groups_checked(self.doc,self.checked,'whole')

    def test_boolean_is_not_trial_identity(self):
        self.checked['trials'][0]['trial_id']=False
        with self.assertRaises(ValueError):matrix.trial_groups_checked(self.doc,self.checked,'whole')


class CostDistributionTests(unittest.TestCase):
    def cost_rows(self):return [r for r in synthetic_rows() if r['policy']=='cost-200']

    def test_slice_errors_stay_grouped_by_six_trials(self):
        result=matrix.summarize_cost(self.cost_rows())
        self.assertEqual(len(result['per_trial']),6)
        self.assertEqual(result['pooled_descriptive_counts'],dict(trials=6,slices=12,predicted_slices=6,budget_exceeded_slices=12))
        self.assertEqual(result['final_head_prediction_error_ms']['count'],6)
        self.assertEqual(result['final_head_prediction_error_ms']['values'],[-100.0]*6)
        self.assertEqual(result['trial_level']['fallback_slices']['values'],[1]*6)

    def test_budget_misses_and_minimum_fallback_do_not_fail_correctness(self):
        result=matrix.aggregate_rows(synthetic_rows())
        self.assertEqual(result['cost_estimator']['trial_level']['budget_exceeded_fraction']['values'],[1.0]*6)

    def test_missing_final_head_rejected(self):
        rows=self.cost_rows();rows[0]['main']['cost_slices'][-1]['head_evaluated']=False
        with self.assertRaises(ValueError):matrix.summarize_cost(rows)


class RecordedOrderTests(unittest.TestCase):
    def test_serial_metadata_order(self):
        result=matrix.serial_order_checked(synthetic_rows());self.assertTrue(result['passed']);self.assertEqual(len(result['intervals']),36)

    def test_reordered_records_rejected(self):
        rows=synthetic_rows();rows[0],rows[1]=rows[1],rows[0]
        with self.assertRaises(ValueError):matrix.serial_order_checked(rows)

    def test_overlapping_process_intervals_rejected(self):
        rows=synthetic_rows();rows[0]['recorded_run']['elapsed_seconds']=3.0
        with self.assertRaises(ValueError):matrix.serial_order_checked(rows)

    def test_timezone_required(self):
        rows=synthetic_rows();rows[0]['recorded_run']['started_at_utc']='2026-10-29T00:00:00'
        with self.assertRaises(ValueError):matrix.serial_order_checked(rows)

    def test_extreme_finite_elapsed_is_a_structured_rejection(self):
        rows=synthetic_rows();rows[0]['recorded_run']['elapsed_seconds']=1e300
        with self.assertRaisesRegex(ValueError,'supported datetime range'):matrix.serial_order_checked(rows)


class MatrixControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='decode-matrix-unit-');self.batch=Path(self.temp.name)
        (self.batch/'plan.json').write_text(json.dumps(plan_fixture()))

    def tearDown(self):self.temp.cleanup()

    def analyze(self,rows):
        lookup={row['cell_id']:row for row in rows}
        def inspect(batch,cell,plan_sha,audit):return copy.deepcopy(lookup[cell['cell_id']])
        with patch.object(matrix,'load_checker',return_value=object()),patch.object(matrix,'inspect_cell',side_effect=inspect):
            return matrix.analyze_matrix(self.batch,'unused-synthetic-audit')

    def test_synthetic_control_fixture_accepted_only_with_all_groups(self):
        result=self.analyze(synthetic_rows())
        self.assertTrue(result['passed']);self.assertEqual(result['accepted_main_trials'],36);self.assertEqual(result['verified_warmups'],36)

    def test_missing_cell_retained_and_statistics_suppressed(self):
        rows=synthetic_rows();rows[7]=dict(matrix.expected_cells()[7],passed=False,status='missing',missing_files=['synthetic'])
        result=self.analyze(rows)
        self.assertFalse(result['passed']);self.assertIsNone(result['statistics']);self.assertEqual(len(result['cells']),36)
        self.assertEqual(result['completed_cell_audits'],35);self.assertEqual(result['cells'][7]['status'],'missing')

    def test_changed_common_identity_suppresses_statistics(self):
        rows=synthetic_rows();rows[8]['_common_identity']['runtime']['python']='other-runtime'
        result=self.analyze(rows)
        self.assertFalse(result['passed']);self.assertIsNone(result['statistics']);self.assertTrue(result['errors'])

    def test_unpinned_audit_module_is_not_executed(self):
        path=self.batch/'unexpected.py';path.write_text("raise AssertionError('must never execute unpinned source')\n")
        with self.assertRaisesRegex(ValueError,'checker bytes differ'):matrix.load_checker(path)


if __name__=='__main__':unittest.main()
