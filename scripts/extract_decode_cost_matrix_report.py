#!/usr/bin/env python3
"""Report projection of a SHA-bound accepted matrix audit and its unchanged raw.

Not a replacement correctness checker. No harness/model imports or execution.
All main/warmup facts remain separate; no new performance acceptance criterion.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics

MATRIX_SHA='245f9161cdea7db99667674a85bd5ccf0b80b6a9acd8d9ede7a842a4612b8f1f'
CHECKER_SHA='6a9a6149fac3f098bff2c8fb6e0a80b79ce38bcdf27ebeedefc675375da347b4'
POLICIES=('no-b','whole','fixed-32','fixed-128','fixed-512','cost-200')
AFTER=('a_after_anchor_itl_max_ms','a_after_anchor_itl_median_ms','a_after_anchor_itl_sum_ms')

def require(ok,message):
    if not ok: raise ValueError(message)

def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def describe(values):
    return dict(values=values,count=len(values),min=min(values),median=statistics.median(values),max=max(values)) if values else dict(values=[],count=0)

def resource_fact(trial):
    snapshots=trial['observations']['snapshots'];events=trial['events']
    release=[e for e in events if e['name']=='resource.released' and e['request_id']=='A']
    require(len(release)==1,'A release identity')
    release_seq=release[0]['seq'];last_a=next(s for s in reversed(snapshots) if 'A' in s['owners'])
    a_pages={r['layer']:r['page_ids'] for r in last_a['owners']['A']}
    reused=[]
    for layer,pages in a_pages.items():
        for snap in snapshots:
            if snap['event_seq']<=release_seq or 'B' not in snap['owners']: continue
            b=next(p for p in snap['owners']['B'] if p['layer']==layer)
            common=sorted(set(pages)&set(b['page_ids']))
            if common:
                reused.append(dict(layer=layer,page_ids=common,first_observed_snapshot_seq=snap['event_seq'],label=snap['label']))
                break
    changes=[];previous=None
    names=('layer','allocated_page_ids','capacity_pages','backing_bytes','storage_growths','copied_bytes_on_growth','reused_page_allocations')
    for snapshot in snapshots:
        layers=[{k:p[k] for k in names} for p in snapshot['pools']]
        signature=(layers,sorted(snapshot['owners']))
        if signature!=previous:
            changes.append(dict(label=snapshot['label'],event_seq=snapshot['event_seq'],owners=snapshot['owners'],layers=layers))
        previous=signature
    final=snapshots[-1];pools=final['pools']
    return dict(a_release_event_seq=release_seq,last_snapshot_A_owned=last_a['event_seq'],
        final_per_layer=[{k:p[k] for k in names} for p in pools],
        final_unique_capacity_pages=sorted({p['capacity_pages'] for p in pools}),
        final_unique_allocated_page_ids=sorted({p['allocated_page_ids'] for p in pools}),
        a_page_ids_later_observed_in_B=reused,resource_change_snapshots=changes,
        all_final_used_empty=all(not p['used_page_ids'] for p in pools),final_reserved=final['reservations']['used'],
        scope='Already audited logical page IDs and recorded business-cache snapshots. Reuse observation is after A release; no physical pointer identity. Growth includes initial zero-to-four allocation; copy counter is not measured memory traffic, and snapshots are not process RSS or continuous/transient peaks.')

def trial_fact(raw,checked,config):
    events=raw['events'];metrics=checked['metrics']
    a_tokens=[e for e in events if e['name']=='a.token_delivered'];anchor=config['arrival_after_a_decode']
    after=metrics['a_after_anchor_itl_ms']
    require(len(after)==config['a_decode_steps']-anchor,'post-anchor interval count')
    require(after==[(b['t_ns']-a['t_ns'])/1e6 for a,b in zip(a_tokens[anchor:],a_tokens[anchor+1:])],'post-anchor event projection')
    completed=[e for e in events if e['name']=='step.completed' and e['request_id']=='B']
    by_phase={phase:[e['details']['input_tokens'] for e in completed if e['details']['a_runnable_before_step']==flag]
              for phase,flag in [('while_A_runnable',True),('after_A_completed',False)]}
    observed={e['details']['step_id']:e for e in events if e['name']=='policy.observed'}
    decision={e['details']['step_id']:e for e in events if e['name']=='policy.decision_finished'}
    slices=[]
    for done in completed:
        step=done['details'];o=observed[step['step_id']];d=decision[step['step_id']]
        slices.append(dict(phase='while_A_runnable' if step['a_runnable_before_step'] else 'after_A_completed',
            step_id=step['step_id'],completion_event_seq=done['seq'],decision_event_seq=d['seq'],observation_event_seq=o['seq'],
            decision=d['details'],observation=o['details']))
    require(sum(map(sum,by_phase.values()))==config['b_prompt_length']*int(config['b_present']),'complete B input projection')
    fields=('total_model_input_tokens','total_output_tokens','constructed_head_graphs','evaluated_token_heads',
            'peak_active_page_slots','peak_backing_bytes','peak_reserved_pages_per_layer','work_ms','through_drain_ms','a_completion_ms')
    result=dict(trial_id=checked['trial_id'],warmup=checked['warmup'],
        a_after_anchor_itl_ms=after,a_output_interval_indices=[[i,i+1] for i in range(anchor,config['a_decode_steps'])],
        a_after_anchor_summary=metrics['a_after_anchor_itl_summary'],a_after_anchor_sum_ms=sum(after),
        a_full_itl_summary=metrics['a_user_itl_summary'],metrics={k:metrics[k] for k in fields},
        b_phase_partitions=by_phase,b_slice_records=slices,resources=resource_fact(raw))
    if config['b_present']:
        result['b_tail_metrics']={k:metrics[k] for k in ('b_ttft_ms','b_tokens_while_A_runnable','b_tokens_after_A_completed',
            'b_chunks_while_A_runnable','b_chunks_after_A_completed','b_tail_service_ms')}
    if config['policy']=='cost-200':
        result['cost_by_phase']={phase:dict(slices=len(rows),tokens=sum(r['observation']['tokens'] for r in rows),
            reason_counts=dict(Counter(r['decision']['reason'] for r in rows)),
            actual_over_budget=sum(r['observation']['budget_overshoot_ns']>0 for r in rows),
            head_evaluations=sum(r['observation']['head_evaluated'] for r in rows))
            for phase in by_phase for rows in [[r for r in slices if r['phase']==phase]]}
    return result

def extract(analysis,expected_sha):
    require(sha(analysis)==expected_sha,'matrix audit bytes changed')
    audit=json.loads(Path(analysis).read_text())
    require(audit['passed'] is True and audit['completed_cell_audits']==audit['accepted_main_trials']==audit['verified_warmups']==36,'complete accepted matrix required')
    require(audit['matrix_analyzer_sha256']==MATRIX_SHA and audit['independent_checker_sha256']==CHECKER_SHA,'audit source revision differs')
    rows=[]
    for cell in audit['cells']:
        path=Path(cell['source']);require(sha(path)==cell['raw_sha256'],'audited raw changed: '+str(path))
        document=json.loads(path.read_text());config=document['config']
        require(all(config[k]==v for k,v in audit['shape'].items()),'raw shape differs')
        row={k:cell[k] for k in ('cell_id','block','position','policy','source','raw_sha256')}
        row['recorded_process_started_at_utc']=cell['recorded_run']['started_at_utc']
        row['main']=trial_fact(document['trials'][0],cell['main'],config)
        row['warmup']=trial_fact(document['warmups'][0],cell['warmup'],config)
        rows.append(row)
    per_policy={}
    for policy in POLICIES:
        selected=sorted([r for r in rows if r['policy']==policy],key=lambda r:r['block'])
        stats=audit['statistics']['no_b_A_interference_baseline'] if policy=='no-b' else audit['statistics']['B_present_equal_input_output_work'][policy]
        per_policy[policy]=dict(main_trials=6,warmups_excluded=6,
            selected_metrics={k:v for k,v in stats['metrics'].items() if k in (*AFTER,'a_itl_median_ms','a_itl_max_ms','a_completion_ms','b_ttft_ms','work_ms')},
            main_capacity_by_block=[dict(block=r['block'],unique_capacity_pages=r['main']['resources']['final_unique_capacity_pages'],
                unique_allocated_page_ids=r['main']['resources']['final_unique_allocated_page_ids']) for r in selected],
            main_b_tail_by_block=[dict(block=r['block'],partitions=r['main']['b_phase_partitions'],metrics=r['main'].get('b_tail_metrics')) for r in selected])
    pairs=[]
    for original in audit['statistics']['paired_cost_vs_B_conditions']:
        projection={k:original['metrics'][k] for k in (*AFTER,'b_ttft_ms','work_ms')}
        signs={k:dict(negative=sum(v<0 for v in r['delta']['values']),positive=sum(v>0 for v in r['delta']['values']),zero=sum(v==0 for v in r['delta']['values'])) for k,r in projection.items()}
        pairs.append(dict(left_policy='cost-200',right_policy=original['right_policy'],metrics=projection,difference_sign_counts=signs))
    return dict(schema_version=1,kind='decode-cost-matrix-report-projection',extraction_passed=True,
        analysis=str(Path(analysis).resolve()),analysis_sha256=expected_sha,plan_sha256=audit['plan_sha256'],shape=audit['shape'],
        source_revisions=dict(matrix=MATRIX_SHA,checker=CHECKER_SHA,extractor=sha(__file__)),common=audit['common'],
        cells=rows,main_summary=per_policy,paired_cost_after_anchor=pairs,
        cost_warm_main_timeline=[{k:r[k] for k in ('block','cell_id','recorded_process_started_at_utc','main','warmup')} for r in rows if r['policy']=='cost-200'],
        scope='Projection only after complete independent audit acceptance; each raw SHA rebound. No harness/model execution or new speed-based pass rule. Main/warm separate; paired quantities copied from frozen audit. Process record times describe order, not thermal state, GPU clocks, or a controlled warmup effect.')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--analysis',type=Path,required=True);parser.add_argument('--analysis-sha256',required=True)
    args=parser.parse_args()
    try: result=extract(args.analysis,args.analysis_sha256)
    except (ValueError,KeyError,TypeError,IndexError,OSError) as error: result=dict(extraction_passed=False,errors=[str(error)])
    print(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False));return 0 if result['extraction_passed'] else 1

if __name__=='__main__': raise SystemExit(main())
