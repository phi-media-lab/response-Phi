"""Strict JSON decoder -> typed immutable nodes, never string scans or eval."""
import json
import re
from .syntax import (Name,Size,Witness,Relation,Telescope,Foot,Effect,Type,Node,Theta,
    Binding,Program,PureSignature,KernelSignature,Signature,ParseError,SORTS,
    VALUE_FIELDS,EXPR_FIELDS,COMMAND_FIELDS,fields)

IDENT=re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')

def fail(where,message):raise ParseError(where+': '+message)
def obj(value,keys,where):
    if type(value) is not dict:fail(where,'object required')
    if set(value)!=set(keys):fail(where,'exact fields required: '+','.join(sorted(keys)))
    return value
def sequence(value,where):
    if type(value) is not list:fail(where,'array required')
    return value
def ident(value,where):
    if type(value) is not str or IDENT.fullmatch(value) is None:fail(where,'identifier required')
    return value
def unique(values,where):
    if len(values)!=len(set(values)):fail(where,'duplicate entries')
    return tuple(values)
def natural(value,where):
    if type(value) is not int or value<0:fail(where,'nonnegative integer required (not bool)')
    return value
def name(value,where):
    obj(value,{'name','sort'},where);text=ident(value['name'],where+'.name')
    if type(value['sort']) is not str or value['sort'] not in SORTS:fail(where,'unknown nominal sort')
    return Name(text,value['sort'])
def size(value,where):
    if type(value) is int:return Size(literal=natural(value,where))
    obj(value,{'witness'},where)
    return Size(witness=ident(value['witness'],where+'.witness'))

TYPE_FIELDS={
    **{t:{} for t in ('Unit','Bool','Nat','Word','ErrorPayload','FaultSet','Failure')},
    'Vector':{'size':'size'},'Pair':{'left':'type','right':'type'},'Sum':{'left':'type','right':'type'},
    'Exists':{'interface':'telescope','body':'type'},
    'Fn':{'mode':'mode','foot':'foot','effect':'effect','parameter':'type','result':'type'},
    'Owner':{'owner':'id','size':'size'},'Scope':{'scope':'id','region':'id','kind':'kind'},
    'Shared':{'job':'id','payload':'type'},'Control':{'job':'id'},
    'Lease':{'scope':'id','owner':'id','epoch':'id','lease':'id','region':'id'},
    'Read':{'access':'id','owner':'id','epoch':'id','region':'id'},
    'Write':{'owner':'id','epoch':'id'},
}
REL_FIELDS={
    'owner':{'owner':'id','size':'size'},'positiveSize':{'size':'size'},'sizeEqual':{'left':'size','right':'size'},
    'epoch':{'epoch':'id','owner':'id','ordinal':'nat'},
    'scope':{'scope':'id','region':'id','kind':'kind','parent_invocation':'optional_id'},
    'job':{'job':'id','task':'id','scope':'id','result':'type'},
    'jobRegion':{'job':'id','region':'id'},'bodyRegion':{'job':'id','role':'role','region':'id'},
    'result':{'job':'id','result':'type'},'producerOf':{'job':'id','scope':'id','owner':'id','epoch':'id'},
    'access':{'access':'id','job':'id','owner':'id','epoch':'id','region':'id'},
    'outlives':{'inner':'id','outer':'id'},
}
EFFECT_FIELDS={
    'set':{'atoms':'atoms'},'union':{'left':'effect','right':'effect'},
    'nu':{'name':'nominal','body':'effect'},
}
ATOM_FIELDS={
    'Alloc':{},'Raise':{},'Enter':{'scope':'id'},'Register':{'scope':'id'},
    'Issue':{'owner':'id'},'Read':{'owner':'id'},'HostInput':{'owner':'id'},'HostResponse':{'owner':'id'},'Need':{'job':'id','mode':'wait_mode'},
    'HostService':{'scope':'id','service':'service_literal'},'LocalRead':{'capability':'capability_literal'},
    'WaitDetach':{'job':'id'},'RequestCancel':{'job':'id'},'Drain':{'scope':'id'},
}

def tagged(value,key,schemas,constructor,where):
    if type(value) is not dict or key not in value or type(value[key]) is not str:fail(where,'tagged object required')
    tag=value[key]
    if tag not in schemas:fail(where,'unknown '+key+' '+tag)
    schema=schemas[tag];obj(value,{key,*schema},where)
    return constructor(tag,tuple((k,convert(value[k],conversion,where+'.'+k)) for k,conversion in schema.items()))
def type_node(value,where='type'):return tagged(value,'type',TYPE_FIELDS,Type,where)
def relation(value,where):return tagged(value,'relation',REL_FIELDS,Relation,where)
def telescope(value,where):
    obj(value,{'names','facts'},where)
    names=tuple(name(x,where+'.names') for x in sequence(value['names'],where+'.names'))
    unique([n.text for n in names],where+'.names')
    facts=tuple(relation(x,where+'.facts') for x in sequence(value['facts'],where+'.facts'))
    unique(facts,where+'.facts')
    return Telescope(names,facts)
def effect(value,where):return tagged(value,'effect',EFFECT_FIELDS,Effect,where)
def footprint(value,where):
    arities={'scope':2,'lease':5,'read':4,'write':2,'region':1}
    answer=[]
    for item in sequence(value,where):
        obj(item,{'kind','names'},where)
        if type(item['kind']) is not str or item['kind'] not in arities:fail(where,'unknown footprint kind')
        names=tuple(ident(x,where+'.names') for x in sequence(item['names'],where+'.names'))
        if len(names)!=arities[item['kind']]:fail(where,'wrong footprint identity arity')
        answer.append(Foot(item['kind'],names))
    return frozenset(unique(answer,where))
