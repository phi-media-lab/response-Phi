#!/usr/bin/env python3
"""Independent C4b artifact, diagnostic, numerical and event audit (stdlib only).

Does not import the driver, run Swift, or promote original required-control failure
to success. CLI success means evidence was independently checked, not that the
experiment's original required_controls_passed became true.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

class InvalidEvidence(ValueError):pass
def need(value,message):
    if not value:raise InvalidEvidence(message)
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def ident(path):return {'sha256':sha(path),'bytes':Path(path).stat().st_size}
def read(path):return json.loads(Path(path).read_text())
def canon(x):return json.dumps(x,sort_keys=True,separators=(',',':'))

RUN_B1={'dynamic_two_readers':['C1','C2'],'dynamic_escaped_lease':['C1'],'dynamic_close_with_active_scan':['C1']}
RUN_B2={'value_local_read':['A'],'value_two_copies':['A','A'],'value_summary_then_overwrite':['A','B'],
        'value_forward_borrow':['A'],'value_across_await':['A']}
OBSERVE={'value_overwrite_before_final_read','value_detached_capture','value_nonconcurrent_escaping_capture',
         'value_across_await','value_callback_generic_result','value_callback_local_dependence'}
DIAGNOSTICS={
 'dynamic_cannot_extract_lease':["'raw' is inaccessible due to 'fileprivate' protection level"],
 'dynamic_cannot_extract_owner':["'owner' is inaccessible due to 'private' protection level"],
 'dynamic_cannot_construct_lease':["'DynamicLease' initializer is inaccessible due to 'fileprivate' protection level","missing argument for parameter #1 in call"],
 'value_local_owner_escape':['lifetime-dependent value escapes its scope'],
 'value_overwrite_before_final_read':["overlapping accesses to 'owner', but modification requires exclusive access [#ExclusivityViolation]"],
 'value_store_in_array':["cannot convert value of type 'Span<UInt32>' to expected element type 'Any'"],
 'value_detached_capture':["lifetime-dependent variable 'view' escapes its scope"],
 'value_nonconcurrent_escaping_capture':["lifetime-dependent variable 'view' escapes its scope"],
 'value_callback_generic_result':["global function 'withLocal' requires that 'Span<UInt32>' conform to 'Escapable'"],
 'value_callback_local_dependence':['lifetime-dependent value escapes its scope']*2}
SOURCE_PINS={
 'source/DynamicFacade.swift':'b9676da39bc37d521e3505f8dd4b390a5c25f49904e6ff535ee7904e88f1e669',
 'source/LifetimeValues.swift':'99bb195316459a6b131f097e2278c641e0d634aa7b1593445ab05ea8db88874a',
 'dependencies/ObligationCore.swift':'3769e443cbcefb6437ceb41dd21c4bea7c4e0cca9bd8fd31de09a3fdcacf4c2a',
 'dependencies/Support.swift':'721c2300015abf074fce04bffb6d959ce53575f5af9e2b37ad4125d94ccd84d7',
 'dependencies/LiveOwner.swift':'afabcf1616e3c4d4694b4cf69bf3b77a4e40ffba0b7bd765a049a1047f4f63ad',
 'source/fixtures.json':'904f02f52b5c346f82da719696c45fd77530d146a57426d917cc0eb639030183'}
NAMES=set(RUN_B1)|set(RUN_B2)|set(DIAGNOSTICS)

def oracle(request,passes):
    seed={'A':0x13579bdf,'B':0x2468ace0}[request]
    values=[]
    for index in range(128):
        value=index^seed
        for _ in range(31):value=(value*1664525+1013904223)&0xffffffff
        values.append(value)
    checksum=0x9e3779b97f4a7c15;out=[]
    for p in range(passes):
        for i,value in enumerate(values):
            checksum=(((checksum<<7)|(checksum>>57))&0xffffffffffffffff)^value
            checksum=(checksum+i+p)&0xffffffffffffffff
        out.append({'pass_index':p,'actual_values':values[:],'checksum':checksum,'visits':128*(p+1),'mismatches':0})
    return out

def event_clock(events):
    need(events and all(type(e['t_ns']) is int and e['t_ns']>=0 for e in events),'nonempty integer event clock')
    need(all(a['t_ns']<=b['t_ns'] for a,b in zip(events,events[1:])),'event clock order')
    need(all(e.get('cancelled') is False and e.get('priority_source')=='sampled' and type(e.get('priority_raw')) is int for e in events),'actual event context')

def validate_b2(name,data):
    need(data['fixture']==name and data['facts']=={},'B2 identity/facts')
    requests=RUN_B2[name];need(len(data['scans'])==len(requests),'B2 scan count')
    expected_events=[];checks=[]
    if name=='value_across_await':expected_events+=['value.before_await','value.after_await']
    for index,(request,scan) in enumerate(zip(requests,data['scans'])):
        expected=oracle(request,2)
        need(scan=={'actual_passes':[p['actual_values'] for p in expected],'checksum':expected[-1]['checksum'],'visits':256},'B2 complete arrays/checksum/visits')
        if name=='value_summary_then_overwrite' and index==1:expected_events+=['value.overwrite_started','value.overwrite_ended']
        expected_events+=['span.scan_started','span.pass_completed','span.pass_completed','span.scan_ended']
        checks.extend(expected)
    events=data['events'];event_clock(events)
    need([e['name'] for e in events]==expected_events,'B2 complete ordered event chain')
    for e in events:
        need(e['request_id']==('value' if e['name'].startswith('span.') else 'harness'),'B2 event request identity')
        if e['name'] not in ('span.pass_completed','span.scan_started'):need(e['details']=={},'B2 unexpected auxiliary event details')
    pass_rows=[e for e in events if e['name']=='span.pass_completed']
    for e,p in zip(pass_rows,checks):
        need(e['request_id']=='value' and e['details']=={'pass':str(p['pass_index']+1),'visits':str(p['visits']),'checksum':str(p['checksum'])},'B2 pass raw counters')
    need(all(e['details']=={'elements':'128'} for e in events if e['name']=='span.scan_started'),'B2 actual length')
    return {'track':'B2','full_numeric_scans':len(requests),'read_values':len(requests)*256,
            'actual_suspend_proven':False,'physical_in_place_proven':False}

def validate_b1(name,data):
    need(data['fixture']==name,'B1 fixture identity');owner=data['owner'];events=data['events'];event_clock(events)
    readers=RUN_B1[name];count=len(readers);owner_id=owner['owner_id'];resource=owner['resource_id']
    need(owner_id.startswith('live-owner-') and resource.startswith('CPU-allocation-'),'B1 resource identity')
    need(owner['faults']==[] and owner['retained_tasks']==0,'B1 unexpected errors/retained tasks')
    need(owner['registered_reader_tasks']==owner['registered_scan_tasks']==count,'B1 registered counts')
    need(len(owner['producer_results'])==2 and len(owner['results'])==count,'B1 result counts')
    for row in owner['producer_results']+owner['results']:
        producer=row['lease_id']=='producer';epoch=row['epoch'];expected=oracle('A' if epoch==1 else 'B',2 if producer else 3)
        need(epoch in (1,2) and row['generation']==epoch,'B1 numerical generation')
        need(row['owner_id']==owner_id and row['resource_id']==resource,'B1 numeric owner/resource identity')
        need(row['passes']==expected,'B1 every actual element and intermediate counter')
        need(row['checksum']==expected[-1]['checksum'] and row['visits']==expected[-1]['visits'] and row['mismatches']==0,'B1 final scan counters')
        need(not producer or row['access_id']=='producer-'+str(epoch),'producer identity')
        need(producer or epoch==1 and row['lease_id'] in ['1/'+r for r in readers],'reader generation/lease')
    need({r['epoch'] for r in owner['producer_results']}=={1,2},'both producers present')
    need(Counter(map(canon,data['values']))==Counter(map(canon,owner['results'])),'delivery multiset matches registered scan results')
    need(len(data['values'])==count,'B1 delivered values count')
    phase='idle';epoch=0;producer='none';producer_pass=0;producer_done=False
    leases={};scans={};returns=0;rejections=[];busy=0;indices={}
    expected_results={r['access_id']:r for r in owner['results']}
    need(len(expected_results)==count,'duplicate scan identity in results')
    for index,e in enumerate(events):
        n=e['name'];d=e['details'];request=e['request_id'];indices.setdefault(n,[]).append(index)
        if n.startswith('client.'):
            need(name=='dynamic_close_with_active_scan' and n in ('client.wrapper_done','client.wrapper_observed') and request=='client' and d=={},'unknown client event')
            continue
        need(d.get('owner_id')==owner_id and d.get('resource_id')==resource,'event owner/resource identity')
        if n=='owner.permit_granted':
            need(phase=='idle' and int(d['epoch'])==epoch+1 and int(d['generation'])==epoch+1,'new grant before previous drain or wrong epoch')
            epoch+=1;phase='leased';producer='granted';producer_pass=0;producer_done=False
            need(request==('A' if epoch==1 else 'B') and epoch<=2,'producer order')
        elif n=='scan.rejected':
            supplied=int(d['epoch']);generation=int(d['generation'])
            reason='expired' if supplied!=epoch or generation!=epoch else 'closing' if phase!='leased' else 'closedLease'
            need(d['reason']==reason and d['owner_phase']==phase and int(d['current_epoch'])==epoch,'rejection did not match actual state')
            need(d['lease_id']=='1/C1' and request==d['lease_id'] and d['supplied_owner_id']==owner_id,'rejected capability identity')
            rejections.append({'operation':'scan','reason':reason,'owner_id':owner_id,'supplied_owner':owner_id,
                               'supplied_epoch':supplied,'supplied_generation':generation,'lease_id':d['lease_id']})
            continue
        else:
            need(int(d['epoch'])==epoch and int(d['generation'])==epoch,'event current epoch/generation')
        if n!='owner.busy_rejected':need(e.get('generation')==epoch,'sampled event generation')
        if n.startswith('producer.') or n in ('owner.permit_granted','owner.draining','owner.permit_returned'):
            need(request==('A' if epoch==1 else 'B'),'producer/owner request identity')
        if n=='owner.permit_granted':pass
        elif n=='producer.registered':need(producer=='granted' and phase=='leased','producer registration');producer='registered'
        elif n=='producer.access_started':need(producer=='registered','duplicate/early write');producer='writing'
        elif n=='producer.write_ended':need(producer=='writing','write ending without write');producer='cleanup-pending'
        elif n=='producer.cleanup_started':need(producer=='cleanup-pending','scan before write end');producer='cleanup'
        elif n=='producer.pass_completed':
            need(producer=='cleanup' and producer_pass<2,'producer pass phase/count')
            expected=oracle('A' if epoch==1 else 'B',2)[producer_pass];producer_pass+=1
            need({k:d[k] for k in ('pass','checksum','visits','mismatches')}=={'pass':str(producer_pass),'checksum':str(expected['checksum']),'visits':str(expected['visits']),'mismatches':'0'},'producer raw pass counters')
        elif n=='producer.ready':need(producer=='cleanup' and producer_pass==2,'producer ready before validation');producer='ready'
        elif n=='producer.done':need(producer=='ready' and not producer_done,'producer completion');producer_done=True
        elif n=='reader.registered':
            lid=d['lease_id'];need(phase=='leased' and producer_done and epoch==1 and lid not in leases and lid=='1/'+lid.split('/')[-1] and lid.split('/')[-1] in readers,'reader admission')
            need(request==lid,'reader request identity');leases[lid]={'phase':'active','body_started':False,'body_done':False,'done':False,'active':set()}
        elif n.startswith('reader.') or n.startswith('lease.'):
            lid=d['lease_id'];need(lid in leases and request==lid,'unknown reader/lease');l=leases[lid]
            if n=='reader.body_started':need(not l['body_started'],'duplicate reader body');l['body_started']=True
            elif n=='reader.body_returned':need(l['body_started'] and not l['body_done'] and d['outcome']=='success','reader body outcome');l['body_done']=True
            elif n=='lease.closing':need(l['body_done'] and l['phase']=='active' and int(d['active_accesses'])==len(l['active']),'close with incorrect access count');l['phase']='closing'
            elif n=='lease.closed':need(l['phase']=='closing' and l['body_done'] and not l['active'],'lease closed with active access');l['phase']='closed'
            elif n=='reader.done':need(l['body_done'] and not l['done'],'reader done state');l['done']=True
            else:raise InvalidEvidence('unknown lease/reader event '+n)
        elif n=='scan.registered':
            aid=d['access_id'];lid=d['lease_id']
            need(phase=='leased' and producer=='ready' and lid in leases and leases[lid]['phase']=='active','scan admitted after closure')
            need(request==aid and aid not in scans and int(d['passes'])==3,'scan registration identity/work')
            scans[aid]={'lease':lid,'phase':'registered','passes':0,'done':False};leases[lid]['active'].add(aid)
        elif n.startswith('scan.'):
            aid=d['access_id'];need(aid in scans and request==aid and d['lease_id']==scans[aid]['lease'],'scan identity binding');s=scans[aid]
            if n=='scan.access_started':need(s['phase']=='registered','scan started state');s['phase']='reading'
            elif n=='scan.pass_completed':
                need(s['phase']=='reading' and s['passes']<3,'scan pass phase/count');expected=oracle('A',3)[s['passes']];s['passes']+=1
                need({k:d[k] for k in ('pass','checksum','visits','mismatches')}=={'pass':str(s['passes']),'checksum':str(expected['checksum']),'visits':str(expected['visits']),'mismatches':'0'},'scan raw counters')
            elif n=='scan.access_ended':
                need(s['phase']=='reading' and s['passes']==3 and d['outcome']=='success' and d['visits']=='384','scan premature end')
                s['phase']='completed';leases[s['lease']]['active'].remove(aid)
            elif n=='scan.done':need(s['phase']=='completed' and not s['done'],'scan done state');s['done']=True
            else:raise InvalidEvidence('unknown scan event '+n)
        elif n=='owner.draining':
            need(phase=='leased' and producer_done,'drain before producer done');phase='draining'
            current_leases=set(leases) if epoch==1 else set();current_scans=set(scans) if epoch==1 else set()
            need(set(filter(None,d['reader_ids'].split(',')))==current_leases and set(filter(None,d['scan_ids'].split(',')))==current_scans,'drain registry omits tasks')
        elif n=='owner.permit_returned':
            need(phase=='draining' and producer_done and all(l['phase']=='closed' and l['done'] and not l['active'] for l in leases.values()) and all(s['phase']=='completed' and s['done'] for s in scans.values()),'permit returned before all obligations')
            phase='idle';returns+=1
        elif n=='owner.busy_rejected':need(name=='dynamic_close_with_active_scan' and epoch==1 and phase=='draining' and d['phase']==phase and request=='B','busy rejection phase');busy+=1
        else:raise InvalidEvidence('unknown event '+n)
    need(owner['ledger']=={'phase':'idle','producer':'ready','epoch':2,'generation':2,'unresolved':0,'active_leases':0,'active_accesses':0,'grants':2,'returns':2} and returns==2 and phase=='idle','final replay/ledger')
    need(set(leases)=={'1/'+r for r in readers} and set(scans)==set(expected_results),'complete registered obligations')
    for aid,s in scans.items():need(expected_results[aid]['lease_id']==s['lease'],'result routed to wrong lease')
    expected_leases=[{'lease_id':lid,'name':lid.split('/')[1],'epoch':1,'generation':1,'phase':'closed','body_done':True,'active_access_ids':[]} for lid in sorted(leases)]
    expected_accesses=[{'access_id':aid,'lease_id':scans[aid]['lease'],'epoch':1,'generation':1,'requested_passes':3,'phase':'completed','completed_passes':3} for aid in sorted(scans)]
    need(owner['leases']==expected_leases and owner['accesses']==expected_accesses,'final identity ledger does not match replay')
    reasons={'dynamic_two_readers':[],'dynamic_escaped_lease':['closing','expired'],'dynamic_close_with_active_scan':['closing']}[name]
    need(owner['rejections']==rejections and [r['reason'] for r in rejections]==reasons,'complete rejection evidence')
    facts={'dynamic_two_readers':{},'dynamic_escaped_lease':{'after_scope':'closing','after_B':'expired'},'dynamic_close_with_active_scan':{'after_close':'closing','busy':'busy','body_value':'31'}}[name]
    need(data['facts']==facts and busy==int(name=='dynamic_close_with_active_scan'),'client result facts')
    if name=='dynamic_close_with_active_scan':
        positions=lambda n:indices.get(n,[])
        need(len(positions('client.wrapper_done'))==len(positions('client.wrapper_observed'))==1,'wrapper observer completion')
        a_release=positions('owner.permit_returned')[0]
        need(positions('client.wrapper_done')[0]<positions('client.wrapper_observed')[0] and a_release<positions('client.wrapper_observed')[0]<positions('owner.permit_granted')[1],'wrapper observation/b-reuse order')
        drain=positions('owner.draining')[0];passes=positions('scan.pass_completed')
        need(passes[0]<positions('reader.body_returned')[0]<drain<passes[1]<passes[2]<a_release,'admitted scan did not demonstrably continue after body/scope close')
    return {'track':'B1','full_numeric_scans':2+count,'read_values':512+384*count,'lease_count':count,
            'same_private_cpu_allocation':True,'general_child_scope_proven':False}

def diagnostic_messages(stderr):
    return re.findall(r'^.+\.swift:\d+:\d+: error: (.+)$',stderr,re.MULTILINE)

def audit_attempt(directory,outer):
    root=Path(directory).resolve();outer=Path(outer).resolve()
    report=read(root/'report.json');manifest=read(root/'manifest.json');prepared=read(root/'prepared.json');pin=read(root/'execution-pin.json')
    need(report['experiment']==manifest['experiment']=='swift-lifetime-fixtures','experiment identity')
    need(sha(root/'manifest.json')==prepared['manifest_sha256']==pin['manifest_sha256'],'manifest binding')
    need(sha(root/'source/driver.py')==prepared['driver_sha256']==pin['driver_sha256'],'driver binding')
    need(report['provenance']==pin and pin['verified_before'] is True and report['sources_verified_after'] is True,'before/after recorded provenance')
    need(pin['python_identity']==manifest['runtime']['python_identity'] and pin['sdk_interface']==manifest['runtime']['sdk_interface'],'runtime metadata continuity')
    need(pin['compiler_identity']['bytes']>0 and re.fullmatch('[0-9a-f]{64}',pin['compiler_identity']['sha256']),'compiler recorded identity')
    origin=Path(prepared['execute_command'][4]);need(pin['actual_driver']==str(origin/'source/driver.py'),'actual frozen driver path')
    need(pin['prepared_command']==prepared['execute_command'] and pin['actual_argv'][1:]==prepared['execute_command'][2:],'actual driver arguments')
    files=manifest['files'];need(set(files)=={str(p.relative_to(root)) for folder in ('source','dependencies','clients') for p in (root/folder).iterdir() if p.is_file()},'complete copied source inventory')
    for relative,row in files.items():
        p=root/relative;need(not p.is_symlink() and p.resolve().is_relative_to(root) and ident(p)=={k:row[k] for k in ('sha256','bytes')},'copied source identity '+relative)
    for relative,digest in SOURCE_PINS.items():need(files[relative]['sha256']==digest,'audited implementation source changed '+relative)
    catalog=read(root/'source/fixtures.json')['fixtures'];need({f['name'] for f in catalog}==NAMES and len(catalog)==18,'fixture contract set')
    catalog={f['name']:f for f in catalog};fixtures=report['fixtures'];need({f['name'] for f in fixtures}==NAMES and len(fixtures)==18,'complete unique collected fixtures')
    config=manifest['configuration'];need(config['b2_feature']=='Lifetimes' and config['b2_annotation']=='@_lifetime' and config['target']=='arm64-apple-macosx26.0','compiler feature configuration')
    known={r:{k:v[k] for k in ('sha256','bytes')} for r,v in files.items()};stage_map={};results=[]
    for stage in report['stages']:
        name=stage['name'];need(name not in stage_map,'duplicate stage');folder=root/name
        need(read(folder/'run.json')==stage,'stage report differs from raw record '+name)
        started=read(folder/'started.json');need(started=={'command':stage['command'],'inputs_before':stage['inputs_before']},'start/finish identity '+name)
        need(not stage['timed_out'] and type(stage['returncode']) is int and 'error' not in stage and stage['inputs_unchanged_after'] is True,'unknown stage failure '+name)
        for p,row in stage['inputs_before'].items():need(p in known and row==known[p] and ident(root/p)==row,'unbound stage input '+p)
        for p,row in stage['files'].items():need(p in ('stdout','stderr') and ident(folder/p)==row,'stage log binding '+name)
        need(set(stage['files'])=={'stdout','stderr'},'complete stage streams')
        for p,row in stage['artifacts'].items():need(p not in known and ident(root/p)==row,'duplicate/changed produced artifact '+p);known[p]=row
        stage_map[name]=stage
    need(len(stage_map)==30,'expected compiler/libraries/18compiles/8runs')
    for name in ('compiler-version','library-core','library-DynamicLeases','library-LifetimeValues'):
        need(stage_map[name]['returncode']==0,'required tool/library compilation')
    base=[config['compiler'],'-swift-version','6','-target',config['target'],'-sdk',config['sdk'],'-O','-parse-as-library']
    expected_commands={
      'compiler-version':[config['compiler'],'--version'],
      'library-core':base+['-module-name','ObligationCore','-emit-module','-emit-module-path',str(origin/'build/ObligationCore.swiftmodule'),'-emit-object',str(origin/'dependencies/ObligationCore.swift'),'-o',str(origin/'build/ObligationCore.o')]}
    expected_inputs={'compiler-version':set(),'library-core':{'dependencies/ObligationCore.swift'}}
    expected_outputs={'compiler-version':set(),'library-core':{'build/ObligationCore.swiftmodule','build/ObligationCore.o'}}
    for module,sources in [('DynamicLeases',['dependencies/Support.swift','dependencies/LiveOwner.swift','source/DynamicFacade.swift']),('LifetimeValues',['source/LifetimeValues.swift'])]:
        flags=base+(['-enable-experimental-feature','Lifetimes'] if module=='LifetimeValues' else [])
        expected_commands['library-'+module]=flags+['-whole-module-optimization','-I',str(origin/'build'),'-module-name',module,'-emit-module','-emit-module-path',str(origin/'build'/(module+'.swiftmodule')),'-emit-object',*[str(origin/s) for s in sources],'-o',str(origin/'build'/(module+'.o'))]
        expected_inputs['library-'+module]=set(sources)|{'build/ObligationCore.swiftmodule'}
        expected_outputs['library-'+module]={'build/'+module+'.swiftmodule','build/'+module+'.o'}
    failed_required=[]
    for fixture in fixtures:
        name=fixture['name'];contract=catalog[name];module='DynamicLeases' if name.startswith('dynamic_') else 'LifetimeValues'
        need(fixture['module']==contract['module']==module and fixture['expectation']==contract['expectation'],'fixture contract binding')
        flags=base+(['-enable-experimental-feature','Lifetimes'] if module=='LifetimeValues' else [])
        compiled=name in RUN_B1 or name in RUN_B2;runtime=contract['runtime'] is not None
        output='client' if runtime else 'client.o';compile_name='fixtures/'+name+'/compile'
        cmd=flags+['-I',str(origin/'build'),str(origin/'clients'/(name+'.swift'))]
        cmd+=([str(origin/'build'/(module+'.o')),str(origin/'build/ObligationCore.o')] if runtime else ['-emit-object'])+['-o',str(origin/'fixtures'/name/output)]
        expected_commands[compile_name]=cmd
        expected_inputs[compile_name]={'clients/'+name+'.swift','build/'+module+'.swiftmodule','build/ObligationCore.swiftmodule'}
        if runtime:expected_inputs[compile_name]|={'build/'+module+'.o','build/ObligationCore.o'}
        expected_outputs[compile_name]={'fixtures/'+name+'/'+output} if compiled else set()
        stage=stage_map[compile_name];need(fixture['compile']==stage and fixture['compile_succeeded'] is compiled and stage['returncode']==(0 if compiled else 1),'actual compiler outcome')
        messages=diagnostic_messages((root/compile_name/'stderr').read_text());need(messages==DIAGNOSTICS.get(name,[]),'exact primary diagnostics changed '+name)
        need(fixture['actual_error_messages']==sorted(set(messages)),'diagnostics lost or rewritten')
        if name=='value_callback_local_dependence':need('closure capture; this is not yet supported' in (root/compile_name/'stderr').read_text(),'closure support boundary note lost')
        if name=='value_callback_generic_result':need("'where R: Escapable' is implicit here" in (root/compile_name/'stderr').read_text(),'generic default boundary note lost')
        observed=name in OBSERVE
        expected_met=None if observed else name!='value_store_in_array'
        need(fixture['collected'] is True and fixture['expectation_met'] is expected_met and fixture['semantic_passed'] is expected_met,'original control/observe classification rewritten')
        row={'name':name,'track':'B1' if module=='DynamicLeases' else 'B2','compiled':compiled,'original_expectation_met':expected_met,'primary_diagnostics':messages}
        if compiled:
            execution_name='fixtures/'+name+'/execution';execution=stage_map[execution_name]
            need(fixture['execution']==execution and execution['returncode']==0,'actual client process failed')
            expected_commands[execution_name]=[str(origin/'fixtures'/name/'client')]
            expected_inputs[execution_name]={'fixtures/'+name+'/client'};expected_outputs[execution_name]=set()
            need((root/execution_name/'stderr').read_bytes()==b'','unexpected runtime stderr')
            data=read(root/execution_name/'stdout');row['independent_runtime']=validate_b1(name,data) if name in RUN_B1 else validate_b2(name,data)
            need(fixture['runtime_check']['passed'] is True,'original runtime self-failure must remain')
        else:need('execution' not in fixture and not (root/'fixtures'/name/'execution').exists(),'rejected client cannot have runtime result')
        if expected_met is False:failed_required.append(name)
        results.append(row)
    need(set(expected_commands)==set(stage_map),'unknown/missing stage')
    for name,cmd in expected_commands.items():
        need(stage_map[name]['command']==cmd,'actual stage command/flags/link inputs '+name)
        need(set(stage_map[name]['inputs_before'])==expected_inputs[name],'stage input inventory '+name)
        need(set(stage_map[name]['artifacts'])==expected_outputs[name],'stage output inventory '+name)
    need(report['execution_completed'] is True and report['all_fixture_processes_collected'] is True and report['errors']==[],'collection failed')
    need(report['required_controls_passed'] is False and failed_required==['value_store_in_array'],'original required failure not preserved')
    need(report['validation_passed'] is None and report['independent_review_required'] is True and report['observed_fixture_count']==6,'unknown observation claims rewritten')
    outer_run=read(outer/'run.json');need(outer_run['returncode']==1 and not outer_run['timed_out'],'original outer exit1 must be retained')
    need(set(outer_run['files'])=={'stdout.json','stderr.log'},'complete outer streams')
    for name,row in outer_run['files'].items():need(ident(outer/name)==row,'outer stream identity')
    need((outer/'stderr.log').read_bytes()==b'','unexpected outer stderr')
    need(read(outer/'stdout.json')==report,'outer stdout and report differ')
    outer_cmd=outer_run['command'];need(outer_cmd[0] in ('python3',manifest['runtime']['python']) and outer_cmd[2:]==prepared['execute_command'][2:],'outer command binding')
    need((Path(outer_run['cwd'])/outer_cmd[1]).resolve()==origin/'source/driver.py','outer actual driver path')
    return {'schema_version':1,'experiment':'swift-lifetime-fixtures-independent-review','evidence_integrity_passed':True,
            'passed':False,'required_controls_passed':False,'original_exit_code':1,'validation_passed':None,
            'fixture_count':18,'runtime_count':8,'compile_rejection_count':10,'required_control_mismatches':failed_required,
            'fixtures':results,'manifest_sha256':sha(root/'manifest.json'),'report_sha256':sha(root/'report.json'),
            'source_sha256':SOURCE_PINS,'external_scope':'Recorded compiler/Python/SDK identities checked for consistency; current external binaries/SDK not rehashed. Not a full SDK/runtime closure.',
            'limits':['B1 proves these private CPU allocation/registered scan paths, not general child scope or GPU lifetime.',
                      'B2 uses experimental Lifetimes with an independent Array value; no same physical storage/in-place proof.',
                      'Task.yield brackets an await expression, not proof that execution actually suspended.',
                      'The original inferred-array Any diagnostic does not establish the predeclared Escapable reason.']}

def audit_array_probe(directory,outer,attempt):
    """Bind the supplementary compile-only client to the original module artifacts."""
    root=Path(directory).resolve();outer=Path(outer).resolve();attempt=Path(attempt).resolve()
    manifest=read(root/'manifest.json');prepared=read(root/'prepared.json');report=read(root/'report.json')
    pin=read(root/'execution-pin.json');stage=read(root/'compile.json')
    original=read(attempt/'manifest.json');original_pin=read(attempt/'execution-pin.json')
    origin=Path(prepared['command'][1]).parent.parent
    original_origin=Path(read(attempt/'prepared.json')['execute_command'][4])
    need(manifest['experiment']==report['experiment']=='swift-lifetime-array-container-probe','probe experiment identity')
    need(manifest['original_attempt']==str(original_origin),'probe original attempt identity')
    need(manifest['original_manifest_sha256']==sha(attempt/'manifest.json') and manifest['original_report_sha256']==sha(attempt/'report.json'),'probe changed original evidence')
    need(sha(root/'manifest.json')==prepared['manifest_sha256']==pin['manifest_sha256'],'probe manifest binding')
    need(sha(root/'source/driver.py')==prepared['driver_sha256']==pin['driver_sha256'],'probe driver binding')
    need(pin['actual_driver']==str(origin/'source/driver.py') and pin['argv'][1:]==prepared['command'][2:],'probe execution arguments')
    need(manifest['configuration']==original['configuration'] and manifest['sdk_interface']==original['runtime']['sdk_interface'] and manifest['python_identity']==original['runtime']['python_identity'],'probe runtime continuity')
    need(pin['compiler_identity']==manifest['compiler_identity']==original_pin['compiler_identity'],'probe compiler continuity')
    files=manifest['files'];modules={'modules/'+m+e for m in ('ObligationCore','LifetimeValues') for e in ('.o','.swiftmodule')}
    need(set(files)==modules|{'source/driver.py','source/prepare.py','source/ExplicitArray.swift'},'probe complete manifest inventory')
    need(set(files)=={str(p.relative_to(root)) for folder in ('source','modules') for p in (root/folder).iterdir() if p.is_file()},'probe actual source/module inventory')
    for relative,row in files.items():
        p=root/relative;need(not p.is_symlink() and p.resolve().is_relative_to(root) and ident(p)=={k:row[k] for k in ('sha256','bytes')},'probe source/module bytes '+relative)
        if relative in modules:
            name=Path(relative).name;module=name.split('.')[0];expected_stage='library-core' if module=='ObligationCore' else 'library-LifetimeValues'
            need(row['original_stage']==expected_stage and row['origin']==str(original_origin/'build'/name),'probe module origin')
            need(row['original_stage_record_sha256']==sha(attempt/expected_stage/'run.json'),'probe module producer record')
            produced=read(attempt/expected_stage/'run.json')['artifacts']['build/'+name]
            need({k:row[k] for k in ('sha256','bytes')}==produced==ident(attempt/'build'/name),'probe module output binding')
    need(files['source/ExplicitArray.swift']['sha256']=='f2aa9c041ff5cd6fb7731ca505ae21bd6a297d487c029b71d975469a54e2fdb4','audited typed-array client changed')
    used={k:v for k,v in files.items() if k.endswith('.swiftmodule')}
    need(pin['used_module_inputs']==used and pin['linked_objects']==[],'probe actual compile inputs; retained objects are not link inputs')
    cfg=manifest['configuration']
    command=[cfg['compiler'],'-swift-version','6','-target',cfg['target'],'-sdk',cfg['sdk'],'-O','-parse-as-library','-enable-experimental-feature','Lifetimes','-I',str(origin/'modules'),str(origin/'source/ExplicitArray.swift'),'-emit-object','-o',str(origin/'explicit-array.o')]
    need(stage['command']==pin['command']==command and report['compile']==stage,'probe exact command/record')
    need(stage['returncode']==1 and stage['timed_out'] is False and stage['artifacts']=={} and not (root/'explicit-array.o').exists(),'probe rejected compilation, no executable result')
    need(set(stage['files'])=={'stdout','stderr'},'probe compile streams')
    for name,row in stage['files'].items():need(ident(root/name)==row,'probe compile stream bytes')
    need((root/'stdout').read_bytes()==b'','unexpected probe compiler stdout')
    messages=diagnostic_messages((root/'stderr').read_text())
    need(messages==["type 'Span<UInt32>' does not conform to protocol 'Escapable'"]==report['actual_error_messages'],'exact supplementary primary diagnostic')
    need(report['provenance']==pin and report['collected'] is True and report['semantic_passed'] is None and report['needs_review'] is True and report['errors']==[],'probe collection/classification')
    need(report['swift_client_executed'] is False and report['gpu_executed'] is False and report['compile_succeeded'] is False and report['declared_diagnostic_match'] is True and report['sources_verified_after'] is True,'probe outcome must remain compile-only rejection')
    run=read(outer/'run.json');need(run['returncode']==0 and run['timed_out'] is False,'probe outer collection result')
    need(set(run['files'])=={'stdout.json','stderr.log'},'probe outer streams')
    for name,row in run['files'].items():need(ident(outer/name)==row,'probe outer stream identity')
    need(read(outer/'stdout.json')==report and (outer/'stderr.log').read_bytes()==b'','probe outer raw report')
    need(run['command'][0]==prepared['command'][0] and run['command'][2:]==prepared['command'][2:],'probe outer command arguments')
    need((Path(run['cwd'])/run['command'][1]).resolve()==origin/'source/driver.py','probe actual frozen driver')
    return {'evidence_integrity_passed':True,'compiled':False,'client_executed':False,'primary_diagnostics':messages,
            'manifest_sha256':sha(root/'manifest.json'),'report_sha256':sha(root/'report.json'),
            'original_required_control_still_failed':True,'inference':'The explicit [Span<UInt32>] container is rejected for Escapable in this toolchain; the original inferred-array Any diagnostic remains separate.'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--attempt',type=Path,required=True);p.add_argument('--outer',type=Path,required=True);p.add_argument('--probe',type=Path);p.add_argument('--probe-outer',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if bool(a.probe)!=bool(a.probe_outer):p.error('--probe and --probe-outer must be supplied together')
    try:
        result=audit_attempt(a.attempt,a.outer)
        if a.probe:result['typed_array_probe']=audit_array_probe(a.probe,a.probe_outer,a.attempt)
    except Exception as e:result={'evidence_integrity_passed':False,'passed':False,'error':str(e)}
    with a.output.open('x') as f:json.dump(result,f,indent=2);f.write('\n')
    print(json.dumps({'output':str(a.output),'evidence_integrity_passed':result['evidence_integrity_passed'],'original_experiment_passed':False}))
    return 0 if result['evidence_integrity_passed'] else 1
if __name__=='__main__':raise SystemExit(main())
