#!/usr/bin/env python3
"""Audit one frozen 36-cell decode-cost matrix, then aggregate whole trials.

Reuses only the hash-pinned independent event/byte checker. It never imports the
experiment, scheduling protocol, MLX, Torch, or a model. Failed/missing cells stay
visible and prevent a complete-matrix statistical result.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import random
import statistics

CHECKER_SHA256 = '6a9a6149fac3f098bff2c8fb6e0a80b79ce38bcdf27ebeedefc675375da347b4'
POLICIES = ('no-b','whole','fixed-32','fixed-128','fixed-512','cost-200')
SEED = 20261029
A_METRICS = ('a_completion_ms','a_itl_max_ms','a_itl_median_ms','a_itl_mean_ms',
             'a_after_anchor_itl_max_ms','a_after_anchor_itl_median_ms','a_after_anchor_itl_sum_ms',
             'a_decode_call_median_ms','a_decode_call_max_ms')
B_METRICS = A_METRICS + ('b_ttft_ms','work_ms','through_drain_ms',
                        'model_input_tokens_per_second','output_tokens_per_second')
WORK_RESOURCE_METRICS = ('total_model_input_tokens','total_output_tokens','constructed_head_graphs',
                        'evaluated_token_heads','peak_active_page_slots','peak_backing_bytes',
                        'peak_reserved_pages_per_layer')


def need(condition,message):
    if not condition: raise ValueError(message)


def exact(a,b):
    if type(a) is not type(b): return False
    if isinstance(b,dict): return a.keys()==b.keys() and all(exact(a[k],v) for k,v in b.items())
    if isinstance(b,list): return len(a)==len(b) and all(exact(x,y) for x,y in zip(a,b))
    return a==b


def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1<<20),b''): digest.update(chunk)
    return digest.hexdigest()


def identity_sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def finite(value): return type(value) in (float,int) and math.isfinite(value)


def summary(values):
    need(all(finite(value) for value in values),'nonfinite statistical value')
    return dict(count=len(values),values=list(values),min=min(values),median=statistics.median(values),
                mean=statistics.mean(values),max=max(values)) if values else dict(count=0,values=[])


def load_checker(path):
    path=Path(path).resolve();need(sha(path)==CHECKER_SHA256,'independent checker bytes differ from frozen revision 4')
    spec=importlib.util.spec_from_file_location('_decode_cost_frozen_independent_checker',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def expected_cells():
    rows=list(range(6));random.Random(SEED).shuffle(rows)
    return [dict(cell_id=f'block-{block:02d}-position-{position:02d}-{POLICIES[(n+row)%6]}',block=block,
                 design_row=row,position=position,policy=POLICIES[(n+row)%6],trials=1,warmup=1)
            for block,row in enumerate(rows) for position,n in enumerate((0,1,5,2,4,3))]


def validate_plan(plan):
    need(exact(plan.get('schema_version'),1) and plan.get('kind')=='tiny-decode-cost-batch' and plan.get('mode')=='matrix','matrix plan kind/schema')
    need(exact(plan.get('schedule_seed'),SEED) and exact(plan.get('cells'),expected_cells()),'matrix is not the predeclared 36-cell Williams order')
    shape=plan['shape'];need(set(shape)=={'a_prompt_length','b_prompt_length','a_decode_steps','arrival_after_a_decode'},'matrix shape keys')
    for key,high in [('a_prompt_length',1024),('b_prompt_length',4096),('a_decode_steps',40)]:
        need(type(shape[key]) is int and 1<=shape[key]<=high,'invalid matrix shape '+key)
    need(type(shape['arrival_after_a_decode']) is int and 1<=shape['arrival_after_a_decode']<shape['a_decode_steps'],'invalid matrix anchor')
    need(plan.get('model_or_gpu_loaded') is False and plan.get('external_full_hashes_checked_at_batch_preparation') is False,'preparation/measurement boundary changed')
    # Check design properties independently of the concrete permutation replay.
    cells=plan['cells'];pairs=[]
    for block in range(6):
        row=[c['policy'] for c in cells if c['block']==block]
        need(len(row)==6 and set(row)==set(POLICIES),'block missing/duplicating policy')
        pairs.extend(zip(row,row[1:]))
    need(len(pairs)==30 and set(pairs)=={(a,b) for a in POLICIES for b in POLICIES if a!=b},'within-block ordered predecessor balance failed')
    for position in range(6): need({c['policy'] for c in cells if c['position']==position}==set(POLICIES),'position balance failed')
    return cells


def recorded_interval(run):
    start=datetime.datetime.fromisoformat(run['started_at_utc'])
    need(start.tzinfo is not None,'recorded process start lacks timezone')
    start=start.astimezone(datetime.timezone.utc)
    elapsed=run['elapsed_seconds'];need(finite(elapsed) and elapsed>0,'recorded elapsed time invalid')
    try: end=start+datetime.timedelta(seconds=elapsed)
    except OverflowError as error:
        raise ValueError('recorded process interval exceeds supported datetime range') from error
    return start,end


def serial_order_checked(rows):
    """A host-record consistency check; not an exact GPU execution timeline."""
    previous=None;intervals=[]
    for row in rows:
        start,end=recorded_interval(row['recorded_run'])
        if previous is not None:
            need(previous[1]<=start,'planned cells overlap or are reordered in recorded process clocks: '+previous[2]+' -> '+row['cell_id'])
        intervals.append(dict(cell_id=row['cell_id'],started_at_utc=start.isoformat(),
                              derived_end_at_utc=end.isoformat(),elapsed_seconds=row['recorded_run']['elapsed_seconds']))
        previous=(start,end,row['cell_id'])
    return dict(passed=True,intervals=intervals,
        scope='Saved UTC start plus monotonic elapsed are consistent with serial processes in planned order. This assumes no unrecorded wall-clock jump; it is not an exact GPU interval or hardware-overlap proof.')


def trial_scalars(metrics,policy):
    result=dict(a_completion_ms=metrics['a_completion_ms'],a_itl_max_ms=metrics['a_user_itl_summary']['max'],
        a_itl_median_ms=metrics['a_user_itl_summary']['median'],a_itl_mean_ms=metrics['a_user_itl_summary']['mean'],
        a_after_anchor_itl_max_ms=metrics['a_after_anchor_itl_summary']['max'],
        a_after_anchor_itl_median_ms=metrics['a_after_anchor_itl_summary']['median'],a_after_anchor_itl_sum_ms=metrics['a_after_anchor_itl_sum_ms'],
        a_decode_call_median_ms=statistics.median(metrics['a_decode_call_ms']),a_decode_call_max_ms=max(metrics['a_decode_call_ms']),
        work_ms=metrics['work_ms'],through_drain_ms=metrics['through_drain_ms'])
    for key in WORK_RESOURCE_METRICS: result[key]=metrics[key]
    if policy!='no-b':
        for key in ('b_ttft_ms','b_admission_ms','b_ready_wait_ms','model_input_tokens_per_second','output_tokens_per_second',
                    'b_chunks','b_tokens_while_A_runnable','b_tokens_after_A_completed','b_chunks_while_A_runnable',
                    'b_chunks_after_A_completed','b_tail_service_ms'):
            result[key]=metrics[key]
        result['policy_decision_total_ms']=sum(metrics['policy_decision_ms'])
    need(all(finite(value) for value in result.values()),'nonfinite main-trial scalar')
    return result


def delivery_curve(trial):
    start=next(e['t_ns'] for e in trial['events'] if e['name']=='work.started')
    return [(e['t_ns']-start)/1e6 for e in trial['events'] if e['name']=='a.token_delivered']


def resource_observations(trial):
    """Extract already independently replayed snapshots; no physical traffic inference."""
    series=[]
    for snapshot in trial['observations']['snapshots']:
        pools=snapshot['pools']
        counters=[dict(layer=p['layer'],storage_growths=p['storage_growths'],
                       copied_bytes_on_growth=p['copied_bytes_on_growth'],
                       reused_page_allocations=p['reused_page_allocations']) for p in pools]
        series.append(dict(label=snapshot['label'],event_seq=snapshot['event_seq'],
            active_page_slots=sum(len(p['used_page_ids']) for p in pools),
            backing_bytes=sum(p['backing_bytes'] for p in pools),
            reserved_pages_per_layer=snapshot['reservations']['used'],
            reserved_high_watermark_per_layer=snapshot['reservations']['high_watermark'],
            storage_growths_across_layers=sum(p['storage_growths'] for p in pools),
            copied_bytes_on_growth_across_layers=sum(p['copied_bytes_on_growth'] for p in pools),
            reused_page_allocations_across_layers=sum(p['reused_page_allocations'] for p in pools),
            per_layer_counters=counters))
    need(bool(series) and series[-1]['label']=='final-drained','resource observations lack final drain snapshot')
    by_request={}
    for owner in ('A','B'):
        events=[event for event in trial['events'] if event['request_id']==owner]
        completed=[e for e in events if e['name']=='step.completed']
        by_request[owner]=dict(forward_calls=sum(e['name']=='step.forward_returned' for e in events),
            completed_steps=len(completed),input_tokens=sum(e['details']['input_tokens'] for e in completed),
            evaluated_token_heads=sum(e['details']['head_evaluated'] for e in completed),
            delivered_tokens=sum(e['name'] in ('a.token_delivered','b.token_delivered') for e in events))
    return dict(snapshot_count=len(series),snapshots=series,final=series[-1],work_by_request=by_request,
        scope='Only independently replayed protocol snapshots, after each completed step and final drain. Peaks are maxima over these snapshots, not continuous memory sampling. Growth, copied bytes, and reuse are allocator bookkeeping counters; they do not measure physical memory traffic, allocation latency, or OS/device memory pressure. Reserved pages are a per-layer logical admission budget, not a physical backing-capacity limit.')


def cost_observations(trial):
    decisions={e['details']['step_id']:e['details'] for e in trial['events'] if e['name']=='policy.decision_finished'}
    result=[]
    for event in trial['events']:
        if event['name']!='policy.observed': continue
        observation=event['details'];decision=decisions[observation['step_id']]
        result.append(dict(step_id=observation['step_id'],tokens=observation['tokens'],reason=decision['reason'],
            predicted_ns=decision['predicted_ns'],actual_ns=observation['actual_ns'],prediction_error_ns=observation['prediction_error_ns'],
            budget_overshoot_ns=observation['budget_overshoot_ns'],head_evaluated=observation['head_evaluated'],
            rho_before_ns_per_token=observation['rho_before_ns_per_token'],rho_after_ns_per_token=observation['rho_after_ns_per_token']))
    return result


def trial_groups_checked(document,checked,policy):
    need(document['config']['policy']==policy and exact(document['config']['trials'],1) and exact(document['config']['warmup'],1),
         'cell must have exactly one distinct warmup and one main trial for the planned policy')
    need(len(checked['trials'])==len(checked['warmups'])==1,'cell trial groups incomplete')
    main,warm=checked['trials'][0],checked['warmups'][0]
    need(exact(main['trial_id'],0) and main['warmup'] is False and exact(warm['trial_id'],-1) and warm['warmup'] is True,'warmup/main identities mixed')
    return main,warm


def inspect_cell(batch,cell,plan_sha,audit):
    root=Path(batch)/cell['cell_id'];raw=root/'execution/stdout.json'
    required=[root/'manifest.json',root/'execution/run.json',raw,root/'execution/stderr.log']
    missing=[str(path) for path in required if not path.is_file()]
    if missing: return dict(cell,passed=False,status='missing',missing_files=missing)
    try:
        document=json.loads(raw.read_text());manifest=json.loads((root/'manifest.json').read_text())
        checked=audit.analyze_document(document)
        main,warm=trial_groups_checked(document,checked,cell['policy'])
        artifact=audit.artifact_audit(raw,root,False);derivation=audit.clone_derivation_checked(root,manifest)
        need(derivation['kind']=='verified-batch-clone' and derivation['plan_sha256']==plan_sha and exact(derivation['cell'],cell),'cell escaped matrix plan')
        run=json.loads((root/'execution/run.json').read_text());argv=run['command']
        need(argv[argv.index('--attempt-manifest')+1]==derivation['original_manifest_path'],'actual execution differs from planned prepared cell')
        source={key:manifest[key] for key in ('payload_files','runtime','external_dependencies','generated_runtime_templates','reference_origin')}
        source['model']=manifest['run_spec']['model'];source['inputs']=document['inputs']
        source['config']={k:v for k,v in document['config'].items() if k not in ('policy','b_present')}
        source['numerical_targets']={k:document['reference'][k]['output_token_ids'] for k in ('A','B-whole')}
        return dict(cell,passed=True,status='audited',source=str(raw),raw_sha256=sha(raw),manifest_sha256=sha(root/'manifest.json'),
            artifacts=artifact,derivation=derivation,recorded_run=run,
            main=dict(main,scalars=trial_scalars(main['metrics'],cell['policy']),a_delivery_ms_from_work_started=delivery_curve(document['trials'][0]),
                      resources=resource_observations(document['trials'][0]),
                      cost_slices=cost_observations(document['trials'][0]) if cell['policy']=='cost-200' else None),
            warmup=dict(warm,a_delivery_ms_from_work_started=delivery_curve(document['warmups'][0]),
                        resources=resource_observations(document['warmups'][0]),
                        cost_slices=cost_observations(document['warmups'][0]) if cell['policy']=='cost-200' else None),
            _common_identity=source)
    except (ValueError,KeyError,TypeError,IndexError,OSError,AttributeError,ZeroDivisionError,SyntaxError) as error:
        return dict(cell,passed=False,status='failed-audit',source=str(raw),raw_sha256=sha(raw),errors=[str(error)])


def paired_values(left,right,identities=None):
    need(len(left)==len(right) and bool(left),'paired observations must be nonempty/equal length')
    need(all(finite(v) for v in left+right),'nonfinite pair')
    identities=identities or [dict(pair_index=i) for i in range(len(left))]
    need(len(identities)==len(left),'pair identity length')
    pairs=[]
    for l,r,identity in zip(left,right,identities):
        pairs.append(dict(identity,left=l,right=r,delta=l-r,ratio=None if r==0 else l/r,
                          ratio_defined=r!=0))
    return dict(pairs=pairs,delta=summary([p['delta'] for p in pairs]),
                ratio=summary([p['ratio'] for p in pairs if p['ratio_defined']]),
                calculation='Subtract/divide within each block first, then summarize those six pairs; never difference/ratio of marginal medians.')


def paired_comparison(blocks,left_policy,right_policy,names):
    result={}
    identities=[dict(block=b,left_cell=blocks[b][left_policy]['cell_id'],right_cell=blocks[b][right_policy]['cell_id']) for b in range(6)]
    for metric in names:
        left=[blocks[b][left_policy]['main']['scalars'][metric] for b in range(6)]
        right=[blocks[b][right_policy]['main']['scalars'][metric] for b in range(6)]
        result[metric]=paired_values(left,right,identities)
    return dict(left_policy=left_policy,right_policy=right_policy,metrics=result)


def summarize_cost(rows):
    per_trial=[]
    for row in rows:
        slices=row['main']['cost_slices'];need(slices and sum(s['head_evaluated'] for s in slices)==1,'cost final head observation missing/duplicate')
        predicted=[s for s in slices if s['predicted_ns'] is not None]
        errors=[s['prediction_error_ns']/1e6 for s in predicted]
        final=next(s for s in slices if s['head_evaluated'])
        per_trial.append(dict(block=row['block'],cell_id=row['cell_id'],slices=slices,slice_count=len(slices),
            predicted_slices=len(predicted),bootstrap_slices=sum(s['reason']=='bootstrap' for s in slices),
            fallback_slices=sum(s['reason']=='minimum_progress_fallback' for s in slices),
            budget_exceeded_slices=sum(s['budget_overshoot_ns']>0 for s in slices),
            budget_exceeded_fraction=sum(s['budget_overshoot_ns']>0 for s in slices)/len(slices),
            prediction_error_ms=summary(errors),budget_overshoot_ms=summary([s['budget_overshoot_ns']/1e6 for s in slices]),
            final_head=dict(final,prediction_error_ms=None if final['prediction_error_ns'] is None else final['prediction_error_ns']/1e6)))
    return dict(per_trial=per_trial,
        trial_level={key:summary([r[key] for r in per_trial]) for key in ('slice_count','predicted_slices','bootstrap_slices','fallback_slices','budget_exceeded_slices','budget_exceeded_fraction')},
        final_head_prediction_error_ms=summary([r['final_head']['prediction_error_ms'] for r in per_trial if r['final_head']['prediction_error_ms'] is not None]),
        pooled_descriptive_counts=dict(trials=len(per_trial),slices=sum(r['slice_count'] for r in per_trial),
            predicted_slices=sum(r['predicted_slices'] for r in per_trial),budget_exceeded_slices=sum(r['budget_exceeded_slices'] for r in per_trial)),
        scope='Each trial is the independent reporting unit. Slice errors remain grouped by trial; pooled counts are descriptive, not extra independent repeats. The 200 ms budget is not an A ITL deadline, and fallback/overshoot never fails correctness.',
        prediction_has_separate_final_head_term=False)


def aggregate_rows(rows):
    need(len(rows)==36 and all(r.get('passed') is True for r in rows),'statistics require all 36 audited cells')
    blocks={b:{} for b in range(6)}
    for row in rows:
        need(row['policy'] not in blocks[row['block']],'duplicate policy in block')
        blocks[row['block']][row['policy']]=row
    need(all(set(block)==set(POLICIES) for block in blocks.values()),'incomplete policy block')
    by_policy={}
    for policy in POLICIES:
        selected=[blocks[b][policy] for b in range(6)]
        names=list(A_METRICS)+['work_ms','through_drain_ms']+list(WORK_RESOURCE_METRICS)
        if policy!='no-b': names+=['b_ttft_ms','b_admission_ms','b_ready_wait_ms','model_input_tokens_per_second','output_tokens_per_second',
            'b_chunks','b_tokens_while_A_runnable','b_tokens_after_A_completed','b_tail_service_ms','policy_decision_total_ms']
        by_policy[policy]=dict(main_trials=6,warmups_excluded=6,cell_ids=[r['cell_id'] for r in selected],
            metrics={name:summary([r['main']['scalars'][name] for r in selected]) for name in names},
            resource_counters={name:summary([r['main']['resources']['final'][name] for r in selected])
                for name in ('storage_growths_across_layers','copied_bytes_on_growth_across_layers','reused_page_allocations_across_layers')},
            resource_counter_scope='Final cumulative allocator counters within each measured trial; detailed snapshot/event identities and per-layer counters remain in cells.main.resources. Warmups excluded. These are not physical traffic measurements.',
            partitions=[dict(block=r['block'],chunks=r['main']['partition']) for r in selected])
    curves=[]
    for policy in POLICIES[1:]:
        per_block=[]
        for block in range(6):
            left=blocks[block][policy]['main']['a_delivery_ms_from_work_started']
            right=blocks[block]['no-b']['main']['a_delivery_ms_from_work_started']
            need(len(left)==len(right),'paired A output curve length differs')
            per_block.append(dict(block=block,left_cell=blocks[block][policy]['cell_id'],baseline_cell=blocks[block]['no-b']['cell_id'],
                                  a_output_indices=list(range(len(left))),delta_ms=[a-b for a,b in zip(left,right)]))
        curves.append(dict(policy=policy,baseline='no-b',per_block=per_block,
                           scope='Descriptive paired A delivery offsets from each trial work start; output indices are correlated points, not independent trials.'))
    return dict(statistical_unit='One complete measured trial per cell; six paired blocks per contrast. Warmups are separate, never replicates.',
        no_b_A_interference_baseline=by_policy['no-b'],
        B_present_equal_input_output_work={p:by_policy[p] for p in POLICIES[1:]},
        paired_A_vs_no_b=[paired_comparison(blocks,p,'no-b',A_METRICS) for p in POLICIES[1:]],
        paired_cost_vs_B_conditions=[paired_comparison(blocks,'cost-200',p,B_METRICS) for p in ('whole','fixed-32','fixed-128','fixed-512')],
        paired_A_delivery_curves=curves,cost_estimator=summarize_cost([blocks[b]['cost-200'] for b in range(6)]),
        interpretation='No no-B versus B-present total-throughput ranking. Equal input/head targets do not imply equal FLOPs or allocation work. Small-sample medians/ranges and every pair are reported; no stable p95, worst-case bound, winner selection, or speed-based pass criterion.')


def analyze_matrix(batch,checker_path):
    batch=Path(batch).resolve();plan_path=batch/'plan.json';plan=json.loads(plan_path.read_text());cells=validate_plan(plan)
    audit=load_checker(checker_path);plan_sha=sha(plan_path)
    rows=[inspect_cell(batch,cell,plan_sha,audit) for cell in cells]
    result=dict(schema_version=1,experiment='tiny-decode-cost-matrix-independent-analysis',passed=False,batch=str(batch),
        plan_sha256=plan_sha,shape=plan['shape'],expected_cells=36,cells=rows,
        completed_cell_audits=sum(row['passed'] for row in rows),expected_main_trials=36,expected_warmup_trials=36,
        independent_checker_sha256=CHECKER_SHA256,errors=[],statistics=None,
        external_full_bytes_rehashed_by_this_matrix_analysis=False)
    if not all(row['passed'] for row in rows):
        result['errors'].append('Missing/failed cells prevent complete-matrix acceptance; partial raw evidence retained and no matrix statistics produced.')
        for row in rows: row.pop('_common_identity',None)
        return result
    try:
        common=rows[0]['_common_identity']
        for row in rows:
            need(exact(row['_common_identity'],common),'matrix source/runtime/model/fixture/isolated numerical target differs across cells')
            need(all(row['_common_identity']['config'][k]==v for k,v in plan['shape'].items()),'measured shape escaped plan')
        result['common']=dict(payload_files=len(common['payload_files']),payload_identity_sha256=identity_sha(common['payload_files']),
            external_dependencies=len(common['external_dependencies']),external_identity_sha256=identity_sha(common['external_dependencies']),
            runtime_identity_sha256=identity_sha(common['runtime']),fixture_sha256=common['inputs']['fixture_sha256'],
            all_actual_inputs_and_isolated_targets_equal=True)
        result['process_order']=serial_order_checked(rows)
        result['statistics']=aggregate_rows(rows)
        result['accepted_main_trials']=36;result['verified_warmups']=36;result['passed']=True
    except (ValueError,KeyError,TypeError,IndexError,AttributeError,ZeroDivisionError) as error:
        result['errors'].append(str(error));result['statistics']=None
    for row in rows: row.pop('_common_identity',None)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch',type=Path,required=True)
    parser.add_argument('--checker',type=Path,default=Path(__file__).with_name('analyze_decode_cost.py'))
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try: result=analyze_matrix(args.batch,args.checker)
    except (ValueError,KeyError,TypeError,IndexError,OSError,AttributeError,ZeroDivisionError,SyntaxError) as error:
        result=dict(passed=False,errors=[str(error)],statistics=None,batch=str(args.batch))
    result['matrix_analyzer_sha256']=sha(__file__)
    encoded=json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False)+'\n'
    if args.output:
        with args.output.open('x') as stream: stream.write(encoded)
    else: print(encoded,end='')
    return 0 if result['passed'] else 1


if __name__=='__main__': raise SystemExit(main())
