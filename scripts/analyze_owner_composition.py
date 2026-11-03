#!/usr/bin/env python3
"""Independent bounded CPU composition audit. Never imports or runs Swift code."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys

CASES = ['shared-result-'+x for x in ('normal','cancel-waiter','cancel-producer','drop-cancel-caller','body-and-producer-error')] + ['live-readers-'+x for x in ('normal','cancel-reader','cancel-scope','body-and-scan-error')] + ['live-reader-body-drops-scan-caller','live-producer-failure-after-access']
PROTOCOL = ['R5_unknown_producer_zero_readers_cannot_release','write_end_alone_does_not_remove_required_cleanup','all_leases_and_invocations_must_end','duplicate_return_does_not_decrement_again','error_is_not_completion_but_known_ended_error_can_close']
SOURCES = {'Support.swift','SharedResult.swift','LiveOwner.swift','Cases.swift','Main.swift','build.py','README.md'}
DEPS = {'ObligationCore.swift':'3769e443cbcefb6437ceb41dd21c4bea7c4e0cca9bd8fd31de09a3fdcacf4c2a','CPUOwner.swift':'0d0f1b33dce6fd024037c02cdafa1ac73e928041572e54a67801c92d224e2e23'}
SEEDS = {'A':0x13579bdf,'B':0x2468ace0}
MASK = (1<<64)-1
class Invalid(ValueError): pass
def require(ok, why):
    if not ok: raise Invalid(why)
def sha(b): return hashlib.sha256(b).hexdigest()
def same(a,b,why): require(a==b,why)
def integer(x): return type(x) is int

def values(request):
    # Closed affine transform: arbitrary-precision integers, reduced modulo 2**32.
    # This is neither the tested iterative kernel nor its binary-composition oracle.
    a,c,n=1664525,1013904223,31
    multiplier=pow(a,n)
    add=c*(multiplier-1)//(a-1)
    return [((i^SEEDS[request])*multiplier+add)%(1<<32) for i in range(128)]
def scans(request,passes):
    v=values(request); checksum=0x9e3779b97f4a7c15; out=[]
    for p in range(passes):
        for i,x in enumerate(v):
            checksum=((((checksum<<7)&MASK)|(checksum>>57))^x)+i+p
            checksum &= MASK
        out.append(dict(pass_index=p,actual_values=v,checksum=checksum,visits=(p+1)*128,mismatches=0))
    return out
def result(request):
    p=scans(request,2)[-1]
    return dict(request=request,checksum=p['checksum'],visits=256,mismatches=0)
def copied(owner,resource,epoch,lease,access,passes):
    p=scans('A' if epoch==1 else 'B',passes)
    return dict(owner_id=owner,resource_id=resource,epoch=epoch,generation=epoch,lease_id=lease,access_id=access,passes=p,checksum=p[-1]['checksum'],visits=128*passes,mismatches=0)
def fault(origin,identity,kind,cause=None):
    f=dict(origin=origin,obligation_id=identity,kind=kind)
    if cause is not None:f['caused_by']=cause
    return f

def event_common(c):
    require(c['passed'] is True and c['checks'] and all(x['passed'] is True for x in c['checks']),'self-check failure')
    same(c['config'],dict(elements=128,producer_iterations=31,producer_cleanup_passes=2,reader_scan_passes=3 if c['track']=='C2' else 0),'fixed work config')
    same(c['observations']['works'],[dict(request=r,elements=128,iterations=31,passes=2,seed=SEEDS[r]) for r in ('A','B')],'actual fixed work')
    ts=[e['t_ns'] for e in c['events']]
    require(ts and all(integer(t) and t>=0 for t in ts) and all(a<=b for a,b in zip(ts,ts[1:])),'event clock/order')
    for e in c['events']:
        require(isinstance(e['details'],dict) and all(isinstance(k,str) and isinstance(v,str) for k,v in e['details'].items()),'event detail schema')
        if e['name']=='producer.escalated':
            require(e.get('cancelled') is None and e['priority_source']=='escalation_callback','escalation source')
        else: require(type(e.get('cancelled')) is bool and e['priority_source']=='sampled','sampled task metadata')
        require(integer(e['priority_raw']) and 0<=e['priority_raw']<=255,'priority sample domain')

def c1(c):
    o,events=c['observations'],c['events']; mode=c['case_id'].removeprefix('shared-result-')
    error=mode=='body-and-producer-error'; waiters=mode in ('normal','cancel-waiter','cancel-producer')
    allowed={'owner.permit_granted','resource.reserved','work.submitted','owner.obligation_registered','producer.started','backend.started','backend.physical_write_ended','backend.completed','completion.observed','cleanup.started','backend.read_started','backend.read_ended','cleanup.ended','resource.released','producer.done','producer.escalated','owner.draining','drain.started','session.closed','drain.ended','owner.permit_returned','owner.busy_rejected','owner.expired_rejected','work.failed','consumer.wait_call_entered','consumer.wait_returned','consumer.cancel_requested','consumer.cancel_accepted','producer.cancel_requested_by_controller','producer.cancel_accepted_by_controller','consumer.all_result_handles_dropped','scope.cancel_requested','scope.cancel_accepted'}
    require(all(e['name'] in allowed for e in events),'unknown C1 event')
    idx=lambda n,r=None:[i for i,e in enumerate(events) if e['name']==n and (r is None or e['request_id']==r)]
    def one(n,r=None):
        a=idx(n,r);require(len(a)==1,'C1 event multiplicity '+n+str(r));return a[0]
    def chain(items):
        pos=[one(n,r) for n,r in items];require(pos==sorted(pos) and len(set(pos))==len(pos),'C1 causal chain')
    ownerphase='idle'; epoch=0; sessions=[];busy=expired=0;closed=False;drained=False;reserved=False;submitted=False;registered=False
    req=None;drain_start=-1
    for i,e in enumerate(events):
        n,d,r=e['name'],e['details'],e['request_id']
        if n=='owner.permit_granted':
            require(ownerphase=='idle' and epoch<2,'C1 early/duplicate owner grant');epoch+=1;req='A' if epoch==1 else 'B';ownerphase='leased'
            same(d,dict(epoch=str(epoch),session_id=f'session-{epoch}'),'C1 owner identity');closed=drained=reserved=submitted=registered=False;drain_start=-1
        elif n=='resource.reserved':
            require(ownerphase=='leased' and not reserved,'C1 reservation');same(e.get('generation'),1,'C1 reservation generation');same(d,{},'C1 reservation details');reserved=True
        elif n=='work.submitted':
            require(ownerphase=='leased' and reserved and not submitted and r==req,'C1 submit admission');same(e.get('generation'),1,'C1 submitted generation');same(d,{},'C1 submitted details');submitted=True
        elif n=='owner.obligation_registered':
            require(ownerphase=='leased' and submitted and not registered and r==req,'C1 retained obligation');registered=True
            same(d,dict(epoch=str(epoch),session_id=f'session-{epoch}'),'C1 obligation epoch')
        elif n=='owner.draining':
            require(ownerphase=='leased' and registered,'C1 close without obligation');ownerphase='draining'
            same(d,dict(epoch=str(epoch),session_id=f'session-{epoch}'),'C1 draining identity')
        elif n=='drain.started':
            require(ownerphase=='draining' and drain_start==-1,'C1 drain before close/duplicate');same(d,{'obligations':'1'},'C1 frozen registry');drain_start=i
        elif n=='session.closed':
            require(ownerphase=='draining' and 0<=drain_start<i and one('producer.done',req)<i and not closed,'C1 session close before actual producer done');closed=True
        elif n=='drain.ended':
            require(ownerphase=='draining' and closed and not drained,'C1 incomplete drain');drained=True
            same(d,{'errors':'1' if error and req=='A' else '0'},'C1 original drain error')
        elif n=='owner.permit_returned':
            require(ownerphase=='draining' and drained,'C1 premature permit return')
            same(d,dict(epoch=str(epoch),session_id=f'session-{epoch}'),'C1 returned identity')
            sessions.append(dict(epoch=epoch,session_id=f'session-{epoch}',final_session=dict(phase='closed',generation=1,submitted=1,completed=1,released=1,active=0,results=[] if error and req=='A' else [result(req)])))
            ownerphase='idle'
        elif n=='owner.busy_rejected':
            require(ownerphase!='idle' and epoch==1,'C1 busy gate');same(d,dict(epoch='1',phase=ownerphase),'C1 busy state');busy+=1
        elif n=='owner.expired_rejected':
            require(ownerphase=='idle' and r==('stale-idle' if epoch==1 else 'stale-next'),'C1 stale rejection phase');same(d,dict(current_epoch=str(epoch),permit_epoch='1',phase='idle'),'C1 stale identity');expired+=1
        elif n in {'backend.started','backend.physical_write_ended','backend.completed','completion.observed','cleanup.started','backend.read_started','backend.read_ended','cleanup.ended','resource.released','producer.done','producer.started','work.failed','producer.escalated'}:
            require(ownerphase!='idle' and submitted and r==req,'C1 unregistered/other request access')
            require(e.get('generation')==(None if n.startswith('backend.read_') else 1),'C1 generation')
    same(o['owner'],dict(phase='idle',epoch=2,leases_granted=2,leases_returned=2,busy_rejections=busy,expired_rejections=expired,sessions=sessions),'C1 final owner snapshot')
    same((busy,expired),(1,2) if waiters else (0,0) if error else (1,0),'C1 required rejections')
    reads={r:values(r) for r in ('A','B') if not(error and r=='A')};same(o['actual_reads'],reads,'C1 full actual array oracle')
    same(o['b_result'],result('B'),'C1 B numeric')
    for r in ('A','B'):
        seq=['work.submitted','producer.started','backend.started','backend.physical_write_ended','backend.completed']
        failed=error and r=='A'
        seq += ['work.failed'] if failed else ['completion.observed','cleanup.started','backend.read_started','backend.read_ended','cleanup.ended']
        seq += ['resource.released','producer.done'];chain([(n,r) for n in seq])
        for n in (['completion.observed','cleanup.started','backend.read_started','backend.read_ended','cleanup.ended'] if failed else ['work.failed']):require(not idx(n,r),'C1 failed output published/unknown failure')
        same(events[one('backend.started',r)]['details'],{'backend':'owned-cpu'},'C1 backend identity')
        same(events[one('backend.completed',r)]['details'],{'status':'error_after_access_ended' if failed else 'completed'},'C1 backend completion')
        if failed:same(events[one('work.failed',r)]['details'],{'error':'backendAfterAccess'},'C1 failure identity')
        else:same(events[one('cleanup.ended',r)]['details'],{k:str(v) for k,v in result(r).items() if k!='request'},'C1 cleanup numeric')
        for n in seq[2:]:same(events[one(n,r)]['cancelled'],mode=='cancel-producer' and r=='A' and n not in ('backend.started','backend.physical_write_ended'),'C1 producer cancellation target')
    # Owner control events have duplicate harness request IDs; validate per-epoch multiplicities.
    for n in ['owner.permit_granted','resource.reserved','owner.obligation_registered','owner.draining','drain.started','session.closed','drain.ended','owner.permit_returned']:same(len(idx(n)),2,'C1 owner event multiplicity '+n)
    controls=[]
    if busy:require(one('owner.busy_rejected')<one('backend.completed','A'),'C1 retained access at busy gate')
    if waiters:
        same(o['mode'],mode,'C1 mode');same(o['rejections'],['resourceBusy','expiredPermit','expiredPermit'],'C1 rejections')
        for who,key in [('C1','first_waiter'),('C2','second_waiter')]:
            same(o[key],dict(result=result('A'),cancelled=mode=='cancel-waiter' and who=='C1'),'C1 copied waiter result')
            chain([('backend.physical_write_ended','A'),('consumer.wait_call_entered',who),('owner.busy_rejected',None),('producer.done','A'),('consumer.wait_returned',who)])
            require(one('consumer.wait_returned',who)<idx('owner.draining')[0],'C1 waiter return before body closes')
            same(events[one('consumer.wait_returned',who)]['cancelled'],mode=='cancel-waiter' and who=='C1','C1 waiter cancellation')
        same(o['escaped_result'],result('A'),'C1 escaped value')
        if mode=='cancel-waiter':controls=['consumer.cancel_requested','consumer.cancel_accepted'];target='C1'
        if mode=='cancel-producer':controls=['producer.cancel_requested_by_controller','producer.cancel_accepted_by_controller'];target='A'
        if controls:chain([('consumer.wait_call_entered','C1'),(controls[0],target),(controls[1],target),('backend.completed','A')])
    elif error:
        same(o['body_error'],'body','C1 body error');same(o['cleanup_errors'],['backendAfterAccess'],'C1 cleanup error');same(o['original_A_outcome'],'failed','C1 failed A classification')
    else:
        same(o['caller_value'],23,'C1 caller value');same(o['caller_cancelled'],True,'C1 scope cancel flag')
        controls=['scope.cancel_requested','scope.cancel_accepted'];target='scope'
        chain([('work.submitted','A'),('consumer.all_result_handles_dropped','A'),('scope.cancel_requested','scope'),('scope.cancel_accepted','scope'),('backend.completed','A')])
    optional=['consumer.cancel_requested','consumer.cancel_accepted','producer.cancel_requested_by_controller','producer.cancel_accepted_by_controller','scope.cancel_requested','scope.cancel_accepted']
    for n in optional:same(len(idx(n)),int(n in controls),'C1 unexpected/missing cancellation')
    same(len(idx('consumer.all_result_handles_dropped')),int(mode=='drop-cancel-caller'),'C1 drop count')
    for n in ['consumer.wait_call_entered','consumer.wait_returned']:same(len(idx(n)),2 if waiters else 0,'C1 waiter count')
    return dict(case_id=c['case_id'],passed=True,track='C1',array_elements_verified=sum(map(len,reads.values())),expected_request_failure=error)


def live(c):
    o=c['observations'];events=c['events'];name=c['case_id'];readers=name.startswith('live-readers-');bad=name.endswith('body-and-scan-error');drop=name=='live-reader-body-drops-scan-caller';pf=name=='live-producer-failure-after-access'
    owner,resource=o['owner']['owner_id'],o['owner']['resource_id']
    require(owner.startswith('live-owner-') and resource.startswith('CPU-allocation-'),'C2 concrete identity')
    def blank(oid,rid):
        return dict(owner_id=oid,resource_id=rid,ledger=dict(phase='idle',producer='none',epoch=0,generation=0,unresolved=0,active_leases=0,active_accesses=0,grants=0,returns=0),leases=[],accesses=[],producer_results=[],results=[],faults=[],rejections=[],registered_reader_tasks=0,registered_scan_tasks=0,retained_tasks=0)
    s=blank(owner,resource);foreign=blank(o['foreign_owner']['owner_id'],o['foreign_owner']['resource_id']) if readers else None
    if foreign:require(owner!=foreign['owner_id'] and resource!=foreign['resource_id'],'C2 foreign allocation identity')
    leases={};accesses={};reader_started=set();reader_done=set();scan_done=set();producer_done=set();producer_pass=0;producer_stage='none';bodyfault=None;busy=0;history=[];captures={};closing=set();cancels=[];wrapped=False;observed=False
    def snap():
        out=copy.deepcopy(s);out['leases']=sorted(copy.deepcopy(list(leases.values())),key=lambda x:x['lease_id']);out['accesses']=sorted(copy.deepcopy(list(accesses.values())),key=lambda x:x['access_id']);return out
    for i,e in enumerate(events):
        n,d,r=e['name'],e['details'],e['request_id'];g=s['ledger'];ep=g['epoch']
        if readers and n=='scan.rejected' and d['reason']=='foreignOwner':captures['both_reading']=snap()
        if readers and n=='scan.rejected' and d['reason']=='closedLease':captures['first_reader_closed']=snap()
        if n=='scan.rejected' and d['reason']=='closing' and 'owner_draining' not in captures:captures['owner_draining']=snap()
        if n=='owner.permit_granted' and r=='B':captures['first_epoch_returned']=snap()
        if 'owner_id' in d:
            foreign_event=foreign is not None and d['owner_id']==foreign['owner_id']
            require(d['owner_id']==owner or foreign_event,'C2 foreign owner mutation')
            same(d['resource_id'],foreign['resource_id'] if foreign_event else resource,'C2 same allocation')
        else:require(n in {'reader.cancel_requested','reader.cancel_accepted','scope.cancel_requested','scope.cancel_accepted','detached_call_wrapper.done','observer.wrapper_observed_done'},'C2 missing identity/unknown event')
        if n in ('scan.rejected','reader.rejected'):
            target=foreign if d['owner_id']!=owner else s;tg=target['ledger'];sup_ep=int(d['epoch']);sup_gen=int(d['generation']);lid=d['lease_id'];sup=d['supplied_owner_id']
            reason='foreignOwner' if sup!=target['owner_id'] else 'expired' if (sup_ep,sup_gen)!=(tg['epoch'],tg['generation']) else 'closing' if tg['phase']!='leased' else 'closedLease' if lid not in leases or leases[lid]['phase']!='active' else 'invalid' if tg['producer']!='ready' else None
            require(reason is not None and d['reason']==reason,'C2 invalid rejection reason')
            same((d['current_epoch'],d['owner_phase']),(str(tg['epoch']),tg['phase']),'C2 rejection state');same(r,lid,'C2 rejection request')
            target['rejections'].append(dict(operation=n.split('.')[0],reason=reason,owner_id=target['owner_id'],supplied_owner=sup,supplied_epoch=sup_ep,supplied_generation=sup_gen,lease_id=lid))
            continue
        if n=='owner.permit_granted':
            require(g['phase']=='idle' and g['returns']==g['grants'] and ep<2,'C2 reuse before safe return');ep+=1;g.update(phase='leased',producer='registered',epoch=ep,generation=ep,grants=ep);producer_stage='claimed';producer_pass=0
        if 'owner_id' in d:
            same((d['epoch'],d['generation']),(str(ep),str(ep)),'C2 live epoch/generation')
            same(e.get('generation'),None if n=='owner.busy_rejected' else ep,'C2 event generation')
        if n.startswith('producer.') or n in ('owner.draining','owner.permit_returned','scope.body_failed'):same(r,'A' if ep==1 else 'B','C2 active request identity')
        if n.startswith('reader.') or n.startswith('lease.'):
            if 'lease_id' in d:same(r,d['lease_id'],'C2 lease request identity')
        if n=='owner.permit_granted':same(r,'A' if ep==1 else 'B','C2 request sequence')
        elif n=='producer.registered':require(producer_stage=='claimed','C2 producer register');producer_stage='registered'
        elif n=='producer.access_started':require(g['phase']=='leased' and producer_stage=='registered','C2 write admission');producer_stage='writing';g.update(producer='writing',unresolved=1)
        elif n=='producer.write_ended':require(producer_stage=='writing' and g['unresolved']==1,'C2 write completion');producer_stage='writeEnded';g.update(producer='cleanup',unresolved=0)
        elif n=='producer.cleanup_started':require(producer_stage=='writeEnded','C2 cleanup before write end');producer_stage='cleanup'
        elif n=='producer.pass_completed':
            require(producer_stage=='cleanup' and producer_pass<2,'C2 producer scan phase');p=scans(r,2)[producer_pass]
            same({k:d[k] for k in ('pass','checksum','visits','mismatches')},{'pass':str(producer_pass+1),'checksum':str(p['checksum']),'visits':str(p['visits']),'mismatches':'0'},'C2 producer pass numeric');producer_pass+=1
        elif n=='producer.ready':
            require(producer_stage=='cleanup' and producer_pass==2,'C2 ready before cleanup');producer_stage='ready';g['producer']='ready';s['producer_results'].append(copied(owner,resource,ep,'producer',f'producer-{ep}',2))
        elif n=='producer.failed_safe':
            require(pf and ep==1 and producer_stage=='writeEnded' and g['unresolved']==0,'C2 failure before ended write/unexpected error');same(d['error'],'injectedAfterAccessEnded','C2 producer error');producer_stage='failedSafe';g['producer']='failedSafe';s['faults'].append(fault('producer','producer-1','injectedAfterAccessEnded'))
        elif n=='producer.done':require(producer_stage in ('ready','failedSafe') and ep not in producer_done,'C2 producer done before completion');producer_done.add(ep)
        elif n=='reader.registered':
            lid=d['lease_id'];require(g['phase']=='leased' and g['producer']=='ready' and ep in producer_done and lid not in leases,'C2 new reader after close/before producer');same(r,lid,'C2 reader identity')
            leases[lid]=dict(lease_id=lid,name=lid.split('/')[1],epoch=ep,generation=ep,phase='active',body_done=False,active_access_ids=[]);g['active_leases']+=1;s['registered_reader_tasks']+=1;s['retained_tasks']+=1
        elif n=='reader.body_started':
            lid=d['lease_id'];require(lid in leases and lid not in reader_started,'C2 reader body register');same(r,lid,'C2 reader request');reader_started.add(lid)
        elif n=='scan.registered':
            lid,aid=d['lease_id'],d['access_id'];require(g['phase']=='leased' and lid in reader_started and leases[lid]['phase']=='active' and aid not in accesses,'C2 unregistered/closed scan');same((r,d['passes']),(aid,'3'),'C2 scan work');same(aid,f'scan-{len(accesses)+1}','C2 unique access identity')
            accesses[aid]=dict(access_id=aid,lease_id=lid,epoch=ep,generation=ep,requested_passes=3,phase='registered',completed_passes=0);leases[lid]['active_access_ids'].append(aid);g['active_accesses']+=1;s['registered_scan_tasks']+=1;s['retained_tasks']+=1
        elif n.startswith('scan.'):
            aid,lid=d['access_id'],d['lease_id'];require(aid in accesses and accesses[aid]['lease_id']==lid and r==aid,'C2 scan identity');a=accesses[aid]
            if n=='scan.access_started':require(a['phase']=='registered','C2 duplicate/unregistered actual read');a['phase']='reading'
            elif n=='scan.pass_completed':
                require(a['phase']=='reading' and a['completed_passes']<3,'C2 read after access end');p=scans('A',3)[a['completed_passes']]
                same({k:d[k] for k in ('pass','checksum','visits','mismatches')},{'pass':str(a['completed_passes']+1),'checksum':str(p['checksum']),'visits':str(p['visits']),'mismatches':'0'},'C2 scan pass numeric');a['completed_passes']+=1
            elif n=='scan.access_ended':
                fail=bad and lid=='1/C1';expected=1 if fail else 3
                require(a['phase']=='reading' and a['completed_passes']==expected,'C2 access ended missing work');same((d['outcome'],d['visits']),('error_after_read_ended' if fail else 'success',str(expected*128)),'C2 actual read failure classification')
                a['phase']='failedSafe' if fail else 'completed';g['active_accesses']-=1;leases[lid]['active_access_ids'].remove(aid);s['results'].append(copied(owner,resource,ep,lid,aid,expected))
                if fail:s['faults'].append(fault('scan',aid,'injectedAfterReadEnded'))
            elif n=='scan.done':require(a['phase'] in ('completed','failedSafe') and aid not in scan_done,'C2 scan task ended before access');scan_done.add(aid)
            else:raise Invalid('unknown C2 scan event '+n)
        elif n=='reader.body_returned':
            lid=d['lease_id'];require(lid in reader_started and not leases[lid]['body_done'] and r==lid,'C2 reader return multiplicity');fail=bad and lid=='1/C1'
            if fail:
                matches=[a for a in accesses.values() if a['lease_id']==lid];require(len(matches)==1 and matches[0]['phase']=='failedSafe' and matches[0]['access_id'] in scan_done,'C2 failure lost cause');aid=matches[0]['access_id']
                kind=f'CompositionFault(origin: "scan", obligation_id: "{aid}", kind: "injectedAfterReadEnded", caused_by: nil)';same((d['outcome'],d['error'],d['caused_by']),('error',kind,aid),'C2 reader failure identity');s['faults'].append(fault('reader_body',lid,kind,aid))
            else:
                same((d['outcome'],d['value']),('success','11' if drop else '384'),'C2 reader result')
                if not drop:require(not leases[lid]['active_access_ids'],'C2 reader returned before awaited scan')
            same(e['cancelled'],name=='live-readers-cancel-reader' and lid=='1/C1','C2 reader cancellation target');leases[lid].update(body_done=True,phase='closing')
        elif n=='lease.closing':
            lid=d['lease_id'];require(lid in leases and leases[lid]['body_done'] and lid not in closing,'C2 lease close boundary');closing.add(lid);same(d['active_accesses'],str(len(leases[lid]['active_access_ids'])),'C2 active access obligation lost')
        elif n=='lease.closed':
            lid=d['lease_id'];require(lid in closing and leases[lid]['phase']=='closing' and not leases[lid]['active_access_ids'],'C2 lease closes with active read');leases[lid]['phase']='closed';g['active_leases']-=1
        elif n=='reader.done':
            lid=d['lease_id'];require(lid in closing and leases[lid]['body_done'] and lid not in reader_done,'C2 reader done');reader_done.add(lid)
        elif n=='scope.body_failed':
            require(bad and bodyfault is None,'C2 unknown body error');same(d['error'],'injectedBody','C2 body error');bodyfault=fault('body','scope-1','injectedBody')
        elif n=='owner.draining':
            require(g['phase']=='leased' and ep in producer_done,'C2 close before producer');g['phase']='draining'
            same(d['reader_ids'],','.join(lid for lid in leases if leases[lid]['epoch']==ep),'C2 frozen reader registry');same(d['scan_ids'],','.join(aid for aid in accesses if accesses[aid]['epoch']==ep),'C2 frozen scan registry')
        elif n=='owner.permit_returned':
            require(g['phase']=='draining' and g['unresolved']==g['active_leases']==g['active_accesses']==0 and ep in producer_done and g['producer'] in ('ready','failedSafe'),'C2 premature resource return')
            require(all(aid in scan_done for aid,a in accesses.items() if a['epoch']==ep) and all(lid in reader_done for lid,l in leases.items() if l['epoch']==ep),'C2 return before registered task join')
            same(e['cancelled'],name=='live-readers-cancel-scope' and ep==1,'C2 scope cancellation');g['phase']='idle';g['returns']+=1;s['retained_tasks']=0
        elif n=='owner.busy_rejected':require(g['phase']!='idle' and r=='rejected-B' and ep==1,'C2 busy before write');same(d['phase'],g['phase'],'C2 busy phase');busy+=1
        elif n in {'reader.cancel_requested','reader.cancel_accepted','scope.cancel_requested','scope.cancel_accepted'}:
            cancels.append(n);expected_target='1/C1' if n.startswith('reader.') else 'scope-1';same(r,expected_target,'C2 cancel target')
            if n.startswith('reader.'):require(leases['1/C1']['phase']=='active' and g['active_accesses']==2,'C2 cancellation phase')
            else:require(g['phase']=='draining' and g['active_accesses']==1,'C2 scope cancellation phase')
        elif n=='detached_call_wrapper.done':require(drop and not wrapped and scan_done=={'scan-1'},'C2 wrapper done before scan');wrapped=True
        elif n=='observer.wrapper_observed_done':require(wrapped and not observed,'C2 wrapper observation');observed=True
        else:raise Invalid('unknown C2 event '+n)
        if n.startswith('producer.') or n.startswith('scan.'):
            same(e['cancelled'],False,'C2 consumer/scope cancellation leaked into producer/scan')
        history.append((i,snap()))
    same(o['owner'],snap(),'C2 final snapshot/history/complete numeric arrays')
    same(g,dict(phase='idle',producer='ready',epoch=2,generation=2,unresolved=0,active_leases=0,active_accesses=0,grants=2,returns=2),'C2 final conservation')
    same(len(leases),2 if readers else 1 if drop else 0,'C2 full reader work');same(len(accesses),len(leases),'C2 full scan work');same(busy,0 if pf else 1,'C2 busy coverage')
    expected_cancel=['reader.cancel_requested','reader.cancel_accepted'] if name.endswith('cancel-reader') else ['scope.cancel_requested','scope.cancel_accepted'] if name.endswith('cancel-scope') else []
    same(cancels,expected_cancel,'C2 cancellation event chain')
    if readers:
        same([x['reason'] for x in s['rejections']],['expired','closedLease','closing','closing','expired'],'C2 actual rejection chain')
        same([x['reason'] for x in foreign['rejections']],['foreignOwner'],'C2 foreign rejection count')
        for label,count in [('both_reading',2),('first_reader_closed',1),('owner_draining',1)]:
            g0=captures[label]['ledger'];same((g0['active_leases'],g0['active_accesses']),(count,count),'C2 gate coverage counts')
            active=[a for a in captures[label]['accesses'] if a['phase']=='reading'];require(all(a['completed_passes']==1 for a in active),'C2 gate must follow exactly first pass')
        same(bodyfault,fault('body','scope-1','injectedBody') if bad else None,'C2 required original body fault')
        same(o['foreign_owner'],foreign,'C2 foreign owner rejection/zero accesses');same(o['phases'],{k:captures[k] for k in ('both_reading','first_reader_closed','owner_draining','first_epoch_returned')},'C2 anchored phase snapshots')
        same(o['rejections'],['foreignOwner','expired','closedLease','busy','closing','closing','expired'],'C2 rejection coverage')
        expected=dict(succeeded=not bad,cancelled=name.endswith('cancel-scope'))
        if bad:expected['failure']=dict(body=bodyfault,obligations=s['faults'],unresolved=[])
        else:expected['value']=17
        same(o['scope_outcome'],expected,'C2 original failure/scope outcome')
        for who,key in [('1/C1','escaped_copied_first'),('1/C2','escaped_copied_second')]:
            r=next(x for x in s['results'] if x['lease_id']==who)
            same(o[key],None if bad and who=='1/C1' else r,'C2 escaped copied numeric')
    elif drop:
        same([x['reason'] for x in s['rejections']],['closing'],'C2 drop rejection count');held=captures['owner_draining'];same((held['ledger']['active_accesses'],held['ledger']['active_leases']),(1,1),'C2 dropped body access held');same(held['accesses'][0]['completed_passes'],1,'C2 dropped gate after first pass');same((held['leases'][0]['phase'],held['leases'][0]['body_done']),('closing',True),'C2 body closes with independent access')
        same(o['retained_phase'],captures['owner_draining'],'C2 closed body retains real scan obligation');same(o['copied_result'],s['results'][0],'C2 detached copied numeric');same(o['rejections'],['closing','busy'],'C2 drop rejection coverage');require(observed,'C2 wrapper not observed')
        p={e['name']:i for i,e in enumerate(events) if e['name'] in ('observer.wrapper_observed_done','detached_call_wrapper.done')};grant=next(i for i,e in enumerate(events) if e['name']=='owner.permit_granted' and e['request_id']=='B');require(p['observer.wrapper_observed_done']<grant,'C2 B before wrapper observation')
    else:
        same(s['rejections'],[],'C2 no reader probes after failed producer')
        same(o['original_A_outcome'],'failed','C2 producer failure result');same(o['failure'],dict(obligations=s['faults'],unresolved=[]),'C2 original producer fault')
    return dict(case_id=name,passed=True,track='C2',array_elements_verified=sum(len(p['actual_values']) for x in s['producer_results']+s['results'] for p in x['passes']),expected_request_failure=bad or pf,snapshot_boundary='Fixed case label bound to preceding gate-controlled event; not an instrumented snapshot timestamp.')


def check_data(data):
    same((data['schema_version'],data['experiment']),(1,'swift-owner-composition'),'schema')
    require(data['passed'] is True and data['source_verified_before'] is True and data['source_verified_after'] is True and 'error' not in data,'failed/incomplete report')
    same(data['config'],dict(selection='all',protocol_only='false'),'full correctness selection')
    same([x['name'] for x in data['protocol_tests']],PROTOCOL,'CPU ledger test set');require(all(x['passed'] is True for x in data['protocol_tests']),'CPU ledger self-test failure')
    same([x['case_id'] for x in data['cases']],CASES,'complete eleven cases')
    out=[]
    for c in data['cases']:
        same(c['track'],'C1' if c['case_id'].startswith('shared-result-') else 'C2','track identity');event_common(c)
        out.append(c1(c) if c['track']=='C1' else live(c))
    return out


def artifact_audit(attempt,record,data):
    attempt,record=Path(attempt).resolve(),Path(record).resolve()
    def read(root,relative):
        p=root/relative;require(p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(root),'missing/escaped artifact '+str(p));return p.read_bytes()
    sm_bytes=read(attempt,'source-manifest.json');sm=json.loads(sm_bytes);bb=read(attempt,'build.json');build=json.loads(bb);p=data['provenance']
    same(sm['schema_version'],1,'source manifest schema');same(set(sm['source_sha256']),SOURCES,'source set');same(sm['dependency_sha256'],DEPS,'frozen dependency identities')
    require(build['passed'] is True and build['attempt']==sm['attempt'] and p['attempt']==str(sm['attempt']),'build result/attempt')
    same(p['build_manifest_sha256'],sha(bb),'build manifest binding');same(p['source_manifest_sha256'],sha(sm_bytes),'source manifest binding');same(build['source_manifest_sha256'],sha(sm_bytes),'build source identity')
    for folder,key,kind in [('source','source_sha256','source'),('dependencies','dependency_sha256','dependency')]:
        same(build[key],sm[key],'compiled file map');same({x.name for x in (attempt/folder).iterdir()},set(sm[key]),'source/dependency file set')
        for name,h in sm[key].items():same(sha(read(attempt,folder+'/'+name)),h,'frozen '+folder+'/'+name);same(p[kind+'_sha256:'+name],h,'runtime source binding')
    binary=read(attempt,'build/swift-owner-composition');same(sha(binary),build['binary_sha256'],'binary hash');same(p['binary_sha256'],sha(binary),'runtime binary binding');same(p['swift'],build['swift'],'compiler version binding')
    historical=PurePosixPath(p['attempt_path']);bd=historical/'build';src=historical/'source';dep=historical/'dependencies'
    commands=[['xcrun','swiftc','-swift-version','6','-O','-parse-as-library','-module-name','ObligationCore','-emit-module','-emit-module-path',str(bd/'ObligationCore.swiftmodule'),'-emit-object',str(dep/'ObligationCore.swift'),'-o',str(bd/'ObligationCore.o')],['xcrun','swiftc','-swift-version','6','-O','-parse-as-library','-I',str(bd),str(dep/'CPUOwner.swift')]+[str(src/x) for x in ('Support.swift','SharedResult.swift','LiveOwner.swift','Cases.swift','Main.swift')]+[str(bd/'ObligationCore.o'),'-o',str(bd/'swift-owner-composition')]]
    same(build['commands'],[dict(command=x,returncode=0) for x in commands],'compile commands source closure')
    for i,command in enumerate(commands):
        same(json.loads(read(attempt,f'command-{i}.json')),dict(command=command,returncode=0),'compile record');same(read(attempt,f'compile-{i}.stderr'),b'','compile diagnostic');same(read(attempt,f'compile-{i}.stdout'),b'','compile stdout')
    run=json.loads(read(record,'run.json'));require(run['returncode']==0 and run['timed_out'] is False,'correctness process failure/timeout');require(run['command'] in ([str(bd/'swift-owner-composition')],[str(bd/'swift-owner-composition'),'--case','all']),'recorded binary command')
    for name in ('stdout.json','stderr.log'):
        value=read(record,name);same(run['files'][name],dict(bytes=len(value),sha256=sha(value)),'recorded output hash')
    same(json.loads(read(record,'stdout.json')),data,'raw input binding');same(read(record,'stderr.log'),b'','unexpected stderr')
    return dict(passed=True,attempt_manifest_sha256=sha(sm_bytes),build_manifest_sha256=sha(bb),binary_sha256=sha(binary),raw_sha256=sha(read(record,'stdout.json')),source_files=len(SOURCES),dependencies=len(DEPS),boundary='Checks retained compile command/source/binary identities, not a compiler correctness proof or archived system runtime closure.')


def analyze(attempt,record):
    data=json.loads((Path(record)/'stdout.json').read_bytes());cases=check_data(data);artifacts=artifact_audit(attempt,record,data)
    return dict(schema_version=1,passed=True,cases=cases,artifacts=artifacts,checker_sha256=sha(Path(__file__).read_bytes()),scope='Independent Python integer oracle and event-history replay. Gates establish bounded order, not timing/priority performance. Five ledger tests are recorded self-test outcomes, not independent device-failure evidence. No Swift execution/import, general liveness, compile-fail capability or Metal claim.')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--attempt',type=Path,required=True);p.add_argument('--record',type=Path,required=True);args=p.parse_args()
    try:out=analyze(args.attempt,args.record)
    except (Invalid,KeyError,ValueError,TypeError,OSError,IndexError,StopIteration) as e:out=dict(passed=False,error=type(e).__name__+': '+str(e))
    print(json.dumps(out,indent=2,allow_nan=False));return 0 if out['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