def node(value,family,where):
    schemas={'v':VALUE_FIELDS,'e':EXPR_FIELDS,'c':COMMAND_FIELDS}[family]
    return tagged(value,'node',schemas,lambda tag,f:Node(family,tag,f),where)

def convert(value,kind,where):
    if kind in ('service_literal','capability_literal'):
        wanted={'service_literal':'B','capability_literal':'tokenizer_vocab_v1'}[kind]
        if type(value) is not str or value != wanted:fail(where,'unknown '+kind)
        return value
    if kind in ('v','e','c'):return node(value,kind,where)
    if kind=='id':return ident(value,where)
    if kind=='optional_id':return None if value is None else ident(value,where)
    if kind=='nominal':return name(value,where)
    if kind=='witnesses':
        result=[]
        for item in sequence(value,where):
            if type(item) is not dict:fail(where,'structured witness required')
            if set(item)=={'name'}:result.append(Witness(name=ident(item['name'],where+'.name')))
            elif set(item)=={'size'}:result.append(Witness(size=size(item['size'],where+'.size')))
            else:fail(where,'witness is exactly name or size')
        return tuple(result)  # substitution need not be injective
    if kind=='nat':return natural(value,where)
    if kind=='int':
        if type(value) is not int:fail(where,'integer required (not bool)')
        return value
    if kind=='bool':
        if type(value) is not bool:fail(where,'boolean required')
        return value
    if kind in ('ids','ints','values'):
        conversion={'ids':'id','ints':'int','values':'v'}[kind]
        out=tuple(convert(x,conversion,where) for x in sequence(value,where))
        return unique(out,where) if kind=='ids' else out
    if kind=='type':return type_node(value,where)
    if kind=='size':return size(value,where)
    if kind=='telescope':return telescope(value,where)
    if kind=='effect':return effect(value,where)
    if kind=='foot':return footprint(value,where)
    if kind=='atoms':return frozenset(unique([tagged(x,'atom',ATOM_FIELDS,Effect,where) for x in sequence(value,where)],where))
    if kind in ('mode','wait_mode','role'):
        choices={'mode':{'U','A'},'wait_mode':{'publication','terminal'},'role':{'ordinary','reader','cleanup'}}[kind]
        if type(value) is not str or value not in choices:fail(where,'unknown '+kind)
        return value
    if kind=='kind':
        if value=='plain':return 'plain'
        obj(value,{'owner','epoch'},where)
        return (ident(value['owner'],where+'.owner'),ident(value['epoch'],where+'.epoch'))
    if kind in ('producer_names','job_names','reader_names'):
        keys={'job','task','invocation','region'}
        if kind=='producer_names':keys|={'write_access','cleanup_access','cleanup_region'}
        if kind=='reader_names':keys|={'access','read_region'}
        obj(value,keys,where)
        parsed=tuple((k,ident(value[k],where+'.'+k)) for k in sorted(keys))
        unique([x[1] for x in parsed],where)
        return parsed
    fail(where,'internal schema conversion missing '+kind)

def binding(value,where):
    obj(value,{'name','type'},where)
    return Binding(ident(value['name'],where+'.name'),type_node(value['type'],where+'.type'))
def signature(value):
    obj(value,{'pure','kernels'},'signature');pure=[];kernels=[]
    for item in sequence(value['pure'],'signature.pure'):
        obj(item,{'name','parameters','result'},'pure signature')
        pure.append(PureSignature(ident(item['name'],'pure name'),
            tuple(type_node(t,'pure parameter') for t in sequence(item['parameters'],'pure parameters')),
            type_node(item['result'],'pure result')))
    for item in sequence(value['kernels'],'signature.kernels'):
        obj(item,{'name','size_binder','parameter','size','postcondition'},'kernel signature')
        binder=None if item['size_binder'] is None else name(item['size_binder'],'kernel size binder')
        if binder is not None and binder.sort!='Size':fail('kernel','size binder must have Size sort')
        kernels.append(KernelSignature(ident(item['name'],'kernel name'),binder,
            type_node(item['parameter'],'kernel parameter'),size(item['size'],'kernel size'),
            ident(item['postcondition'],'kernel postcondition')))
    unique([s.name for s in pure+kernels],'signature names')
    return Signature(tuple(pure),tuple(kernels))
def parse_program(value):
    obj(value,{'schema','signature','world','context','body','result'},'program')
    if value['schema']!='p8-static-json-v0.1':fail('program','unsupported schema')
    c=obj(value['context'],{'theta','copy','affine'},'context')
    t=obj(c['theta'],{'job','task','invocation','region','scope_stack'},'theta')
    theta=Theta(*(ident(t[k],'theta.'+k) for k in ('job','task','invocation','region')),
        tuple(ident(x,'theta.scope_stack') for x in sequence(t['scope_stack'],'theta.scope_stack')))
    copy=tuple(binding(x,'context.copy') for x in sequence(c['copy'],'context.copy'))
    affine=tuple(binding(x,'context.affine') for x in sequence(c['affine'],'context.affine'))
    unique([b.name for b in (*copy,*affine)],'context variables')
    return Program(signature(value['signature']),telescope(value['world'],'world'),theta,copy,affine,
        node(value['body'],'c','body'),type_node(value['result'],'result'))
def decode_document(text):
    if type(text) is not str:fail('document','UTF-8 decoded string required')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:fail('document','duplicate JSON key '+key)
            result[key]=value
        return result
    def constant(value):fail('document','nonfinite JSON number '+value)
    try:value=json.loads(text,object_pairs_hook=pairs,parse_constant=constant)
    except json.JSONDecodeError as error:fail('document','invalid JSON: '+error.msg)
    return value

def loads(text):return parse_program(decode_document(text))
