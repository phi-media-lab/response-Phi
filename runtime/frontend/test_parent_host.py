#!/usr/bin/env python3
"""First-run tests await independent source review; no execution in author turn."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
import parent_fixture as f
from p8source.entry import check_closed_main,check_open_fragment
from p8source.execution import prepare_execution
from p8source.sigma import fixed_parent_sigma,fixed_sigma,wire_sigma,PARENT_FORBIDDEN
from p8source.binding import free_terms
from p8source.source_schema import source_children


def static(program,ref):
    value=program['static_values'][int(ref['ref'])]
    if value is None or type(value) in (str,bool):return value
    if 'integer' in value:return int(value['integer'])
    if 'array' in value:return [static(program,x) for x in value['array']]
    return {k:static(program,v) for k,v in value['object']}


class ParentHostTests(unittest.TestCase):
    def accepted(self,raw):
        result=check_closed_main(fixed_parent_sigma(),raw)
        self.assertEqual(result.verdict,'checked-closed-main-source',result.diagnostic)
        return result
    def rejected(self,raw,message=None,sigma=None):
        result=check_closed_main(fixed_parent_sigma() if sigma is None else sigma,raw)
        self.assertIn(result.verdict,('semantic-no','unsupported'),result.diagnostic)
        self.assertIsNone(result.derivation)
        if message:self.assertIn(message,result.diagnostic['message'])
        return result
    def exported(self,raw):
        data=json.dumps(raw,ensure_ascii=False,separators=(',',':')).encode()
        response=prepare_execution(fixed_parent_sigma(),data)
        self.assertEqual(response['status'],'ready',response['diagnostic'])
        return response['program']
    def test_complete_parent_profile_and_result(self):
        raw=f.source();self.accepted(raw);program=self.exported(raw)
        self.assertEqual(program['profile'],'src1b-parent-host-close-v1')
        self.assertEqual(program['sigma'],wire_sigma(fixed_parent_sigma()))
        self.assertEqual(sorted(program['sigma']),['abi','host_parent_contract','host_service_contract','id','operations','tool_contract'])
        self.assertEqual(free_terms(raw['main']['body']),frozenset())
    def test_exact_two_command_exports(self):
        program=self.exported(f.source());rows={r['node_kind']:r for r in program['sites'] if r['node_kind'] in ('command:call_service','command:call_tool')}
        self.assertEqual(set(rows),{'command:call_service','command:call_tool'})
        expected={'command:call_service':('Word','HostService',{'service':'B'}),'command:call_tool':('Pair','LocalRead',{'capability':'tokenizer_vocab_v1'})}
        for kind,(tag,atom,literals) in expected.items():
            row=rows[kind];typ=static(program,row['type']);effect=static(program,row['effect'])
            self.assertEqual((typ['family'],typ['tag']),('type',tag))
            self.assertEqual((effect['family'],effect['tag']),('effect','set'))
            atoms=effect['fields']['atoms'];self.assertEqual(sorted(a['tag'] for a in atoms),sorted([atom,'Raise']))
            selected=next(a for a in atoms if a['tag']==atom)
            for key,value in literals.items():self.assertEqual(selected['fields'][key],value)
            self.assertEqual(row['usage']['copy'],[]);self.assertEqual(row['usage']['move'],[]);self.assertIsNone(row['frame'])
        self.assertEqual(source_children(f.service(),'command')[0][0],('scope',))
        self.assertEqual(source_children(f.service(),'command')[1][0],('tokens',))
    def test_child_rejects_parent_commands(self):self.rejected(f.source(),'parent command prohibited',fixed_sigma())
    def test_child_rejects_declaration_only_parent_effect(self):
        for atom in (f.atom('LocalRead',capability='tokenizer_vocab_v1'),f.atom('HostService',scope='NoAuthority',service='B')):
            raw={'schema':'p8-runtime-closed-source-v0.1','main':{'result':f.t('Unit'),'effect':f.eff(atom),'body':f.ret()}}
            self.rejected(raw,'parent effect prohibited',fixed_sigma())
    def test_child_original_empty_source_unchanged(self):
        raw={'schema':'p8-runtime-closed-source-v0.1','main':{'result':f.t('Unit'),'effect':f.eff(),'body':f.ret()}}
        self.assertEqual(check_closed_main(fixed_sigma(),raw).verdict,'checked-closed-main-source')
        response=prepare_execution(fixed_sigma(),json.dumps(raw).encode())
        self.assertEqual(response['status'],'ready');self.assertEqual(response['program']['profile'],'src1-response-offer-owner1-v1')
        self.assertEqual(response['program']['sigma'],wire_sigma())
    def test_parent_rejects_existing_backend_commands(self):
        def names(sorts):return [{'name':'N'+str(i),'sort':sort} for i,sort in enumerate(sorts)]
        commands=[f.c('new_owner',value=f.v('nat',literal=1),names=[{'name':'N','sort':'Size'},{'name':'O','sort':'Owner'}],name='owner',body=f.ret()),
            f.c('run',owner=f.var('owner'),names=names(['Epoch','Scope','Region','Invocation']),name='runScope',body=f.ret()),
            f.c('fork',scope=f.var('s'),names=names(['Job','Task','Invocation','Region']),job={'captures':[],'result':f.t('Unit'),'body':f.ret()},result_name='h',control_name='k',next=f.ret()),
            f.c('produce',scope=f.var('s'),kernel='Prefill8',argument=f.tokens_value(),names=names(['Job','Task','Invocation','Region','Access','Access','Region']),
                cleanup={'captures':[],'parameter':{'name':'r'},'result':f.t('Unit'),'body':f.ret()},result_name='h',control_name='k',next=f.ret()),
            f.c('receive_request',name='B',owner=f.var('owner')),f.c('offer_response',name='B',owner=f.var('owner'),token=f.word(1)),
            f.c('await',shared=f.var('h')),f.c('await_cancel',shared=f.var('h')),f.c('load',read=f.var('r'),index=f.v('nat',literal=0)),
            f.c('request_cancel',control=f.var('k'),next=f.ret())]
        self.assertEqual({x['command'] for x in commands},set(PARENT_FORBIDDEN))
        for command in commands:
            with self.subTest(command=command['command']):self.rejected(f.in_scope(command,f.eff()),'command prohibited by parent profile')
    def test_open_parent_has_no_private_authority(self):
        result=check_open_fragment(fixed_parent_sigma(),{})
        self.assertEqual(result.verdict,'semantic-no');self.assertIn('private closed INIT',result.diagnostic['message'])
    def test_service_missing_extra_and_wrong_fields(self):
        for mutation in ('missing','extra','name'):
            raw=f.source();node=f.service_node(raw)
            if mutation=='missing':del node['tokens']
            elif mutation=='extra':node['caller']='main'
            else:node['name']='C'
            with self.subTest(mutation=mutation):self.rejected(raw)
    def test_tool_exact_dotted_name_only(self):
        raw=f.source();f.tool_node(raw)['name']='tokenizer.other';self.rejected(raw,'fixed readonly-tool name')
        raw=f.source();raw['main']['body']['body']['name']='bad.binding';self.rejected(raw)
    def test_service_requires_eight_complete_words(self):
        for count in (1,7,9):
            raw=f.source();f.service_node(raw)['tokens']=f.tokens_value(tuple(range(count)))
            with self.subTest(count=count):self.rejected(raw,'Tokens8')
        raw=f.source();f.service_node(raw)['tokens']=f.v('nat',literal=8);self.rejected(raw,'Tokens8')
    def test_service_scope_type(self):
        raw=f.source();f.service_node(raw)['scope']=f.word(1);self.rejected(raw,'plain Scope')
    def test_tool_word_argument(self):
        for value in (f.v('unit'),f.v('nat',literal=1),f.v('pair',left=f.word(1),right=f.word(2))):
            raw=f.source();f.tool_node(raw)['argument']=value;self.rejected(raw,'argument must be Word')
    def test_missing_service_effect(self):
        raw=f.source();f.declared_atoms(raw)[:]=[x for x in f.declared_atoms(raw) if x['atom']!='HostService'];self.rejected(raw,'effect upper bound')
    def test_missing_tool_effect(self):
        raw=f.source();f.declared_atoms(raw)[:]=[x for x in f.declared_atoms(raw) if x['atom']!='LocalRead'];self.rejected(raw,'effect upper bound')
    def test_try_does_not_absorb_local_read(self):
        raw=f.source();slot=raw['main']['body']['body']['next']['next']
        zero=f.v('pair',left=f.v('nat',literal=0),right=f.tokens_value((0,0,0,0)))
        slot['first']=f.c('try',body=slot['first'],failure_name='failure',handler=f.ret(zero))
        self.accepted(raw)
        f.declared_atoms(raw)[:]=[x for x in f.declared_atoms(raw) if x['atom']!='LocalRead']
        self.rejected(raw,'effect upper bound')
    def test_effect_literal_not_nominal(self):
        for tag,key,value in [('HostService','service','C'),('LocalRead','capability','other_file')]:
            raw=f.source();next(a for a in f.declared_atoms(raw) if a['atom']==tag)[key]=value;self.rejected(raw,'unknown')
    def test_no_explicit_plain_scope_authority(self):
        raw={'schema':'p8-runtime-closed-source-v0.1','main':{'result':f.tool_info_type(),'effect':f.eff(f.atom('LocalRead',capability='tokenizer_vocab_v1'),f.atom('Raise')),'body':f.tool(f.word(1))}}
        self.rejected(raw,'current plain scope')
    def test_private_main_not_symbolic_lambda(self):
        fn=f.v('lambda',mode='U',captures=[],foot={'support':'all','items':[]},projections={'selectors':[],'entries':[]},requires={'requires':'true'},links=[],
               effect=f.eff(f.atom('LocalRead',capability='tokenizer_vocab_v1'),f.atom('Raise')),
               parameter={'name':'arg','type':f.t('Word')},result=f.tool_info_type(),body=f.tool(f.var('arg')))
        raw=f.in_scope(f.c('let_e',name='fn',expression={'expression':'value','value':fn},next=f.ret()),f.eff())
        self.rejected(raw,'original INIT main')
    def test_foreign_outer_scope_rejected(self):
        inner=f.in_scope(f.service(scope='outer',tokens=f.tokens_value()),f.eff(f.atom('HostService',scope='InnerS',service='B'),f.atom('Raise')),f.t('Word'),prefix='Inner',name='inner')
        raw=f.in_scope(inner['main']['body'],inner['main']['effect'],f.t('Word'),name='outer')
        self.rejected(raw,'current innermost scope')
    def test_declared_result_type_exact(self):
        raw=f.source();raw['main']['result']=f.t('Unit');self.rejected(raw,'type')


if __name__=='__main__':unittest.main()
