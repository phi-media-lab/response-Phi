#!/usr/bin/env python3
"""Independent schedule/record replay and G/P pairing for the fixed P6 matrix."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import random
import statistics
import sys
from analyze_control_location import analyze_document, audit_artifacts, digest, strict_json
from control_math import require

SYMBOLS = {'H':'cpu-feedback','G':'gpu-step','P':'gpu-preencoded','F':'device-loop-global'}
ROWS = ('HGFP','GPHF','PFGH','FHPG')
METRICS = ('work_ms','encoding_ms','first_submit_to_completion_ms','gpu_execution_sum_ms',
           'gpu_span_ms','cpu_selection_ms','validation_ms','full_trial_ms')


def expected_cells():
    # Reconstruct from the preregistered mathematical order; no driver import.
    block_ids = list(range(8))
    random.Random(20261029).shuffle(block_ids)
    cells = []
    for execution_block, block in enumerate(block_ids):
        replicate, row = divmod(block,4)
        for t_position, rounds in enumerate((16,64) if replicate == 0 else (64,16)):
            for mode_position, symbol in enumerate(ROWS[row]):
                sequence = len(cells)
                cells.append({'sequence': sequence,'cell_id':f'{sequence:02d}-block-{block:02d}-t{rounds}-{symbol}',
                    'block_id':f'block-{block:02d}','execution_block':execution_block,'williams_row':row,
                    'replicate':replicate,'T':rounds,'T_position':t_position,'mode':SYMBOLS[symbol],
                    'mode_symbol':symbol,'mode_position':mode_position})
    return cells


def check_schedule(schedule):
    cells = expected_cells()
    require(schedule['cells'] == cells, 'matrix order differs from fixed preregistration')
    required = {'schema_version':1,'experiment':'metal-control-location-normal-matrix','shuffle_seed':20261029,
                'rounds':[16,64],'blocks_per_T':8,'main_trials_per_cell':1,'warmups_per_cell':1,
                'main_trials_total':64,'warmups_total':64,'cancel_policy':'none','modes':SYMBOLS,
                'williams_rows':[list(row) for row in ROWS]}
    for key,value in required.items():
        require(schedule[key] == value, 'schedule metadata differs: '+key)
    require(len(schedule['blocks']) == 8, 'block inventory')
    for block, start in zip(schedule['blocks'], range(0,64,8)):
        first = cells[start]
        require(block == {'block_id':first['block_id'],'williams_row':first['williams_row'],
            'replicate':first['replicate'],'T_order':[cells[start]['T'],cells[start+4]['T']],
            'mode_order':list(ROWS[first['williams_row']]),'execution_block':first['execution_block']}, 'block metadata differs')
    return cells


def expected_command(plan, cell):
    return [plan['binary'],'--reference',plan['references'][str(cell['T'])], '--mode',cell['mode'],
            '--rounds',str(cell['T']),'--cancel-policy','none','--warmups','1','--trials','1',
            '--timeout-seconds','30','--json']


def summary(values):
    require(len(values) == 8, 'each distribution must contain exactly eight measured samples')
    return {'n':len(values),'median':statistics.median(values),'min':min(values),'max':max(values),'values':values}


def summarize_trials(rows):
    require(len(rows)==64, 'missing measured rows')
    paired=defaultdict(dict)
    for row in rows:
        key=(row['T'],row['block_id']);mode=row['mode_symbol']
        require(mode not in paired[key], 'duplicate mode in paired block')
        paired[key][mode]=row
    require(len(paired)==16 and all(set(group)==set(SYMBOLS) for group in paired.values()), 'incomplete paired block')
    result={}
    for rounds in (16,64):
        ordered=[paired[(rounds,f'block-{block:02d}')] for block in range(8)]
        modes={symbol:{metric:summary([group[symbol]['metrics'][metric] for group in ordered]) for metric in METRICS} for symbol in SYMBOLS}
        pairs=[]
        for block,group in enumerate(ordered):
            g,p=group['G']['metrics']['work_ms'],group['P']['metrics']['work_ms']
            require(g>0 and p>0, 'invalid pair duration')
            pairs.append({'block_id':f'block-{block:02d}','G_work_ms':g,'P_work_ms':p,
                          'P_minus_G_ms':p-g,'P_over_G':p/g,
                          'G_sequence':group['G']['sequence'],'P_sequence':group['P']['sequence']})
        result[str(rounds)]={'modes':modes,'G_P_pairs':pairs,
            'P_minus_G_ms':summary([p['P_minus_G_ms'] for p in pairs]),
            'P_over_G':summary([p['P_over_G'] for p in pairs]),
            'P_faster_pairs':sum(p['P_minus_G_ms']<0 for p in pairs)}
    return result


def audit(root,bundle=None):
    root=Path(root).resolve()
    binding=strict_json(root/'bundle-binding.json')
    bundle=Path(bundle).resolve() if bundle else Path(binding['bundle'])
    manifest=strict_json(bundle/'manifest.json')
    require(digest(bundle/'manifest.json')==binding['manifest_sha256'],'bundle binding changed')
    require(manifest==strict_json(root/'frozen-manifest.json'),'recorded bundle manifest differs')
    actual={str(p.relative_to(bundle)) for p in bundle.rglob('*') if p.is_file()}
    require(actual==set(manifest['files'])|{'manifest.json'},'frozen bundle inventory differs')
    for name,sha in manifest['files'].items():
        relative=Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts,'unsafe bundle path')
        require(digest(bundle/name)==sha,'frozen bundle bytes differ: '+name)
    # Root performs the same numeric replay implementation that was frozen.
    for name in ('analyze_control_location.py','control_math.py'):
        require(digest(Path(__file__).with_name(name))==digest(bundle/'source'/name),'numeric audit implementation differs from frozen checker')
    plan=strict_json(root/'plan.json')
    require(plan==strict_json(bundle/'plan.json'),'matrix plan changed')
    schedule=strict_json(root/'schedule.json')
    require(schedule==plan['schedule']==strict_json(bundle/'source/schedule.json'),'schedule files differ')
    cells=check_schedule(schedule)
    require(plan['cases']==[dict(c,command=expected_command(plan,c)) for c in cells],'planned command mapping differs')
    result=strict_json(root/'matrix-result.json')
    require(result['passed'] is True and result['expected_cells']==64 and result['completed_cells']==64,'matrix failed or incomplete')
    require(result['performance_improvement_required'] is False and result['warmups_are_main_samples'] is False,'wrong acceptance/counting policy')
    require(len(result['results'])==64,'matrix result rows missing')
    progress=[json.loads(line) for line in (root/'progress.jsonl').read_text().splitlines()]
    require(progress==result['results'],'progress/result disagreement')
    require({p.name for p in root.iterdir() if p.is_dir()}=={c['cell_id'] for c in cells},'missing/extra cell directory')
    measured=[];warmups=[];outputs=[]
    for cell,row in zip(cells,result['results']):
        cid=cell['cell_id'];directory=root/cid;raw_path=directory/'stdout.json'
        require(row['cell_id']==cid and row['sequence']==cell['sequence'] and row['passed'] is True,'progress does not follow schedule')
        require(row['process_returncode']==0 and row['analysis_returncode']==0,'cell process or checker failed')
        raw=strict_json(raw_path);run=strict_json(directory/'run.json')
        require(run['command']==expected_command(plan,cell) and run['cwd']==plan['workspace'],'executed command differs from preregistration')
        c=raw['config']
        require(c['mode']==cell['mode'] and c['T']==cell['T'] and c['warmups']==c['trials']==1 and c['cancel_policy']=='none','cell config differs')
        replay=analyze_document(raw)
        require(len(replay)==2 and replay[0]['warmup'] is True and replay[1]['warmup'] is False,'warmup/main incorrectly assigned')
        require(row['warmup_trial_id']==0 and row['main_trial_id']==1,'wrong trial identities')
        artifacts=audit_artifacts(raw_path,raw,build_dir=bundle/'artifacts/attempt-1',reference_path=bundle/f"references/t{cell['T']}.json")
        for field,path in [('raw_sha256',raw_path),('run_sha256',directory/'run.json'),('analysis_sha256',directory/'analysis.json')]:
            require(row[field]==digest(path),'progress artifact hash differs: '+field)
        saved=strict_json(directory/'analysis.json')
        require(saved['passed'] is True and len(saved['results'])==1 and saved['results'][0]['passed'] is True,'saved analysis failed')
        require(saved['results'][0]['trials']==replay,'saved analysis differs from independent replay')
        audit_run=strict_json(directory/'audit/run.json')
        require(audit_run['returncode']==0 and audit_run['timed_out'] is False,'recorded checker failed')
        for name,info in audit_run['files'].items():
            require(digest(directory/'audit'/name)==info['sha256'] and (directory/'audit'/name).stat().st_size==info['bytes'],'recorded checker output changed')
        measured.append(dict(cell,trial_id=1,metrics=replay[1]['metrics']))
        warmups.append(dict(cell,trial_id=0,metrics=replay[0]['metrics']))
        outputs.append({'cell_id':cid,'stdout_sha256':digest(raw_path),'run_sha256':digest(directory/'run.json'),
                        'analysis_sha256':digest(directory/'analysis.json'),'artifacts':artifacts})
    return {'passed':True,'cells':64,'measured_trials':len(measured),'warmup_trials':len(warmups),
            'bundle_manifest_sha256':binding['manifest_sha256'],'schedule_sha256':digest(root/'schedule.json'),
            'matrix_result_sha256':digest(root/'matrix-result.json'),'main':summarize_trials(measured),
            'measured':measured,'warmups':warmups,'outputs':outputs,
            'scope':'64 measured trials and 64 separate warmups; full integer/event/artifact replay; G/P paired within T/block; finite pilot, no performance PASS or hard bound'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path)
    parser.add_argument('--bundle',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try: report=audit(args.root,args.bundle)
    except (ValueError,KeyError,TypeError,IndexError,OSError) as error:report={'passed':False,'error':str(error)}
    report['checker_sha256']=digest(__file__)
    rendered=json.dumps(report,indent=2,allow_nan=False)+'\n'
    if args.output:
        with args.output.open('x') as f:f.write(rendered)
    else: print(rendered,end='')
    return 0 if report['passed'] else 1


if __name__=='__main__':sys.exit(main())
