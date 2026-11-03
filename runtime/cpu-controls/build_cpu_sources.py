#!/usr/bin/env python3
"""Six pure CM2 sources for seven CPU groups / eight serial worker fixtures.

Imports only the fixed pure source constructors. No checker/runtime/worker is
imported here; the root freezes this file before its first generation.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CONSTRUCTORS=ROOT/'programs/build_cm2_programs.py'
CONSTRUCTORS_SHA='bdfc3e50af02059a74a43e56c58cc61ef9bf755a6ddb8d1d87103053d5878743'
if hashlib.sha256(CONSTRUCTORS.read_bytes()).hexdigest()!=CONSTRUCTORS_SHA: raise RuntimeError('CM2 pure constructor pin')
spec=importlib.util.spec_from_file_location('cm2_cpu_pure_data',CONSTRUCTORS)
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
m=f.m


def run(body,effect,result):
    names,facts=m.run_parts('A')
    command=f.c('run',owner=f.var('owner'),names=names,name='AScope',body=body)
    eps=f.union(f.eff(f.atom('Raise')),f.nu(names,facts,f.union(effect,
        f.eff(f.atom('Enter',scope='AS'),f.atom('Drain',scope='AS'),f.atom('Raise')))))
    return m.with_owner(command,eps,result)


def rebind(value,mapping):
    if isinstance(value,str): return mapping.get(value,value)
    if isinstance(value,list): return [rebind(v,mapping) for v in value]
    if isinstance(value,dict): return {k:rebind(v,mapping) for k,v in value.items()}
    return value


def owned_coexist():
    # The second attempt is ordinary checked source, reached only after a real
    # first publication. Its SourceFailure is caught; it must create no Job.
    names,facts=m.producer_parts('Second',f.t('Word'))
    facts=rebind(facts,{'SecondS':'AS','SecondG':'AG','SecondR':'AR'})
    second=f.c('produce',scope=f.var('AScope'),kernel='Prefill32',
        argument=m.tokens_value([f.word(x) for x in m.A32]),names=names,
        cleanup={'captures':[],'parameter':{'name':'SecondRead'},'result':f.t('Word'),
            'body':f.c('load',read=f.var('SecondRead'),index=f.v('nat',literal=0))},
        result_name='SecondH',control_name='SecondK',next=f.ret())
    second_effect=f.nu(names,facts,f.union(m.read_effect(),f.eff(f.atom('Register',scope='AS'),
        f.atom('Issue',owner='O'),f.atom('Read',owner='O'),f.atom('Raise'))))
    caught=f.c('try',body=second,failure_name='duplicateFailure',handler=f.ret())
    final=f.bind('duplicateHandled',caught,f.ret(f.pair(f.var('fnWord'),f.var('aWord'))))
    leased,le=f.fork('leased','AScope','AS','AR',f.t('Unit'),f.ret(),[],final,next_effect=second_effect)
    after_eval=f.bind('aWord',m.await_payload('AH','FirstA'),leased)
    evaluating,ee=f.fork('eval','AScope','AS','AR',f.t('Unit'),f.ret(),[],after_eval,
        next_effect=f.union(m.need('AP'),le))
    producer=m.producer_run('A','Prefill32',m.tokens_value([f.word(x) for x in m.A32]),evaluating)['body']
    pn,pf=m.producer_parts('A',f.t('Word'))
    pe=f.nu(pn,pf,f.union(m.read_effect(),ee,f.eff(f.atom('Register',scope='AS'),
        f.atom('Issue',owner='O'),f.atom('Read',owner='O'),f.atom('Raise'))))
    consume=f.bind('returnedFn',f.c('await',shared=f.var('beforeH')),
        f.bind('fnWord',f.call('returnedFn'),producer))
    before,be=f.fork('before','AScope','AS','AR',f.fn_type(),f.ret(f.pure_fn()),[],consume,
        next_effect=f.union(f.terminal('beforeP'),pe))
    return run(before,be,f.t('Pair',left=f.t('Word'),right=f.t('Word')))


def owned_ordinary_only():
    ordinary,oe=f.fork('Only','AScope','AS','AR',f.t('Word'),f.ret(f.word(202)),[],
        f.c('await',shared=f.var('OnlyH')),next_effect=f.terminal('OnlyP'))
    return run(ordinary,oe,f.t('Word'))


def cached_early_cancel():
    choice=f.t('Sum',left=f.t('Word'),right=f.t('Unit'))
    result=f.t('Pair',left=choice,right=f.t('Word'))
    selected=f.c('case',value=f.var('rawChoice'),left_name='delivered',
        left=f.ret(f.v('inl',sum_type=choice,payload=f.var('delivered'))),right_name='detached',
        right=f.ret(f.v('inr',sum_type=choice,payload=f.var('detached'))))
    terminal=f.bind('terminalWord',f.c('await',shared=f.var('SeedH')),
        f.ret(f.pair(f.var('selected'),f.var('terminalWord'))))
    body=f.bind('rawChoice',f.c('await_cancel',shared=f.var('SeedH')),f.bind('selected',selected,terminal))
    parent=f.c('request_cancel',control=f.var('WK'),next=f.c('await',shared=f.var('WH')))
    waiter,we=f.fork('W','TestScope','TestS','TestR',result,body,['SeedH'],parent,
        body_effect=f.union(m.need('SeedP'),f.terminal('SeedP')),
        next_effect=f.union(f.eff(f.atom('RequestCancel',job='WP')),f.terminal('WP')))
    next=f.bind('cachedSeed',f.c('await',shared=f.var('SeedH')),waiter)
    seed,se=f.fork('Seed','TestScope','TestS','TestR',f.t('Word'),f.ret(f.word(23)),[],next,
        next_effect=f.union(f.terminal('SeedP'),we))
    return f.in_scope(seed,se,result)


def ordinary_fault():
    # The new ordinary caller, not main, enters this direct synchronous child.
    # Its registration Scope stack is an anchor; ChildScope belongs to FaultP.
    child,ce=f.scope('Child',f.throw_body(),f.eff(f.atom('Raise')))
    fault,fe=f.fork('Fault','InnerScope','InnerS','InnerR',f.t('Word'),child,[],
        f.c('await',shared=f.var('FaultH')),body_effect=ce,next_effect=f.terminal('FaultP'))
    inner,ie=f.scope('Inner',fault,fe)
    caught=f.c('try',body=inner,failure_name='handledOriginal',handler=f.ret(f.word(5005)))
    return f.in_scope(caught,ie,f.t('Word'))


def closed_target():
    denied,de=f.fork('Denied','TestScope','TestS','TestR',f.t('Unit'),f.ret(),[],f.ret())
    waiter,we=f.fork('W','TestScope','TestS','TestR',f.t('Unit'),denied,['TestScope'],f.ret(),body_effect=de)
    inner,ie=f.scope('Test',waiter,we)
    # Parent returns from its source body without awaiting W; ScopeExit starts
    # draining. The trusted native policy opens W's original START only then.
    body=f.bind('scopeSucceeded',inner,f.ret(f.word(0)))
    caught=f.c('try',body=body,failure_name='closedRegistration',handler=f.ret(f.word(6006)))
    return f.closed(caught,ie,f.t('Word'))


CASES=('owned_coexist','owned_ordinary_only','normal','cached_early_cancel','ordinary_fault','closed_target')
BUILDERS={'owned_coexist':owned_coexist,'owned_ordinary_only':owned_ordinary_only,'normal':f.waiter_source,
    'cached_early_cancel':cached_early_cancel,'ordinary_fault':ordinary_fault,'closed_target':closed_target}
FIXTURES=(
    {'id':'C1','source':'owned_coexist','scenario':'cm2_eval_hold','policy':'ownedCoexist','evals':1,'acks':1,
     'result':'Pair(Word17,actual-A-Word)','handled_fault':'PrefillAlreadyClaimed'},
    {'id':'C2','source':'owned_ordinary_only','scenario':'success','policy':'none','evals':0,'acks':0,'result':'Word202'},
    {'id':'C3N','source':'normal','scenario':'success','policy':'none','evals':2,'acks':2,'wait_choice':'either','marker':7001},
    {'id':'C3D','source':'normal','scenario':'success','policy':'attachedCancellation','evals':2,'acks':2,'wait_choice':'right','marker':7001},
    {'id':'C4','source':'cached_early_cancel','scenario':'success','policy':'earlyCancel','evals':0,'acks':0,'result':'Pair(Inr(Unit),Word23)'},
    {'id':'C5','source':'ordinary_fault','scenario':'success','policy':'none','evals':0,'acks':0,'result':'Word5005','handled_fault':'BodyRequestedFailure'},
    {'id':'C6','source':'closed_target','scenario':'success','policy':'closedTarget','evals':0,'acks':0,'result':'Word6006','handled_fault':'RegistrationClosed'},
    {'id':'C7','source':'normal','scenario':'lost_release_ack','policy':'attachedCancellation','evals':1,'acks':0,'outcome':'host-incomplete/Unknown','marker':7001})


def source(case): return BUILDERS[case]()

def selectors(raw):
    result=f.source_sites(raw)
    for path,node in f.nodes(raw):
        pointer='/'+'/'.join(map(str,path))
        if node.get('command')=='fork':
            name=node.get('result_name')
            if name in ('beforeH','evalH','leasedH','WH','DeniedH','FaultH','OnlyH','SeedH'):
                result[name]=pointer
        if node.get('command')=='await_cancel' and 'job' in path: result['waiter_await']=pointer
        if node.get('command')=='scope' and node.get('name')=='TestScope': result['closed_target_scope']=pointer
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=False);rows=[]
    for name in CASES:
        raw=source(name);data=f.encode(raw);path=args.output_dir/(name+'.json');path.write_bytes(data)
        rows.append({'case':name,'path':path.name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
            'canonical_sha256':hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest(),
            'sites':selectors(raw)})
    result={'schema':'cm2-cpu-closed-sources-v1','candidate_executed':False,'checked':False,
        'generator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'constructors_path':str(CONSTRUCTORS),'constructors_sha256':CONSTRUCTORS_SHA,
        'cm1_helper_sha256':f.BASE_SHA,'a2_helper_sha256':m.HELPER_SHA256,'cases':rows,'fixtures':FIXTURES}
    (args.output_dir/'manifest.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))

if __name__=='__main__': main()
