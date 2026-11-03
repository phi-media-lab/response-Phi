"""Pure, finite decoder input mutations from a real parent ready exchange.

No exporter/decoder execution or filesystem IO. The future runner must retain
actual frontend stdin/stdout/exit evidence and independently freeze this oracle.
Every case has a fresh deep copy; appended static objects cannot change shared
original leaves. The source is never rewritten to conceal a tampered exchange.
"""
from copy import deepcopy
import base64
import hashlib
import json

CASES=(
 ('unknown-profile','program version'),
 ('wrong-sigma-id','fixed Sigma implementations'),
 ('source-bytes-hash','exact source bytes'),
 ('service-result-unit','parent fieldless Word contract'),
 ('service-tokens-seven','parent complete Pair contract'),
 ('tool-argument-nat','parent fieldless Word contract'),
 ('tool-result-word-length','parent fieldless Nat contract'),
 ('scope-kind','parent plain Scope kind'),
 ('scope-identity','parent current scope nominal correspondence'),
 ('service-effect-literal','parent HostService fixed literal'),
 ('service-effect-scope','parent current scope nominal correspondence'),
 ('tool-capability','parent LocalRead fixed literal'),
 ('missing-raise','parent exact effect arity'),
 ('extra-effect','parent exact effect arity'),
 ('command-direct-copy','source-directed direct usage'),
)


def _read(program,ref):
    value=program['static_values'][int(ref['ref'])]
    if value is None or type(value) in (str,bool):return value
    if 'integer' in value:return int(value['integer'])
    if 'array' in value:return [_read(program,v) for v in value['array']]
    return {k:_read(program,v) for k,v in value['object']}


def _append(program,value):
    if value is None or type(value) in (str,bool):encoded=value
    elif type(value) is int:encoded={'integer':str(value)}
    elif type(value) is list:encoded={'array':[_append(program,v) for v in value]}
    elif type(value) is dict:encoded={'object':[[k,_append(program,v)] for k,v in value.items()]}
    else:raise TypeError('static JSON data required')
    index=len(program['static_values']);program['static_values'].append(encoded)
    return {'ref':str(index)}


def _type(tag,**fields):return {'family':'type','tag':tag,'fields':fields}
def _words(n):
    value=_type('Word')
    for _ in range(n-1):value=_type('Pair',left=_type('Word'),right=value)
    return value


def build_cases(source_bytes,response_bytes,actual_frontend_exit):
    if type(source_bytes) is not bytes or type(response_bytes) is not bytes or type(actual_frontend_exit) is not int or actual_frontend_exit!=0:
        raise ValueError('real source/response bytes and actual frontend exit0 required')
    original=json.loads(response_bytes)
    if original['status']!='ready' or original['schema']!='p8-runtime-frontend-response-v1':raise ValueError('actual ready response required')
    program=original['program'];source=program['source']
    if program['profile']!='src1b-parent-host-close-v1' or base64.b64decode(source['utf8_base64'],validate=True)!=source_bytes or source['bytes_sha256']!=hashlib.sha256(source_bytes).hexdigest():
        raise ValueError('original checked parent source identity required')
    origins={'source_sha256':hashlib.sha256(source_bytes).hexdigest(),'response_sha256':hashlib.sha256(response_bytes).hexdigest(),'actual_frontend_exit':actual_frontend_exit}
    result=[]
    for case,message in CASES:
        response=deepcopy(original);p=response['program']
        def command(kind):
            rows=[r for r in p['sites'] if r['node_kind']=='command:'+kind]
            if len(rows)!=1:raise ValueError('one complete original '+kind+' site required')
            return rows[0]
        service=command('call_service');tool=command('call_tool')
        def child(row,suffix):
            return next(r for r in p['sites'] if r['site']['pointer']==row['site']['pointer']+'/'+suffix)
        scope=child(service,'scope');tokens=child(service,'tokens');argument=child(tool,'argument')
        def edit(row,key,change):
            value=_read(p,row[key]);change(value);row[key]=_append(p,value)
        def different_scope(value):
            # The existing INIT-private Scope is a well-shaped different identity.
            value.clear();value['nominal']={'site':{'source_sha256':source['canonical_sha256'],'pointer':'/main/body'},'role':'init-private','index':'6','sort':'Scope'}
        def operation(effect,tag):return next(x for x in effect['fields']['atoms'] if x['tag']==tag)
        if case=='unknown-profile':p['profile']='src1b-forged-parent'
        elif case=='wrong-sigma-id':p['sigma']['id']='not-the-fixed-registry'
        elif case=='source-bytes-hash':p['source']['bytes_sha256']='0'*64
        elif case=='service-result-unit':service['type']=_append(p,_type('Unit'))
        elif case=='service-tokens-seven':tokens['type']=_append(p,_words(7))
        elif case=='tool-argument-nat':argument['type']=_append(p,_type('Nat'))
        elif case=='tool-result-word-length':edit(tool,'type',lambda v:v['fields'].__setitem__('left',_type('Word')))
        elif case=='scope-kind':edit(scope,'type',lambda v:v['fields'].__setitem__('kind','forged'))
        elif case=='scope-identity':edit(scope,'type',lambda v:different_scope(v['fields']['scope']))
        elif case=='service-effect-literal':edit(service,'effect',lambda v:operation(v,'HostService')['fields'].__setitem__('service','C'))
        elif case=='service-effect-scope':edit(service,'effect',lambda v:different_scope(operation(v,'HostService')['fields']['scope']))
        elif case=='tool-capability':edit(tool,'effect',lambda v:operation(v,'LocalRead')['fields'].__setitem__('capability','other_file'))
        elif case=='missing-raise':edit(service,'effect',lambda v:v['fields'].__setitem__('atoms',[a for a in v['fields']['atoms'] if a['tag']!='Raise']))
        elif case=='extra-effect':edit(tool,'effect',lambda v:v['fields']['atoms'].append({'family':'atom','tag':'Raise','fields':{}}))
        elif case=='command-direct-copy':service['usage']['copy']=deepcopy(scope['usage']['copy'])
        else:raise AssertionError('unhandled fixed case')
        if response==original:raise ValueError('case made no change: '+case)
        # Old static-table bytes remain equal: mutations append independent DAGs.
        if p['static_values'][:len(program['static_values'])]!=program['static_values']:raise ValueError('shared original static leaf changed')
        result.append({'id':case,'expected_message':message,'input_origin':dict(origins),'response':response})
    return result
