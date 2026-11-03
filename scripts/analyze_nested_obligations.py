#!/usr/bin/env python3
"""Independent bounded C3 CPU evidence audit. Never imports the driver or owner."""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re

MANIFEST='63a91b16f74beae8af39e88d5b166fe2a306e64df968ea720dfd96a18f9ef2a4'
CASES=('same-owner-busy','direct-await','drop-child','cancel-parent','five-faults','cancel-failed-sibling',
       'register-before-close','close-before-register','propagated-child-fault','local-awaited-producer-fault',
       'registered-child-busy','return-observation-reuse','cross-run-local-fault')
FAILED={'five-faults','cancel-failed-sibling','propagated-child-fault','local-awaited-producer-fault',
        'registered-child-busy','cross-run-local-fault'}
DETACHED={'drop-child','cancel-parent','five-faults','cancel-failed-sibling','register-before-close','close-before-register'}
SEEDS={'X':0x13579bdf,'Y':0x2468ace0,'Z':0xabcdef01}

def need(v,message):
    if not v:raise ValueError(message)
def load(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def identity(path):return dict(sha256=sha(path),bytes=Path(path).stat().st_size)
def canon(x):return json.dumps(x,sort_keys=True,separators=(',',':'))
def clock(events,indexed=False):
    need(all(type(e['t_ns']) is int and e['t_ns']>=0 for e in events),'integer event clock')
    need(all(a['t_ns']<=b['t_ns'] for a,b in zip(events,events[1:])),'event time order')
    for e in events:
        need(type(e['priority_raw']) is int,'priority sample/callback value')
        if not indexed and e['name']=='producer.escalated':
            need(e.get('priority_source')=='escalation_callback' and 'cancelled' not in e,'escalation callback is not an actual task sample')
        else:need(type(e.get('cancelled')) is bool,'actual task cancellation sample')
    if indexed:need([e['index'] for e in events]==list(range(len(events))),'global composition event indices')
def one(events,name):
    rows=[e for e in events if e['name']==name];need(len(rows)==1,'one '+name);return rows[0]
def ordered(events,names):
    selected=[one(events,n) for n in names]
    need([events.index(e) for e in selected]==sorted(events.index(e) for e in selected),'event chain '+','.join(names))
    return selected

def oracle(seed):
    values=[]
    for i in range(128):
        value=i^seed
        for _ in range(31):value=(value*1664525+1013904223)&0xffffffff
        values.append(value)
    checksum=0x9e3779b97f4a7c15
    for p in range(2):
        for i,value in enumerate(values):
            checksum=(((checksum<<7)|(checksum>>57))&0xffffffffffffffff)^value
            checksum=(checksum+i+p)&0xffffffffffffffff
    return values,checksum

def specification(name):
    need(name in CASES,'unknown case')
    if name=='same-owner-busy':work=[('X.outer','X',SEEDS['X'],False)];busy=['X.inner'];stored=['X.inner','X.outer']
    elif name=='return-observation-reuse':work=[('X.old','X',SEEDS['X'],False),('X.new','X',SEEDS['Y'],False)];busy=[];stored=['X.old','X.new']
    elif name=='cross-run-local-fault':work=[('Y','Y',SEEDS['Y'],True),('X','X',SEEDS['X'],False)];busy=[];stored=['Y','X']
    else:
        owners=['X'] if name in ('local-awaited-producer-fault','registered-child-busy') else ['X','Y']
        if name in ('five-faults','cancel-failed-sibling'):owners.append('Z')
        work=[(o,o,SEEDS[o],name=='five-faults' or name=='cancel-failed-sibling' and o=='Y' or name=='local-awaited-producer-fault') for o in owners]
        busy=['X.busy'] if name=='registered-child-busy' else [];stored=['Y','X'] if name=='direct-await' else ['X']
    children=[]
    if name in DETACHED or name=='propagated-child-fault':children=[('child-Y','Y','Y')]+([('child-Z','Z','Z')] if name in ('five-faults','cancel-failed-sibling') else [])
    if name=='registered-child-busy':children=[('child-busy','X.busy','X')]
    return dict(work=work,busy=busy,stored=stored,children=children,
                owners=set(w[1] for w in work)|({'Late'} if name=='close-before-register' else set()))

def owner_audit(owner,work,busy_count):
    events=owner['events'];clock(events);snapshot=owner['snapshot']
    need(snapshot['phase']=='idle' and snapshot['epoch']==snapshot['leases_granted']==snapshot['leases_returned']==len(work),'owner final grants/returns')
    need(snapshot['busy_rejections']==busy_count and snapshot['expired_rejections']==0,'owner rejection counts')
    need(len(snapshot['sessions'])==len(work),'one historical session per admitted scope')
    allowed={'owner.permit_granted','owner.busy_rejected','owner.obligation_registered','owner.draining','owner.permit_returned',
        'resource.reserved','work.submitted','producer.started','producer.escalated','backend.started','backend.physical_write_ended',
        'backend.completed','completion.observed','cleanup.started','backend.read_started','backend.read_ended','cleanup.ended',
        'work.failed','resource.released','producer.done','drain.started','session.closed','drain.ended'}
    need(all(e['name'] in allowed for e in events),'unexpected owner event/error')
    need(all(e.get('priority_source') in ('sampled','escalation_callback') for e in events),'owner actual priority provenance')
    need(sum(e['name']=='owner.busy_rejected' for e in events)==busy_count,'actual busy events')
    phase='idle';current=0;session_events={};replayed=[]
    for e in events:
        n=e['name'];d=e['details']
        if n=='owner.permit_granted':
            need(phase=='idle' and d==dict(epoch=str(current+1),session_id='session-'+str(current+1)),'new lease before prior return')
            current+=1;phase='leased';session_events[current]=[]
        need(current>0,'owner event without admitted lease')
        session_events[current].append(e)
        if n=='owner.busy_rejected':need(phase in ('leased','draining') and d==dict(epoch=str(current),phase=phase),'busy before new work')
        if n in ('owner.draining','owner.permit_returned','owner.obligation_registered'):
            need(d==dict(epoch=str(current),session_id='session-'+str(current)),'local epoch/session route')
        if n=='owner.draining':need(phase=='leased','drain phase');phase='draining'
        if n=='owner.permit_returned':need(phase=='draining','return phase');phase='idle'
    need(current==len(work) and phase=='idle','all admitted owner sessions returned')
    copied={}
    for epoch,(scope,owner_id,seed,failed) in enumerate(work,1):
        request=scope+'.A';ev=session_events[epoch]
        request_events=[e for e in ev if e['request_id']==request]
        need(all(e['request_id'] in ('harness',request) for e in ev),'unexpected request in owner epoch')
        chain=['work.submitted','producer.started','backend.started','backend.physical_write_ended','backend.completed']
        chain+=['work.failed'] if failed else ['completion.observed','cleanup.started','backend.read_started','backend.read_ended','cleanup.ended']
        chain+=['resource.released','producer.done']
        filtered=[e for e in request_events if e['name'] not in ('owner.obligation_registered','producer.escalated')]
        need([e['name'] for e in filtered]==chain,'actual producer access/scan/release chain '+request)
        need(all(e['cancelled'] is False for e in request_events if e['name'] not in ('owner.obligation_registered','work.submitted','producer.escalated')),'parent cancellation must not silently cancel producer')
        for e in request_events:
            if e['name'] not in ('owner.obligation_registered','backend.read_started','backend.read_ended'):need(e.get('generation')==1,'session local generation')
        need(one(ev,'backend.completed')['details']==dict(status='error_after_access_ended' if failed else 'completed'),'real backend completion status')
        ordered(ev,['owner.permit_granted','resource.reserved','work.submitted','owner.obligation_registered','owner.draining','drain.started','session.closed','drain.ended','owner.permit_returned'])
        need(ev.index(one(ev,'producer.done'))<ev.index(one(ev,'session.closed')),'session closed before producer task returned')
        need(one(ev,'drain.started')['details']==dict(obligations='1') and one(ev,'drain.ended')['details']==dict(errors=str(int(failed))),'drain actual work/error count')
        need(one(ev,'resource.reserved').get('generation')==1,'reserved generation')
        result=[]
        if failed:
            need(one(ev,'work.failed')['details']==dict(error='backendAfterAccess'),'original typed backend error')
        else:
            values,checksum=oracle(seed);copied[request]=values
            need(one(ev,'cleanup.ended')['details']==dict(checksum=str(checksum),mismatches='0',visits='256'),'scan actual checksum/visits')
            result=[dict(request=request,checksum=checksum,mismatches=0,visits=256)]
        expected=dict(epoch=epoch,session_id='session-'+str(epoch),final_session=dict(active=0,completed=1,generation=1,phase='closed',released=1,results=result,submitted=1))
        need(snapshot['sessions'][epoch-1]==expected,'historical session result/counts')
        replayed.append(dict(scope=scope,epoch=epoch,request=request,failed=failed,physical_address_identity_proven=False))
    need(owner['copied_reads']==copied,'every copied actual element; failed producer cannot acquire a valid read result')
    return replayed

def fault(scope,kind,inv,via):
    origin={'body':'body','backendAfterAccess':'producer/backend_after_access','resourceBusy':'admission'}[kind]
    obligation=scope+{'body':'/body','backendAfterAccess':'/producer','resourceBusy':'/admission'}[kind]
    return dict(invocation_id=inv[scope],scope_id=scope,obligation_id=obligation,origin=origin,kind=kind,reported_via=via)
def same_faults(actual,expected):
    need(len(actual)==len(expected) and sorted(map(canon,actual))==sorted(map(canon,expected)),'exact fault identities, origins and propagation channels')

def audit_case(data):
    name=data['case_name'];spec=specification(name)
    need(data['schema_version']==1 and data['experiment']=='swift-nested-obligations','case schema')
    need(data['incomplete'] is False and data.get('termination_reason') is None and data['diagnostics']==[],'unknown/incomplete harness error')
    need(data.get('validation_passed') is None,'raw semantic observation must stay unclaimed')
    need(data['execution_succeeded'] is (name not in FAILED),'original business/expected-failure status retained')
    owners={o['owner_id']:o for o in data['owners']};need(set(owners)==spec['owners'] and len(owners)==len(data['owners']),'private owner inventory')
    replay={o:owner_audit(owners[o],[w for w in spec['work'] if w[1]==o],int(o=='X' and bool(spec['busy']))) for o in owners}
    events=data['composition_events'];clock(events,True)
    allowed={'scope.claim_started','scope.admitted','scope.claim_rejected','scope.body_error','scope.body_returned','scope.admission_closed',
        'scope.children_drained','scope.owner_returned','local.submitted','local.await_error','child.registered','child.start_gate_opening',
        'child.started','child.run_returned','child.join_started','child.join_returned','child.registration_rejected','client.handles_dropped',
        'checkpoint','parent.cancel_requested','parent.cancel_called','harness.child_return_gate_opening','harness.parent_await_returned',
        'harness.late_rejection_observed','direct.child_await_returned','handle.await_started','handle.await_returned',
        'harness.old_owner_return_observed','harness.old_run_returned_while_new_pending','harness.completion_transferred'}
    need(all(e['name'] in allowed for e in events),'unknown composition event/error')
    claims=[e for e in events if e['name']=='scope.claim_started'];inv={e['scope_id']:e['invocation_id'] for e in claims}
    expected_scopes={w[0] for w in spec['work']}|set(spec['busy'])
    need(set(inv)==expected_scopes and len(inv)==len(claims)==len(set(inv.values())),'unique actual invocation identity')
    need(all(re.fullmatch('[0-9A-F-]{36}',v) for v in inv.values()),'invocation UUID shape')
    per_scope={s:[e for e in events if e['scope_id']==s and e.get('invocation_id')==i] for s,i in inv.items()}
    terminal={}
    work_by_scope={w[0]:w for w in spec['work']};completions={}
    for scope,local in per_scope.items():
        if scope in spec['busy']:
            need([e['name'] for e in local]==['scope.claim_started','scope.claim_rejected'],'rejection before body/submit')
            end=local[-1];need(end['details']==dict(safe='false',fully_drained='true',owner_phase='leased',faults='1',classification='rejectedBeforeAdmission',captured_epoch='none'),'safe rejection differs from admitted release')
        else:
            ordered(local,['scope.claim_started','scope.admitted','local.submitted','scope.body_returned','scope.admission_closed','scope.children_drained','scope.owner_returned'])
            end=one(local,'scope.owner_returned');scope_work=work_by_scope[scope]
            need(all(e.get('owner_id')==scope_work[1] for e in local if e['name'].startswith('scope.')),'scope owner route')
            work=one(local,'local.submitted')['details'];expected=dict(request=scope+'.A',elements='128',iterations='31',passes='2',seed=str(scope_work[2]),producer_invocation=inv[scope])
            need({k:work[k] for k in expected}==expected and set(work)==set(expected)|{'completion_id'},'actual fixed local work/producer identity')
            need(work['completion_id'] not in completions,'unique completion identity');completions[work['completion_id']]=scope
            epoch=next(r['epoch'] for r in replay[scope_work[1]] if r['scope']==scope)
            need(end['details']['safe']=='true' and end['details']['fully_drained']=='true' and end['details']['classification']=='admittedSafelyReturned' and end['details']['captured_epoch']==str(epoch),'terminal safe from actual owner epoch')
        terminal[scope]=end
    # Registry lifecycle and actual child completion identity are cross-checked
    # through the global composition order; local owner clocks are not mixed.
    child_records=[]
    registrations=[e for e in events if e['name']=='child.registered']
    need([(e['child_id'],e['details']['child_scope'],e['owner_id']) for e in registrations]==spec['children'],'registered child set/order')
    parent='X'
    for cid,scope,owner_id in spec['children']:
        child=[e for e in events if e.get('child_id')==cid]
        reg,opened,started=ordered(child,['child.registered','child.start_gate_opening','child.started'])
        launch='detached' if name in DETACHED else 'unstructured'
        need(reg['details']==dict(child_scope=scope,task_retained='true',owner_retained='true',launch_mode=launch),'actual child launch/retention')
        need(started.get('invocation_id')==(None if launch=='detached' else inv[parent]),'detached versus inherited context')
        returned=one(child,'child.run_returned');joined=one(child,'child.join_returned');joining=one(child,'child.join_started')
        closed=one(per_scope[parent],'scope.admission_closed')
        need(started['index']<one(per_scope[scope],'scope.claim_started')['index']<terminal[scope]['index']<returned['index']<joined['index'],'child real run return then parent join')
        need(closed['index']<joining['index']<joined['index']<one(per_scope[parent],'scope.children_drained')['index'],'join only frozen registry, before drain')
        need(reg['index']<closed['index'] and reg['invocation_id']==inv[parent] and joined['invocation_id']==inv[parent],'parent registration/join identity')
        need(returned['details']['child_invocation']==inv[scope] and returned['details']['classification']==terminal[scope]['details']['classification'],'returned child original invocation')
        need(returned['details']['fully_drained']=='true' and returned['details']['safe']==terminal[scope]['details']['safe'] and joined['details']==dict(child_scope=scope,safe='true'),'actual child safe classification')
        child_records.append((cid,scope,owner_id))
    for scope,local in per_scope.items():
        if scope in spec['busy']:continue
        ids=[c[0] for c in child_records] if scope=='X' else []
        need(one(local,'scope.admission_closed')['details']==dict(frozen_children=','.join(ids),count=str(len(ids))),'frozen identity set, not just count')
    expected_faults={s:[] for s in expected_scopes}
    for scope,_,_,failed in spec['work']:
        if failed:expected_faults[scope].append(fault(scope,'backendAfterAccess',inv,['Completion.value','DrainFailure.cleanupErrors[0]'] if name=='local-awaited-producer-fault' else ['DrainFailure.cleanupErrors[0]']))
    for scope in spec['busy']:expected_faults[scope].append(fault(scope,'resourceBusy',inv,['claim']))
    if name=='five-faults':
        for scope in ('X','Y'):expected_faults[scope].insert(0,fault(scope,'body',inv,['body.catch']))
    if name=='propagated-child-fault':expected_faults['Y']=[fault('Y','body',inv,['body.catch'])]
    for _,scope,_ in child_records:expected_faults['X']+=copy.deepcopy(expected_faults[scope])
    if name=='cross-run-local-fault':expected_faults['X']=[fault('Y','backendAfterAccess',inv,['Completion.value'])]
    for scope,end in terminal.items():
        # The frozen source emits this diagnostic before the final identity merge.
        # Completion.value and owner drain are two reports of one local fault.
        occurrences=2 if name=='local-awaited-producer-fault' and scope=='X' else len(expected_faults[scope])
        need(int(end['details']['faults'])==occurrences,'terminal pre-merge fault occurrence count')
    body_errors={'five-faults':{'X':False,'Y':False},'propagated-child-fault':{'X':True,'Y':False},'local-awaited-producer-fault':{'X':True},'cross-run-local-fault':{'X':True}}.get(name,{})
    need({e['scope_id'] for e in events if e['name']=='scope.body_error'}==set(body_errors),'actual body error identities')
    for scope,propagated in body_errors.items():need(one(per_scope[scope],'scope.body_error')['details']['propagated']==str(propagated).lower(),'fault propagation channel classification')
    outcomes=data['outcomes'];need([o['scope_id'] for o in outcomes]==spec['stored'],'complete stored outcomes')
    for outcome in outcomes:
        scope=outcome['scope_id'];rejected=scope in spec['busy'];need(outcome['invocation_id']==inv[scope],'outcome original invocation')
        need(outcome['admitted'] is (not rejected) and outcome['owner_safely_returned'] is (not rejected) and outcome['fully_drained'] is True and outcome['unresolved']==[],'completion/rejection/unknown separation')
        need(outcome['completion_classification']==('rejectedBeforeAdmission' if rejected else 'admittedSafelyReturned'),'outcome classification')
        same_faults(outcome['faults'],expected_faults[scope]);evidence=outcome['owner_return_evidence']
        need(evidence['matching_lease_records']==evidence['matching_return_events']==int(not rejected) and evidence['session_closed'] is (not rejected),'owner actual history/return evidence')
        if rejected:
            need('registry' not in outcome and 'captured_epoch' not in evidence,'no fictional lease for rejected child')
            continue
        owner_id=work_by_scope[scope][1];epoch=next(r['epoch'] for r in replay[owner_id] if r['scope']==scope)
        need(outcome['owner_id']==owner_id and evidence['captured_epoch']==epoch and evidence['session_id']=='session-'+str(epoch) and evidence['session_active']==0,'captured owner epoch/session')
        need(evidence['observed_owner_phase']==terminal[scope]['details']['owner_phase'],'observation phase binding')
        registry=outcome['registry'];ids=[c[0] for c in child_records] if scope=='X' else []
        need(registry['phase']=='drained' and registry['fully_drained'] is True and registry['unresolved_ids']==[] and registry['frozen_ids']==registry['joined_ids']==ids,'final exact child drain set')
        need(len(registry['children'])==len(ids),'final child records complete')
        for row,(cid,child_scope,child_owner) in zip(registry['children'],child_records):
            expected_phase='rejectedBeforeAdmission' if child_scope in spec['busy'] else 'failedSafe' if expected_faults[child_scope] else 'succeeded'
            need(row['child_id']==cid and row['scope_id']==child_scope and row['owner_id']==child_owner and row['invocation_id']==inv[child_scope] and row['joined'] is True and row['unresolved']==[] and row['phase']==expected_phase,'child final identity and safety')
            need(row['completion_classification']==terminal[child_scope]['details']['classification'],'child completion classification')
            same_faults(row['faults'],expected_faults[child_scope])
    await_errors=[e for e in events if e['name']=='local.await_error']
    need(len(await_errors)==int(name in ('local-awaited-producer-fault','cross-run-local-fault')),'observed local error count')
    for e in await_errors:
        d=e['details'];producer=completions.get(d['completion_id']);need(producer is not None,'await same registered completion')
        need(e['scope_id']==producer and d['producer_invocation']==inv[producer] and e['invocation_id']==inv[d['waiting_scope']] and d['obligation_id']==producer+'/producer' and d['origin']=='producer/backend_after_access','original fault identity survives cross-run observation')
    if name=='cross-run-local-fault':
        transfer=one(events,'harness.completion_transferred');aw=await_errors[0]
        need(transfer['details']==dict(completion_id=aw['details']['completion_id'],producer_invocation=inv['Y'],producer_scope='Y'),'same escaped completion identity')
        need(terminal['Y']['index']<transfer['index']<one(per_scope['X'],'scope.claim_started')['index']<aw['index'],'cross-run await after source owner returned')
    checkpoints=data['checkpoints'];need(len({c['label'] for c in checkpoints})==len(checkpoints),'checkpoint label identity')
    for checkpoint in checkpoints:
        label=events[checkpoint['event_index']];need(label['name']=='checkpoint' and label['details']==dict(label=checkpoint['label']),'checkpoint exact atomic recorder index')
        for observed in checkpoint['owners']:
            actual=owners[observed['owner_id']];need(observed['events']==actual['events'][:len(observed['events'])],'checkpoint owner log is actual prefix')
            for req,values in observed['copied_reads'].items():need(actual['copied_reads'].get(req)==values,'checkpoint actual read bytes')
    by_label={c['label']:c for c in checkpoints}
    if name in DETACHED:
        pending=by_label['parent-closing-child-pending'];states={o['owner_id']:o for o in pending['owners']}
        need(states['X']['snapshot']['phase']=='leased' and states['X']['snapshot']['leases_returned']==0,'parent holds permit while child pending')
        for o in ('Y','Z') if name in ('five-faults','cancel-failed-sibling') else ('Y',):
            need(states[o]['snapshot']['leases_returned']==0 and any(e['name']=='backend.physical_write_ended' for e in states[o]['events']) and not any(e['name']=='backend.completed' for e in states[o]['events']),'real child completion pending checkpoint')
        need(one(events,'client.handles_dropped')['details']['count']==str(len(child_records)),'all child handles dropped, registry still joins')
    if name in ('five-faults','cancel-failed-sibling'):
        sample=by_label['Y-ended-Z-still-pending'];states={o['owner_id']:o for o in sample['owners']}
        need(states['Y']['snapshot']['leases_returned']==1 and states['Z']['snapshot']['leases_returned']==0 and states['X']['snapshot']['leases_returned']==0,'failed first child cannot skip still-pending sibling')
    cancellations=[e for e in events if e['name']=='parent.cancel_called']
    need(len(cancellations)==int(name in ('cancel-parent','cancel-failed-sibling')),'actual cancellation case')
    if cancellations:
        need(terminal['X']['cancelled'] is True and all(terminal[s]['cancelled'] is False for s in ('Y','Z') if s in terminal),'parent cancellation, child work retained')
        need(one(events,'parent.cancel_requested')['index']<cancellations[0]['index']<terminal['X']['index'],'cancel before actual parent completion')
    if name=='close-before-register':
        rejected=one(events,'child.registration_rejected');need(rejected['details']==dict(error='closing',child_scope='Late') and rejected['child_id']=='child-Late','known late admission error')
        need(one(per_scope['X'],'scope.admission_closed')['index']<rejected['index'] and owners['Late']['events']==[],'late rejected before any owner work')
    else:need(not [e for e in events if e['name']=='child.registration_rejected'],'unexpected rejected registration')
    if name=='return-observation-reuse':
        old,new=outcomes;need(old['owner_return_evidence']['observed_current_epoch']==2 and old['owner_return_evidence']['observed_owner_phase']=='draining','historical old completion observed after new owner claim')
        marker=one(events,'harness.old_run_returned_while_new_pending');checkpoint=by_label['new-epoch-live-before-old-final-observation']
        need(checkpoint['event_index']<terminal['X.old']['index']<marker['index']<terminal['X.new']['index'],'actual delayed old outcome and new pending order')
        sampled=checkpoint['owners'][0];need(sampled['snapshot']['epoch']==2 and sampled['snapshot']['leases_returned']==1 and len(sampled['snapshot']['sessions'])==1,'old history retained while epoch2 active')
    return dict(case_name=name,passed=True,original_execution_succeeded=data['execution_succeeded'],invocations=len(inv),
        registered_children=len(child_records),actual_admitted_producers=len(spec['work']),valid_full_arrays=sum(not w[3] for w in spec['work']),
        failed_producers=[w[0] for w in spec['work'] if w[3]],faults_preserved=[o['faults'] for o in outcomes],
        storage_scope='Privately owned Swift Array value; no physical-address identity inference.',priority_scope='Samples retained, no priority/latency guarantee.')

def ledger_snapshot(s):
    rows=s['children'];ids=[c['child_id'] for c in rows]
    need(len(set(ids))==len(ids) and len({c['scope_id'] for c in rows})==len(rows),'ledger unique identities')
    final=('succeeded','failedSafe','rejectedBeforeAdmission')
    unresolved=[c['child_id'] for c in rows if c['phase'] not in final or not c['joined']]
    need(s['joined_ids']==[c['child_id'] for c in rows if c['joined']] and s['unresolved_ids']==unresolved,'ledger derived identity lists')
    need(all(not c['unresolved'] or c['phase']=='unresolved' for c in rows),'unknown access retained')
    need(s['fully_drained'] is (s['phase']=='drained' and not unresolved and set(s['frozen_ids'])==set(ids)),'fully drained condition')
    if s['phase']!='open':need(s['frozen_ids']==ids,'closed registry freeze set')

def audit_ledger(data):
    finish={'registered-id-must-join':False,'unknown-child-is-not-safe-owner':False,
            'safe-failure-still-drains':True,'known-pre-admission-rejection-is-safe-failure':True}
    rejects={'duplicate-child-rejected':'duplicate','closing-rejects-before-registration':'closing',
             'join-running-rejected':'unfinished','duplicate-join-rejected':'unfinished',
             'same-count-different-identity-rejected':'wrongPhase','different-child-same-scope-label-rejected':'duplicate'}
    reported={'propagation-does-not-create-body-fault','distinct-invocations-preserve-same-label-faults',
              'producer-propagation-channels-retain-one-identity'}
    checks=data['checks'];need(len(checks)==13 and {c['name'] for c in checks}==set(finish)|set(rejects)|reported,'all 13 ledger records')
    need(data['evidence_kind']=='CPU ledger only; no device/owner unknown-completion injection','CPU-only ledger scope')
    for row in checks:
        need(row['passed'] is True,'original ledger failure remains failure')
        name=row['name']
        if name in reported:
            need('before' not in row and 'after' not in row,'boolean-only check must not invent runtime snapshots');continue
        before=row['before'];after=row['after'];ledger_snapshot(before);ledger_snapshot(after)
        if name in rejects:need(before==after and row['observed_error']==rejects[name],'rejected transition preserves identities')
        else:
            expected=copy.deepcopy(before);expected['phase']='drained' if finish[name] else 'unresolved';expected['fully_drained']=finish[name]
            need(after==expected,'independent finish transition from actual pre-state')
    unknown=next(r for r in checks if r['name']=='unknown-child-is-not-safe-owner')
    need(unknown['after']['children'][0]['unresolved']==['Y/access-unknown'] and unknown['after']['fully_drained'] is False,'unknown child remains explicitly unresolved')
    return dict(passed=True,records=13,snapshot_transitions_independently_checked=10,boolean_only_self_reports=sorted(reported),
                device_or_actual_owner_unknown_completion_tested=False,
                independent_owner_safely_returned_field='An explicit ledger premise, not observed execution of another owner.')

def artifact_audit(root,outer):
    root=Path(root).resolve();outer=Path(outer).resolve()
    manifest=load(root/'manifest.json');prepared=load(root/'prepared.json');pin=load(root/'execution-pin.json');report=load(root/'report.json')
    need(sha(root/'manifest.json')==MANIFEST==prepared['manifest_sha256']==pin['manifest_sha256'],'frozen audited manifest')
    files=manifest['files'];need(set(files)=={str(p.relative_to(root)) for folder in ('source','dependencies') for p in (root/folder).iterdir() if p.is_file()},'source/dependency inventory')
    known={}
    for relative,row in files.items():
        p=root/relative;need(not p.is_symlink() and p.resolve().is_relative_to(root),'source path')
        known[relative]={k:row[k] for k in ('sha256','bytes')};need(identity(p)==known[relative],'source bytes '+relative)
    need(known['source/driver.py']['sha256']==prepared['driver_sha256']==pin['driver_sha256'],'actual frozen driver')
    command=prepared['execute_command'];origin=Path(command[4]);cfg=manifest['configuration']
    need(pin['driver_path']==str(origin/'source/driver.py') and pin['argv']==command[1:] and command[0]==manifest['runtime']['python'],'frozen invocation command')
    need(pin['python_identity']==manifest['runtime']['python_identity'],'recorded Python identity')
    need(pin['compiler_identity']==dict(sha256='2ed38571e92c0283091838c1649e27650ad9c99950288e883c7b2dc6c4ce89fb',bytes=171036592),'recorded compiler identity')
    need(report['provenance']==pin and report['completed'] is True and report['validation_passed'] is True and report['ledger_passed'] is True and report['errors']==[],'original collection/validation failure')
    base=[cfg['compiler'],'-swift-version','6','-target',cfg['target'],'-sdk',cfg['sdk'],'-O','-parse-as-library']
    client_sources=['dependencies/CPUOwner.swift']+['source/'+n+'.swift' for n in ('Protocol','NestedScope','Cases','LedgerChecks','Main')]
    expected={
        'compiler-version':([cfg['compiler'],'--version'],set(),set()),
        'compile-core':(base+['-module-name','ObligationCore','-emit-module','-emit-module-path',str(origin/'build/ObligationCore.swiftmodule'),'-emit-object',str(origin/'dependencies/ObligationCore.swift'),'-o',str(origin/'build/ObligationCore.o')],{'dependencies/ObligationCore.swift'},{'build/ObligationCore.o','build/ObligationCore.swiftmodule'}),
        'compile-client':(base+['-I',str(origin/'build'),*[str(origin/p) for p in client_sources],str(origin/'build/ObligationCore.o'),'-o',str(origin/'build/nested-obligations')],set(client_sources)|{'build/ObligationCore.o','build/ObligationCore.swiftmodule'},{'build/nested-obligations'}),
        'ledger':([str(origin/'build/nested-obligations'),'--ledger'],{'build/nested-obligations'},set())}
    expected.update({'cases/'+name:([str(origin/'build/nested-obligations'),name],{'build/nested-obligations'},set()) for name in CASES})
    need([s['name'] for s in report['stages']]==list(expected),'complete exact stage order')
    for stage in report['stages']:
        name=stage['name'];path=root/name;cmd,inputs,outputs=expected[name]
        need(load(path/'run.json')=={k:v for k,v in stage.items() if k!='name'} and load(path/'started.json')==dict(command=stage['command'],inputs_before=stage['inputs_before']),'raw/start/summary stage binding')
        need(stage['command']==cmd and set(stage['inputs_before'])==inputs and set(stage['artifacts'])==outputs,'actual stage command and input/output inventory')
        need(stage['returncode']==0 and stage['timed_out'] is False and stage['inputs_unchanged_after'] is True and not stage.get('error'),'failed/incomplete actual process')
        need(math.isfinite(stage['elapsed_seconds']) and stage['elapsed_seconds']>0,'recorded actual elapsed')
        for p,row in stage['inputs_before'].items():need(known.get(p)==row==identity(root/p),'prior-produced actual input '+p)
        for p,row in stage['artifacts'].items():need(p not in known and identity(root/p)==row,'new artifact bytes '+p);known[p]=row
        need(set(stage['logs'])=={'stdout','stderr'},'complete raw log streams')
        for p,row in stage['logs'].items():need(identity(path/p)==row,'raw stage log bytes')
        # swiftc --version also records its driver banner on stderr in this run.
        expected_stderr=b'swift-driver version: 1.148.6 ' if name=='compiler-version' else b''
        need((path/'stderr').read_bytes()==expected_stderr,'unknown compiler/runtime stderr')
    need([r['name'] for r in report['cases']]==list(CASES),'all case summary records')
    for r in report['cases']:
        need(r['validation_passed'] is True and r['validation']['passed'] is True and r['original_exit_code']==0 and r['execution_succeeded'] is (r['name'] not in FAILED),'preserve original failed business outcomes')
    run=load(outer/'run.json');need(run['command']==command and run['returncode']==0 and run['timed_out'] is False,'outer frozen command/result')
    need(set(run['files'])=={'stdout.json','stderr.log'},'outer raw streams')
    for p,row in run['files'].items():need(identity(outer/p)==row,'outer log binding')
    need(load(outer/'stdout.json')==report and (outer/'stderr.log').read_bytes()==b'','outer report continuity')
    return dict(passed=True,manifest_sha256=MANIFEST,report_sha256=sha(root/'report.json'),
        binary=known['build/nested-obligations'],source_sha256={p:v['sha256'] for p,v in files.items()},
        external_scope='Recorded compiler/Python identity consistency; current external binaries/SDK/dylib closure not rehashed.')

def analyze(root,outer):
    root=Path(root);artifacts=artifact_audit(root,outer)
    cases=[audit_case(load(root/'cases'/name/'stdout')) for name in CASES]
    ledger=audit_ledger(load(root/'ledger/stdout'))
    return dict(schema_version=1,experiment='swift-nested-obligations-independent-review',passed=True,evidence_integrity_passed=True,
                cases=cases,case_count=len(cases),original_failed_business_cases=[r['case_name'] for r in cases if not r['original_execution_succeeded']],
                full_valid_arrays=sum(r['valid_full_arrays'] for r in cases),artifacts=artifacts,ledger=ledger,
                limits=['CPU private Array values: no GPU, fixed physical address, scheduling performance or arbitrary-child progress guarantee.',
                        'One producer per admitted scope and bounded cases; separate owner-local clocks are not a global timeline.',
                        'Unknown access is tested only by CPU ledger snapshots; no actual owner/device unknown-completion recovery.',
                        'Checkpoints identify an exact composition event but sample different owners separately, not atomically.',
                        'Expected body/backend/admission faults remain failed business outcomes even when safe drain succeeds.'])

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--attempt',type=Path,required=True);p.add_argument('--outer',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    try:result=analyze(a.attempt,a.outer)
    except Exception as e:result=dict(passed=False,evidence_integrity_passed=False,error=type(e).__name__+': '+str(e))
    result['checker_sha256']=sha(__file__)
    with a.output.open('x') as f:json.dump(result,f,indent=2,allow_nan=False);f.write('\n')
    print(json.dumps(dict(passed=result['passed'],output=str(a.output))))
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
