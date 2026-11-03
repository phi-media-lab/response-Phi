#!/usr/bin/env python3
"""Independent tiny-decode-cost event/byte audit. Never imports runner/MLX/model.

Token equality is against separately recorded isolated executions, not a second
model computation. Each successful trial is one statistical unit; this checker
does not rank performance or accept failed/unknown-completion records as success.
"""
import argparse
import ast
import copy
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

POLICIES = ('no-b', 'whole', 'fixed-32', 'fixed-128', 'fixed-512', 'cost-200')
PAGE, BUDGET, CANDIDATES = 128, 200_000_000, (32, 64, 128, 256, 512)
SPEC_KEYS = ('policy', 'a_prompt_length', 'b_prompt_length', 'a_decode_steps', 'arrival_after_a_decode', 'trials', 'warmup')
CHECKS = {'trial_execution_succeeded', 'all_fixed_work_completed', 'A_all_tokens_match_isolated',
          'B_fixed_output_count', 'all_snapshots_consistent', 'all_resources_returned',
          'no_B_business_work_when_absent', 'fixed_inputs_outputs_evaluated_heads', 'complete_A_interval_sequences'}
HELPER = 'cf2cb10f68165a7caa54f340f755e8828cbefb45e6bfcb4f92891546776c9201'
HELPER_PROTOCOL = 'cc1db8957f019869d6f591af61e55b58434d76e50a0e116fdd5dbf91df6528ee'
GENERATED_MODULE = '_remote_module_non_scriptable'


def torch_generated_source(generator_text,template_text):
    """Only parse constants and a narrowly checked AST; never execute Torch."""
    generator=ast.parse(generator_text);template=ast.parse(template_text)
    def function(tree,name):
        found=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name]
        need(len(found)==1,'generated-source function missing/duplicate: '+name)
        return found[0]
    def constant_assignments(tree):
        values={}
        for node in tree.body:
            if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
                try: value=ast.literal_eval(node.value)
                except (ValueError,TypeError): pass
                else:
                    need(node.targets[0].id not in values,'duplicate literal generator/template assignment')
                    values[node.targets[0].id]=value
        return values
    def same_ast(node,text):
        return ast.dump(node,include_attributes=False)==ast.dump(ast.parse(text).body[0],include_attributes=False)
    need(constant_assignments(generator).get('_FILE_PREFIX')=='_remote_module_','unexpected generated module prefix')
    instantiate=function(generator,'instantiate_non_scriptable_remote_module_template')
    need(len(instantiate.body)==3 and same_ast(instantiate.body[0],'generated_module_name = f"{_FILE_PREFIX}non_scriptable"'),'generated module name derivation differs')
    assignment=instantiate.body[1]
    need(isinstance(assignment,ast.Assign) and len(assignment.targets)==1 and isinstance(assignment.targets[0],ast.Name)
         and assignment.targets[0].id=='str_dict' and isinstance(assignment.value,ast.Call)
         and isinstance(assignment.value.func,ast.Name) and assignment.value.func.id=='dict' and not assignment.value.args,
         'generated formatting arguments must be literal named dict')
    parameters={}
    for keyword in assignment.value.keywords:
        need(keyword.arg is not None and keyword.arg not in parameters,'duplicate/expanded generated formatting parameter')
        parameters[keyword.arg]=ast.literal_eval(keyword.value)
    expected=dict(assign_module_interface_cls='module_interface_cls = None',args='*args',kwargs='**kwargs',
                  arg_types='*args, **kwargs',arrow_and_return_type='',arrow_and_future_return_type='',jit_script_decorator='')
    need(exact(parameters,expected),'non-scriptable generated formatting parameters differ')
    need(same_ast(instantiate.body[2],'return _do_instantiate_remote_module_template(generated_module_name, str_dict, True)'),
         'generated template selector differs')
    renderer=function(generator,'_do_instantiate_remote_module_template')
    need(same_ast(renderer.body[0],'generated_code_text = get_remote_module_template(enable_moving_cpu_tensors_to_cuda).format(**str_dict)'),
         'generated renderer formula differs')
    selector=function(template,'get_remote_module_template')
    need(len(selector.body)==1 and same_ast(selector.body[0],
        'return _TEMPLATE_PREFIX + (_REMOTE_FORWARD_TEMPLATE_ENABLE_MOVING_CPU_TENSORS_TO_CUDA if enable_moving_cpu_tensors_to_cuda else _REMOTE_FORWARD_TEMPLATE)'),
        'generated template selection formula differs')
    values=constant_assignments(template)
    first=values.get('_TEMPLATE_PREFIX');second=values.get('_REMOTE_FORWARD_TEMPLATE_ENABLE_MOVING_CPU_TENSORS_TO_CUDA')
    need(isinstance(first,str) and isinstance(second,str),'generated template literals missing')
    text=(first+second).format(**parameters);ast.parse(text)
    return text.encode('utf-8')


def need(ok, message):
    if not ok:
        raise ValueError(message)


def integer(value, low=0, high=None):
    return type(value) is int and value >= low and (high is None or value <= high)


def exact(a, b):
    if type(a) is not type(b): return False
    if isinstance(b, dict): return a.keys() == b.keys() and all(exact(a[k], v) for k, v in b.items())
    if isinstance(b, list): return len(a) == len(b) and all(exact(x, y) for x, y in zip(a, b))
    return a == b


def numeric_equal(a, b):
    if isinstance(b, dict): return isinstance(a, dict) and a.keys() == b.keys() and all(numeric_equal(a[k], v) for k, v in b.items())
    if isinstance(b, list): return isinstance(a, list) and len(a) == len(b) and all(numeric_equal(x, y) for x, y in zip(a, b))
    if type(b) is float:
        return type(a) in (int, float) and math.isfinite(a) and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9)
    return exact(a, b)


def object_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''): digest.update(chunk)
    return digest.hexdigest()


def tokens(value, length=None):
    return isinstance(value, list) and (length is None or len(value) == length) and all(integer(x) for x in value)


class Cursor:
    def __init__(self, events):
        need(isinstance(events, list) and events, 'missing event trace')
        previous = -1
        for index, event in enumerate(events):
            need(isinstance(event, dict) and set(event) == {'seq','name','t_ns','t_ms','request_id','details'}, 'event schema')
            need(integer(event['seq']) and event['seq'] == index and integer(event['t_ns']) and event['t_ns'] >= previous, 'event index/time order')
            need(type(event['t_ms']) in (int,float) and math.isfinite(event['t_ms']) and event['t_ms'] == event['t_ns']/1e6, 'derived event milliseconds')
            need(isinstance(event['name'], str) and isinstance(event['request_id'], str) and isinstance(event['details'], dict), 'event identity')
            previous = event['t_ns']
        self.events, self.index = events, 0

    def take(self, name, who='system', details=None):
        need(self.index < len(self.events), 'missing event '+name)
        event = self.events[self.index]
        need(event['name'] == name and event['request_id'] == who, 'event order: expected '+name+' '+who+', got '+event['name']+' '+event['request_id'])
        if details is not None: need(exact(event['details'], details), 'event details differ: '+name)
        self.index += 1
        return event

    def end(self): need(self.index == len(self.events), 'unexpected trailing events')


def config_checked(config):
    need(config.get('policy') in POLICIES, 'unknown policy')
    for key, high in [('a_prompt_length',1024), ('b_prompt_length',4096), ('a_decode_steps',40), ('trials',20)]:
        need(integer(config.get(key),1,high), 'invalid '+key)
    need(integer(config.get('warmup'),0,5) and integer(config.get('arrival_after_a_decode'),1,config['a_decode_steps']-1), 'invalid warmup/anchor')
    need(exact({k:config.get(k) for k in ('page_size','intermediate_eval','b_present','budget_ns','candidate_chunks','fixed_length_ignores_eos')},
        dict(page_size=PAGE, intermediate_eval='pools-only', b_present=config['policy']!='no-b', budget_ns=BUDGET,
             candidate_chunks=list(CANDIDATES), fixed_length_ignores_eos=True)), 'work/evaluation configuration differs')
    need(set(config) == set(SPEC_KEYS)|{'page_size','intermediate_eval','b_present','budget_ns','candidate_chunks','fixed_length_ignores_eos'}, 'extra configuration field')


def capacity_for(pages):
    capacity = 0
    for used in range(1,pages+1):
        if used > capacity: capacity = max(4,used,capacity*2)
    return capacity


def reference_checked(record, inputs, partition, decode, layers, page_bytes=None):
    need(record.get('passed') is True and record.get('error') is None and not record.get('cleanup_error'), 'reference failed/unknown')
    need(integer(record.get('clock_origin_ns'),1) and integer(record.get('elapsed_ns'),1), 'reference clocks missing')
    need(tokens(record.get('output_token_ids'),decode+1), 'reference output count/type')
    need(exact(record.get('partition'),partition) and record.get('partition_sha256') == object_sha(partition), 'reference partition differs')
    need(partition and all(integer(n,1) for n in partition) and sum(partition) == len(inputs), 'reference partition coverage')
    cursor = Cursor(record['events']); offset = output = 0
    for index in range(len(partition)+decode):
        prefill = index < len(partition)
        part = inputs[offset:offset+partition[index]] if prefill else [record['output_token_ids'][output-1]]
        head = not prefill or index == len(partition)-1
        cursor.take('reference.forward_started','reference',dict(offset=offset,input_token_ids=part,input_tokens=len(part),kind='prefill' if prefill else 'decode',head_evaluated=head))
        cursor.take('reference.forward_returned','reference',dict(head_graph_constructed=True,head_evaluated=head))
        cursor.take('reference.eval_started','reference',dict(head_evaluated=head))
        cursor.take('reference.eval_returned','reference',dict(head_evaluated=head))
        offset += len(part)
        if head:
            cursor.take('reference.token_delivered','reference',dict(token_id=record['output_token_ids'][output],output_index=output)); output += 1
    for name in ('reference.synchronize_started','reference.synchronize_completed','reference.cache_released','reference.drained'):
        cursor.take(name,details={})
    cursor.end()
    need(record['elapsed_ns'] >= cursor.events[-1]['t_ns'], 'reference elapsed precedes drain')
    pages = (len(inputs)+decode+PAGE-1)//PAGE; capacity = capacity_for(pages)
    pool = record['final_pool']; backing = pool.get('backing_bytes')
    need(integer(backing,1) and backing % (layers*capacity) == 0, 'reference backing geometry')
    actual_page_bytes = backing//(layers*capacity)
    if page_bytes is not None: need(actual_page_bytes == page_bytes, 'reference geometry changed')
    need(exact(pool,dict(used_pages=0,free_pages=pages*layers,allocated_page_ids=pages*layers,
         backing_capacity_pages=capacity*layers,backing_bytes=backing,conserved=True)), 'reference pool not drained/conserved')
    return actual_page_bytes


class Pages:
    """Own integer page/offset model; no cache implementation import."""
    def __init__(self, config, layers, page_bytes):
        self.layers,self.page_bytes = layers,page_bytes
        self.sizes = {'A':(config['a_prompt_length']+config['a_decode_steps']+PAGE-1)//PAGE,
                      'B':(config['b_prompt_length']+PAGE-1)//PAGE}
        self.reserved,self.caches = {},{}
        self.used,self.free,self.allocated,self.capacity = set(),[],0,0
        self.high,self.reused,self.growths,self.copied = 0,0,0,0

    def admit(self,who):
        need(who not in self.reserved,'duplicate admission')
        self.reserved[who]=self.sizes[who]
        self.high=max(self.high,sum(self.reserved.values()))
        self.caches[who]=dict(offset=0,page_ids=[],page_lens=[])

    def append(self,who,count):
        cache=self.caches[who]; remaining=count
        if cache['page_lens'] and cache['page_lens'][-1] < PAGE:
            take=min(PAGE-cache['page_lens'][-1],remaining);cache['page_lens'][-1]+=take;remaining-=take
        while remaining:
            if self.free: page=self.free.pop();self.reused+=1
            else: page=self.allocated;self.allocated+=1
            need(page not in self.used,'allocation aliases another owner')
            self.used.add(page)
            if self.allocated>self.capacity:
                if self.capacity: self.copied+=(self.allocated-1)*self.page_bytes
                self.capacity=max(4,self.allocated,self.capacity*2);self.growths+=1
            cache['page_ids'].append(page);cache['page_lens'].append(min(PAGE,remaining));remaining-=min(PAGE,remaining)
        cache['offset']+=count
        need(len(cache['page_ids'])<=self.sizes[who],'work exceeds reservation')

    def release(self,who):
        for page in self.caches[who]['page_ids']:
            need(page in self.used,'release unknown page');self.used.remove(page);self.free.append(page)
        del self.caches[who];del self.reserved[who]

    def snapshot(self,label,seq):
        total=sum(self.sizes.values());used=sum(self.reserved.values())
        return dict(label=label,event_seq=seq,
            owners={who:[dict(layer=i,**copy.deepcopy(cache)) for i in range(self.layers)] for who,cache in self.caches.items()},
            pools=[dict(layer=i,used_page_ids=sorted(self.used),free_page_ids=list(self.free),allocated_page_ids=self.allocated,
                        capacity_pages=self.capacity,backing_bytes=self.capacity*self.page_bytes,storage_growths=self.growths,
                        copied_bytes_on_growth=self.copied,reused_page_allocations=self.reused) for i in range(self.layers)],
            reservations=dict(capacity=total,used=used,free=total-used,owners=dict(self.reserved),high_watermark=self.high),consistent=True)


def decision_for(policy, remaining, rho, observations, step_id):
    predictions=[]
    if policy=='whole': selected,reason=remaining,'whole'
    elif policy.startswith('fixed-'): selected,reason=min(int(policy[6:]),remaining),'fixed'
    elif rho is None: selected,reason=min(32,remaining),'bootstrap'
    else:
        candidates=sorted(set(min(limit,remaining) for limit in CANDIDATES))
        predictions=[dict(tokens=n,predicted_ns=(5*rho*n+3)//4) for n in candidates]
        fitting=[r['tokens'] for r in predictions if r['predicted_ns']<=BUDGET]
        selected,reason=(max(fitting),'largest_predicted_fit') if fitting else (min(candidates),'minimum_progress_fallback')
    return dict(policy=policy,decision_id=observations,step_id=step_id,remaining_tokens=remaining,selected_tokens=selected,
                reason=reason,rho_before_ns_per_token=rho,completed_samples=observations,
                budget_ns=BUDGET if policy=='cost-200' else None,candidate_predictions=predictions,
                predicted_ns=next((r['predicted_ns'] for r in predictions if r['tokens']==selected),None),prediction_has_separate_head_term=False)


def summary(values):
    return dict(count=len(values),min=min(values),median=statistics.median(values),mean=statistics.mean(values),max=max(values)) if values else dict(count=0)


def metrics_recomputed(events,config,snapshots):
    def rows(name): return [e for e in events if e['name']==name]
    def t(name):
        selected=rows(name);need(len(selected)==1,'nonunique metric endpoint '+name);return selected[0]['t_ns']
    def intervals(rows): return [(b['t_ns']-a['t_ns'])/1e6 for a,b in zip(rows,rows[1:])]
    completed=rows('step.completed');delivered=[e for e in events if e['name'] in ('a.token_delivered','b.token_delivered')]
    starts={e['details']['step_id']:e['t_ns'] for e in rows('step.started')}
    decode={e['details']['step_id']:e['t_ns'] for e in rows('a.decode_call_started')}
    itl=intervals(rows('a.token_delivered'));after=itl[config['arrival_after_a_decode']:]
    duration=t('work.completed')-t('work.started');need(duration>0,'nonpositive work interval')
    inputs=sum(e['details']['input_tokens'] for e in completed)
    result=dict(work_ms=duration/1e6,through_drain_ms=(t('trial.drained')-t('work.started'))/1e6,
        a_completion_ms=(t('a.completed')-t('work.started'))/1e6,a_user_itl_ms=itl,a_user_itl_summary=summary(itl),
        a_after_anchor_itl_ms=after,a_after_anchor_itl_summary=summary(after),a_after_anchor_itl_sum_ms=sum(after),
        a_decode_call_ms=[(e['t_ns']-decode[e['details']['step_id']])/1e6 for e in rows('a.decode_call_completed')],
        global_step_completion_intervals_ms=intervals(completed),global_token_delivery_intervals_ms=intervals(delivered),
        total_model_input_tokens=inputs,total_output_tokens=len(delivered),constructed_head_graphs=len(rows('step.forward_returned')),
        evaluated_token_heads=sum(e['details']['head_evaluated'] for e in completed),model_input_tokens_per_second=inputs*1e9/duration,
        output_tokens_per_second=len(delivered)*1e9/duration,
        peak_active_page_slots=max(sum(len(p['used_page_ids']) for p in s['pools']) for s in snapshots),
        peak_backing_bytes=max(sum(p['backing_bytes'] for p in s['pools']) for s in snapshots),
        peak_reserved_pages_per_layer=max(s['reservations']['high_watermark'] for s in snapshots))
    if config['b_present']:
        b=[e for e in completed if e['request_id']=='B']
        decision_starts={e['details']['step_id']:e['t_ns'] for e in rows('policy.decision_started')}
        service={e['details']['step_id']:e['t_ns']-starts[e['details']['step_id']] for e in rows('step.yield_ready') if e['request_id']=='B'}
        result.update(b_ttft_ms=(t('b.first_token')-t('b.requested'))/1e6,b_admission_ms=(t('b.admitted')-t('b.requested'))/1e6,
            b_ready_wait_ms=(t('b.compute_started')-t('b.admitted'))/1e6,b_chunk_tokens=[e['details']['input_tokens'] for e in b],b_chunks=len(b),
            policy_decision_ms=[(e['t_ns']-decision_starts[e['details']['step_id']])/1e6 for e in rows('policy.decision_finished')],
            b_slice_service_ms=[service[e['details']['step_id']]/1e6 for e in b],
            b_final_head_service_ms=[service[e['details']['step_id']]/1e6 for e in b if e['details']['head_evaluated']],
            b_tokens_while_A_runnable=sum(e['details']['input_tokens'] for e in b if e['details']['a_runnable_before_step']),
            b_tokens_after_A_completed=sum(e['details']['input_tokens'] for e in b if not e['details']['a_runnable_before_step']),
            b_chunks_while_A_runnable=sum(e['details']['a_runnable_before_step'] for e in b),
            b_chunks_after_A_completed=sum(not e['details']['a_runnable_before_step'] for e in b),
            b_tail_service_ms=sum(service[e['details']['step_id']] for e in b if not e['details']['a_runnable_before_step'])/1e6)
        if config['policy']=='cost-200':
            obs=[e['details'] for e in rows('policy.observed')];dec=[e['details'] for e in rows('policy.decision_finished')]
            result.update(budget_overshoot_ns=[o['budget_overshoot_ns'] for o in obs],prediction_error_ns=[o['prediction_error_ns'] for o in obs],
                final_head_prediction_error_ns=[o['prediction_error_ns'] for o in obs if o['head_evaluated']],
                final_head_budget_overshoot_ns=[o['budget_overshoot_ns'] for o in obs if o['head_evaluated']],
                budget_exceeded_slices=sum(o['budget_overshoot_ns']>0 for o in obs),bootstrap_count=sum(d['reason']=='bootstrap' for d in dec),
                minimum_progress_fallback_count=sum(d['reason']=='minimum_progress_fallback' for d in dec))
    return result


def trial_checked(trial,config,inputs,refs,layers,page_bytes,trial_id,warmup):
    need(trial.get('passed') is True and trial.get('execution_passed') is True and not trial.get('unexpected_error'), 'failed/incomplete trial')
    need(integer(trial.get('clock_origin_ns'),1) and exact(trial.get('trial_id'),trial_id) and exact(trial.get('warmup'),warmup), 'trial identity/clock')
    tc={k:v for k,v in config.items() if k not in ('trials','warmup')}
    need(exact(trial.get('config'),tc),'trial configuration drift')
    obs=trial['observations'];need(obs.get('diagnostic_errors')==[] and obs.get('pending_step_id') is None,'diagnostic failure/pending work')
    for flag in ('hardware_parallelism_claimed','prediction_has_separate_head_term','physical_backing_capacity_capped'):
        need(obs.get(flag) is False,'unsupported claim '+flag)
    cursor=Cursor(trial['events']);pages=Pages(config,layers,page_bytes);snapshots=[]
    outputs={'A':[],'B':[]};partition=[];a_prefilled=anchor=False;a_done=b_done=step_id=observed=0;next_b=True;rho=None
    pages.admit('A');cursor.take('a.admitted','A',dict(reserved_pages_per_layer=pages.sizes['A']));cursor.take('work.started',details={})
    while True:
        if a_prefilled and a_done==config['arrival_after_a_decode'] and not anchor:
            cursor.take('anchor.reached','A',dict(after_a_decode=a_done,delivered_a_output_index=len(outputs['A'])-1,b_present=config['b_present']))
            if config['b_present']:
                cursor.take('b.requested','B',dict(after_a_decode=a_done));pages.admit('B')
                cursor.take('b.admitted','B',dict(reserved_pages_per_layer=pages.sizes['B']))
            anchor=True
        a_left=config['a_decode_steps']-a_done;b_left=config['b_prompt_length']-b_done if config['b_present'] else 0
        if a_prefilled and not a_left and not b_left: who,kind='done',None
        elif not a_prefilled: who,kind='A','a_prefill'
        elif anchor and config['b_present'] and b_left and (not a_left or next_b): who,kind='B','b_prefill'
        else: who,kind='A','a_decode'
        cursor.take('scheduler.boundary','scheduler',dict(choice=who,next_step_kind=kind,a_remaining_decode_steps=a_left,
                    b_remaining_prompt_tokens=b_left,b_arrived=anchor and config['b_present'],anchor_seen=anchor))
        if who=='done': break
        decision=None
        if who=='B':
            cursor.take('policy.decision_started','B',dict(step_id=step_id,policy=config['policy']))
            decision=decision_for(config['policy'],b_left,rho,observed,step_id)
            cursor.take('policy.decision_finished','B',decision)
        if kind=='a_prefill': offset=0;part=inputs['a_prompt_token_ids'];head=True
        elif kind=='a_decode': offset=config['a_prompt_length']+a_done;part=[outputs['A'][-1]];head=True
        else: offset=b_done;part=inputs['b_prompt_token_ids'][b_done:b_done+decision['selected_tokens']];head=len(part)==b_left
        step=dict(step_id=step_id,owner=who,kind=kind,offset=offset,input_tokens=len(part),head_evaluated=head,a_runnable_before_step=bool(a_left))
        if kind=='a_decode': step['decode_index']=a_done+1
        if who=='B' and not b_done: cursor.take('b.compute_started','B',{})
        start=cursor.take('step.started',who,dict(step,input_token_ids=part))
        if kind=='a_decode': cursor.take('a.decode_call_started','A',dict(step_id=step_id,decode_index=a_done+1))
        cursor.take('step.forward_returned',who,dict(step_id=step_id,head_graph_constructed=True))
        cursor.take('step.eval_started',who,dict(step_id=step_id));cursor.take('step.eval_returned',who,dict(step_id=step_id))
        pages.append(who,len(part))
        if kind=='a_decode': cursor.take('a.decode_call_completed','A',dict(step_id=step_id,decode_index=a_done+1))
        cursor.take('step.completed',who,step)
        if kind=='a_prefill': a_prefilled=True
        elif kind=='a_decode': a_done+=1;next_b=True
        else: b_done+=len(part);partition.append(len(part));next_b=False
        if head:
            output_index=len(outputs[who]);event=cursor.take(who.lower()+'.token_delivered',who)
            token=event['details'].get('token_id');need(integer(token),'invalid output token')
            need(exact(event['details'],dict(step_id=step_id,token_id=token,output_index=output_index,phase='decode' if kind=='a_decode' else 'prefill')),'token delivery details')
            outputs[who].append(token)
            if output_index==0: cursor.take(who.lower()+'.first_token',who,dict(token_id=token))
        label='after-step-'+str(step_id)
        e=cursor.take('snapshot.recorded',details=dict(label=label,snapshot_index=len(snapshots)))
        snapshots.append(pages.snapshot(label,e['seq']))
        if who=='A' and a_done==config['a_decode_steps'] or who=='B' and b_done==config['b_prompt_length']:
            cursor.take(who.lower()+'.completed',who,dict(output_tokens=len(outputs[who])))
            cursor.take(who.lower()+'.completion_observed',who,dict(evidence='all required explicit eval calls returned'))
            cursor.take('request.cleanup_started',who,dict(normal=True))
            pages.release(who)
            cursor.take('resource.released',who,dict(resource_kind='logical paged KV handles and reservation',normal=True))
        yielded=cursor.take('step.yield_ready',who,step)
        if who=='B':
            elapsed=yielded['t_ns']-start['t_ns'];need(elapsed>0,'nonpositive slice observation')
            sample=(elapsed+len(part)-1)//len(part);prior=rho
            if config['policy']=='cost-200': rho=sample if rho is None else (rho+sample+1)//2
            observed+=1
            cursor.take('policy.observed','B',dict(decision_id=decision['decision_id'],step_id=step_id,tokens=len(part),actual_ns=elapsed,
                sample_ns_per_token=sample,rho_before_ns_per_token=prior,rho_after_ns_per_token=rho,estimator_updated=config['policy']=='cost-200',
                completed_samples=observed,prediction_error_ns=None if decision['predicted_ns'] is None else elapsed-decision['predicted_ns'],
                budget_overshoot_ns=max(0,elapsed-BUDGET) if config['policy']=='cost-200' else None,head_evaluated=head,
                source_started_seq=start['seq'],source_yield_seq=yielded['seq']))
        step_id+=1
        need(step_id<=config['a_decode_steps']+config['b_prompt_length']+1,'unbounded trace')
    cursor.take('work.completed',details={});cursor.take('cleanup.synchronize_started',details={});cursor.take('cleanup.synchronize_completed',details={})
    e=cursor.take('snapshot.recorded',details=dict(label='final-drained',snapshot_index=len(snapshots)))
    snapshots.append(pages.snapshot('final-drained',e['seq']))
    cursor.take('trial.drained',details=dict(recovery=False));cursor.end()
    need(anchor and not pages.used and not pages.reserved and not pages.caches,'final lifecycle incomplete')
    need(exact(obs.get('snapshots'),snapshots),'actual page/cache/reservation snapshots differ from independent replay')
    need(exact(obs.get('output_token_ids'),outputs) and outputs['A']==refs['A']['output_token_ids'],'A actual/reference outputs differ')
    need(len(outputs['A'])==config['a_decode_steps']+1 and len(outputs['B'])==int(config['b_present']),'output count differs')
    need(exact(obs.get('partition'),partition) and obs.get('partition_sha256')==object_sha(partition),'partition evidence differs')
    need(exact(obs.get('estimator_final_rho_ns_per_token'),rho),'final estimator differs')
    expected=metrics_recomputed(cursor.events,config,snapshots)
    need(numeric_equal(trial.get('metrics'),expected),'metric differs from original event endpoints/work')
    wanted_checks=CHECKS|({'B_actual_matches_actual_partition_reference','B_partition_reference_matches_whole'} if config['b_present'] else set())
    checks=trial.get('checks',[])
    need(len(checks)==len(wanted_checks) and {c.get('name') for c in checks}==wanted_checks and all(c.get('passed') is True and not c.get('error') for c in checks),'raw check missing/failed/duplicated')
    if not config['b_present']:
        need(exact(trial.get('reference_validation'),dict(status='not-applicable-no-B')),'no-B reference must be absent')
    return dict(trial_id=trial_id,warmup=warmup,passed=True,metrics=expected,partition=partition,
                partition_sha256=object_sha(partition),step_count=step_id,snapshot_count=len(snapshots))


def inside(record,begin,end,process_origin):
    need(integer(record.get('clock_origin_ns'),1),'missing nested clock origin')
    origin=record['clock_origin_ns']
    need(process_origin+begin['t_ns']<=origin and origin+record['events'][-1]['t_ns']<=process_origin+end['t_ns'],
         'nested trace outside process begin/return')
    if 'elapsed_ns' in record: need(origin+record['elapsed_ns']<=process_origin+end['t_ns'],'reference elapsed outside process return')


def analyze_document(document):
    need(exact(document.get('schema_version'),1) and document.get('experiment')=='tiny-decode-cost','unsupported report')
    need(document.get('passed') is True and not document.get('error'),'top-level failure/incomplete')
    config=document['config'];config_checked(config)
    inputs=document['inputs']
    need(tokens(inputs.get('a_prompt_token_ids'),config['a_prompt_length']) and tokens(inputs.get('b_prompt_token_ids'),config['b_prompt_length']),'input lengths/types')
    need(inputs.get('a_prompt_sha256')==object_sha(inputs['a_prompt_token_ids']) and inputs.get('b_prompt_sha256')==object_sha(inputs['b_prompt_token_ids']),'input digest differs')
    setup=document['setup'];need(not setup.get('error') and not setup.get('cleanup_error') and integer(setup.get('clock_origin_ns'),1),'setup failure/clock')
    c=Cursor(setup['events']);c.take('model.load_started',details={});c.take('model.load_returned',details={})
    model=c.take('model.constructed');layers=model['details'].get('layers')
    need(integer(layers,1,1024) and exact(model['details'],dict(layers=layers)),'layer count');c.end()
    refs=document['reference'];need(set(refs)=={'A','B-whole'},'setup references missing/extra')
    need(refs['A'].get('reference_id')=='A' and refs['B-whole'].get('reference_id')=='B-whole','reference identity')
    page_bytes=reference_checked(refs['A'],inputs['a_prompt_token_ids'],[config['a_prompt_length']],config['a_decode_steps'],layers)
    reference_checked(refs['B-whole'],inputs['b_prompt_token_ids'],[config['b_prompt_length']],0,layers,page_bytes)
    need(integer(document.get('process_clock_origin_ns'),1),'process clock missing')
    origin=document['process_clock_origin_ns'];process=Cursor(document['process_events'])
    need(setup['clock_origin_ns']+setup['events'][-1]['t_ns']<=origin,'setup/process order')
    for name in ('A','B-whole'):
        begin=process.take('reference.started',details=dict(reference_id=name))
        end=process.take('reference.returned',details=dict(reference_id=name,passed=True))
        inside(refs[name],begin,end,origin)
    results=[];all_trials=[]
    for group,warming,count in [('warmups',True,config['warmup']),('trials',False,config['trials'])]:
        need(isinstance(document.get(group),list) and len(document[group])==count,'missing/extra '+group)
        for index,trial in enumerate(document[group]):
            trial_id=-(index+1) if warming else index
            begin=process.take('trial.started',details=dict(trial_id=trial_id,warmup=warming))
            result=trial_checked(trial,config,inputs,refs,layers,page_bytes,trial_id,warming)
            end=process.take('trial.returned',details=dict(trial_id=trial_id,warmup=warming,execution_passed=True))
            inside(trial,begin,end,origin);results.append(result);all_trials.append(trial)
    partitions=document.get('partition_references');need(isinstance(partitions,list),'partition references absent')
    cache={};new_count=0
    if config['b_present']:
        for trial in all_trials:
            partition=trial['observations']['partition'];key=object_sha(partition)
            begin=process.take('partition_validation.started',details=dict(trial_id=trial['trial_id'],warmup=trial['warmup']))
            reused=key in cache
            if not reused:
                need(new_count<len(partitions),'missing actual partition reference')
                ref=partitions[new_count]
                need(ref.get('reference_id')=='partition-'+str(new_count) and ref.get('input_token_ids_sha256')==object_sha(inputs['b_prompt_token_ids']),'partition reference identity/input binding')
                reference_checked(ref,inputs['b_prompt_token_ids'],partition,0,layers,page_bytes)
                cache[key]=ref;new_count+=1
            ref=cache[key]
            need(trial['observations']['output_token_ids']['B']==ref['output_token_ids'],'actual B differs from isolated actual partition')
            need(ref['output_token_ids']==refs['B-whole']['output_token_ids'],'isolated partition differs from whole reference')
            end=process.take('partition_validation.finished',details=dict(trial_id=trial['trial_id'],warmup=trial['warmup'],reference_id=ref['reference_id'],reused=reused,passed=True))
            if not reused: inside(ref,begin,end,origin)
            work_end=next(e['t_ns'] for e in trial['events'] if e['name']=='work.completed')
            expected=dict(status='passed',reference_id=ref['reference_id'],partition_sha256=key,reused=reused,
                process_started_seq=begin['seq'],process_completed_seq=end['seq'],validation_ms=(end['t_ns']-begin['t_ns'])/1e6,
                completion_delay_after_work_ms=(origin+end['t_ns']-trial['clock_origin_ns']-work_end)/1e6)
            need(numeric_equal(trial.get('reference_validation'),expected),'reference replay timing/identity differs')
    need(len(partitions)==new_count,'extra/missing isolated partition replay')
    process.end()
    return dict(passed=True,total_trials=config['trials'],policy=config['policy'],layers=layers,page_bytes=page_bytes,
                warmups=[r for r in results if r['warmup']],trials=[r for r in results if not r['warmup']],
                numerical_scope='Every actual token compared to separately recorded isolated A and actual-partition B; partition B also compared to whole B. No model execution/whole-KV byte oracle.',
                timing_scope='Host event observations; event ordering, work, causal integer estimator and three interval sequences independently reconstructed. No GPU preemption, deadline, or performance ranking claim.')


def attempt_audit(root,external_full_hashes=False):
    root=Path(root);manifest_path=root/'manifest.json';payload=root/'payload'
    manifest=json.loads(manifest_path.read_text());prepared=json.loads((root/'prepared.json').read_text());digest=sha(manifest_path)
    need(manifest.get('kind')=='tiny-decode-cost-attempt' and exact(manifest.get('schema_version'),1),'attempt schema')
    need(prepared.get('manifest_sha256')==digest and prepared.get('model_or_gpu_loaded') is False,'prepared identity differs')
    files=manifest['payload_files'];need(integer(prepared.get('payload_files'),1) and prepared['payload_files']==len(files),'payload count')
    actual={str(p.relative_to(payload)) for p in payload.rglob('*') if p.is_file()}
    need(actual==set(files),'payload inventory changed')
    required={'experiments/tiny-decode-cost/'+name for name in ('run.py','protocol.py','attempt.py','generated_runtime.py','fixture.json','README.md','test_protocol.py','test_runner.py','test_attempt.py','test_generated_runtime.py')}
    required|={'experiments/tiny-interleave/run.py','experiments/tiny-interleave/protocol.py','scripts/run_recorded.py',
        'tiny-source/src/tiny_llm_ref/qwen3_week3.py','tiny-source/src/tiny_llm_ref/paged_kv_cache.py',
        'tiny-source/src/extensions_ref/src/paged_attention.cpp','tiny-source/src/extensions_ref/src/paged_attention.metal','model-metadata/config.json'}
    need(required<=set(files) and any(k.startswith('tiny-source/src/extensions_ref/tiny_llm_ext_ref/_ext.') and k.endswith('.so') for k in files),'required dependency missing')
    for relative,row in files.items():
        path=payload/relative
        need(not Path(relative).is_absolute() and '..' not in Path(relative).parts and not path.is_symlink() and path.resolve().is_relative_to(payload.resolve()),'unsafe payload path')
        need(integer(row.get('bytes')) and path.stat().st_size==row['bytes'] and sha(path)==row.get('sha256'),'payload bytes differ: '+relative)
    need(files['experiments/tiny-interleave/run.py']['sha256']==HELPER and files['experiments/tiny-interleave/protocol.py']['sha256']==HELPER_PROTOCOL,'frozen helper differs')
    external=manifest['external_dependencies'];runtime=manifest['runtime']
    need(isinstance(external,list) and external and len({r['path'] for r in external})==len(external),'external inventory duplicate/empty')
    need({r['path'] for r in external if r['kind']=='runtime'}==set(runtime['files']),'runtime file list differs')
    for row in external:
        need(row.get('kind') in ('runtime','model-weight','model-metadata') and integer(row.get('bytes')) and isinstance(row.get('sha256'),str)
             and len(row['sha256'])==64 and all(c in '0123456789abcdef' for c in row['sha256']),'invalid external file identity')
        if row['kind']!='model-weight': need(row.get('preparation_hash_verified') is True,'metadata/runtime not hashed during capture')
        if row['kind']=='model-metadata':
            name='model-metadata/'+Path(row['path']).name
            need(name in files and files[name]['sha256']==row['sha256'] and files[name]['bytes']==row['bytes'],'model metadata copy differs')
        if external_full_hashes:
            path=Path(row['path']);need(path.stat().st_size==row['bytes'] and sha(path)==row['sha256'],'external dependency bytes differ: '+str(path))
    weights=[r for r in external if r['kind']=='model-weight'];need(weights,'model weights missing')
    generated=manifest.get('generated_runtime_templates')
    need(isinstance(generated,list) and len(generated)==1,'exactly one registered generated template required')
    entry=generated[0]
    need(set(entry)=={'module_name','basename','directory_attribute','derivation','expected_payload','sha256','bytes','generator','template'},'generated declaration schema')
    need(entry['module_name']==GENERATED_MODULE and entry['basename']==GENERATED_MODULE+'.py'
         and entry['directory_attribute']=='INSTANTIATED_TEMPLATE_DIR_PATH' and entry['derivation']=='torch-non-scriptable-constants-v1'
         and entry['expected_payload']=='generated-runtime/expected/'+GENERATED_MODULE+'.py','unrecognized generated template identity')
    indexed_external={r['path']:r for r in external};source_text={}
    for role,module,suffix in [('generator','torch.distributed.nn.jit.instantiator','/torch/distributed/nn/jit/instantiator.py'),
                              ('template','torch.distributed.nn.jit.templates.remote_module_template','/torch/distributed/nn/jit/templates/remote_module_template.py')]:
        row=entry[role];relative='generated-runtime/sources/'+role+'.py'
        need(set(row)=={'module_name','path','payload','sha256','bytes'} and row['module_name']==module and row['payload']==relative
             and row['path'].endswith(suffix),'generated source identity differs')
        need(relative in files and row['path'] in indexed_external,'generated source was not captured')
        ext=indexed_external[row['path']]
        need(ext['kind']=='runtime' and ext['sha256']==files[relative]['sha256']==row['sha256']
             and exact(ext['bytes'],row['bytes']) and exact(files[relative]['bytes'],row['bytes']),'generated source not bound to runtime and payload bytes')
        source_text[role]=(payload/relative).read_text()
    rebuilt=torch_generated_source(source_text['generator'],source_text['template'])
    relative=entry['expected_payload'];need(relative in files,'expected generated source not preserved')
    need((payload/relative).read_bytes()==rebuilt and entry['sha256']==files[relative]['sha256']==hashlib.sha256(rebuilt).hexdigest()
         and exact(entry['bytes'],len(rebuilt)) and exact(files[relative]['bytes'],len(rebuilt)),'generated bytes differ from independent literal reconstruction')
    if external_full_hashes:
        need({str(p) for p in Path(manifest['run_spec']['model']).iterdir() if p.is_file()}=={r['path'] for r in external if r['kind'].startswith('model-')},'model inventory differs')
    return manifest,dict(passed=True,manifest_sha256=digest,payload_files=len(files),external_dependencies=len(external),
        payload_full_hashes_verified=True,external_full_hashes_verified=external_full_hashes,external_bytes=sum(r['bytes'] for r in external),
        generated_template=dict(module_name=GENERATED_MODULE,sha256=entry['sha256'],bytes=entry['bytes'],independent_ast_reconstruction=True))


def reference_paths_checked(record,origin_root,original_payload,cwd,files):
    """Replay sys.path decisions against the recorded child cwd, not ours."""
    need(isinstance(record,dict) and set(record)=={'passed','before','removed','after','preloaded','resolutions','scope'},'reference search-path schema')
    need(record['passed'] is True and exact(record['preloaded'],[]) and record['scope']=='top-level spec discovery only; no reference source execution',
         'reference path binding failed/preloaded')
    before=record['before'];need(isinstance(before,list) and all(type(entry) is str for entry in before),'invalid recorded sys.path')
    original=Path(origin_root).resolve();original_src=original/'src';payload=Path(original_payload)
    frozen=payload/'tiny-source';prefix=[str(frozen/'src'),str(frozen)];kept=[];removed=[]
    for entry in before:
        path=Path(entry)
        path=(path if path.is_absolute() else Path(cwd)/path).resolve()
        if path==original or path.is_relative_to(original_src):
            removed.append(dict(entry=entry,resolved=str(path),reason='original reference root or source tree'))
        elif str(path) not in prefix: kept.append(entry)
    need(exact(record['removed'],removed) and exact(record['after'],prefix+kept),'reference sys.path filtering/prefix differs; cwd must be the actual child cwd')
    wanted={
        'model_names':dict(origin=str(frozen/'model_names.py'),namespace_paths=[],sha256=files['tiny-source/model_names.py']['sha256']),
        'tiny_llm_ref':dict(origin=str(frozen/'src/tiny_llm_ref/__init__.py'),namespace_paths=[str(frozen/'src/tiny_llm_ref')],
                           sha256=files['tiny-source/src/tiny_llm_ref/__init__.py']['sha256']),
        'extensions_ref':dict(origin=None,namespace_paths=[str(frozen/'src/extensions_ref')])}
    need(exact(record['resolutions'],wanted),'reference entrypoint bytes or unique namespace resolution differ')
    return dict(passed=True,removed_entries=len(removed),before_entries=len(before),after_entries=len(prefix+kept),
                scope='Recorded top-level spec/path isolation plus separate actual module-byte bindings; no reference source re-execution')


def artifact_audit(raw_path,attempt_dir=None,external_full_hashes=False):
    raw_path=Path(raw_path);root=Path(attempt_dir) if attempt_dir else raw_path.parent.parent
    doc=json.loads(raw_path.read_text());manifest,result=attempt_audit(root,external_full_hashes)
    files=manifest['payload_files'];config=doc['config'];provenance=doc['provenance'];spec=manifest['run_spec'];runtime=manifest['runtime']
    need(set(spec)==set(SPEC_KEYS)|{'model'} and all(exact(config.get(k),spec[k]) for k in SPEC_KEYS),'run spec differs')
    run_path=raw_path.parent/'run.json';run=json.loads(run_path.read_text());command=run.get('command')
    need(isinstance(command,list) and all(isinstance(x,str) for x in command) and len(command)>=3,'recorded argv missing')
    need(command.count('--attempt-manifest')==1,'manifest argument missing/duplicate')
    original_manifest=Path(command[command.index('--attempt-manifest')+1]);original_payload=original_manifest.parent/'payload'
    expected=[runtime['executable'],str(original_payload/'experiments/tiny-decode-cost/run.py')]
    expected += [value for key in SPEC_KEYS for value in ('--'+key.replace('_','-'),str(spec[key]))]
    expected += ['--model',spec['model'],'--tiny-root',str(original_payload/'tiny-source'),'--attempt-manifest',str(original_manifest),
                 '--snapshot-sha256',result['manifest_sha256'],'--json']
    need(command==expected and run.get('cwd')==str(original_payload/'experiments/tiny-decode-cost'),'actual argv/cwd differs from frozen spec')
    path_isolation=reference_paths_checked(provenance.get('reference_search_path'),manifest['reference_origin']['root'],original_payload,run['cwd'],files)
    need(raw_path.name=='stdout.json' and type(run.get('returncode')) is int and run['returncode']==0 and run.get('timed_out') is False,'failed/timed-out process')
    for field in ('elapsed_seconds','timeout_seconds'):
        need(type(run.get(field)) in (int,float) and math.isfinite(run[field]) and run[field]>0,'invalid recorded '+field)
    need(set(run.get('files',{}))=={'stdout.json','stderr.log'},'missing recorder hashes')
    for name in ('stdout.json','stderr.log'):
        actual=raw_path.parent/name;need(exact(run['files'][name],dict(bytes=actual.stat().st_size,sha256=sha(actual))),'raw log hash/size differs')
    need(provenance.get('manifest_sha256')==result['manifest_sha256'],'raw manifest identity differs')
    for field,relative in {'harness_sha256':'experiments/tiny-decode-cost/run.py','protocol_sha256':'experiments/tiny-decode-cost/protocol.py',
        'fixture_sha256':'experiments/tiny-decode-cost/fixture.json','helper_sha256':'experiments/tiny-interleave/run.py','helper_protocol_sha256':'experiments/tiny-interleave/protocol.py'}.items():
        need(provenance.get(field)==files[relative]['sha256'],'source provenance differs: '+field)
    pre=provenance.get('pre_execution_binding',{});post=provenance.get('post_execution_binding',{})
    for binding in (pre,post):
        need(binding.get('passed') is True and binding.get('external_full_bytes_checked') is True and integer(binding.get('elapsed_ns'),1) and not binding.get('error'),'missing/failed pre/post full external binding')
    external={r['path']:r for r in manifest['external_dependencies']}
    before=provenance.get('imported_binding_before_load',{});after=post.get('imported_binding',{})
    required={'mlx.core','mlx_lm','tiny_llm_ref.qwen3_week3','tiny_llm_ref.paged_kv_cache'}
    for bindings in (before,after):
        need(required<=set(bindings) and GENERATED_MODULE in bindings and any(name.endswith('._ext') for name in bindings),'actual core/native/generated imports missing')
        for name,row in bindings.items():
            if row.get('kind')=='generated-runtime-template':
                entry=manifest['generated_runtime_templates'][0]
                need(name==GENERATED_MODULE,'arbitrary generated module is not registered')
                directory=row.get('generation_directory');address=row.get('path')
                need(isinstance(directory,str) and isinstance(address,str) and Path(directory).is_absolute()
                     and Path(address)==Path(directory)/entry['basename'] and '..' not in Path(directory).parts,'generated runtime path not exact generator directory/basename')
                expected=dict(kind='generated-runtime-template',module_name=name,path=address,sha256=entry['sha256'],bytes=entry['bytes'],
                    expected_payload=entry['expected_payload'],generation_directory=directory,directory_attribute=entry['directory_attribute'],
                    derivation=entry['derivation'],generator=entry['generator'],template=entry['template'])
                need(exact(row,expected),'actual generated source binding differs from independently reconstructed manifest')
                for role in ('generator','template'):
                    source=entry[role]
                    need(exact(bindings.get(source['module_name']),dict(path=source['path'],sha256=source['sha256'],kind='external-runtime')),
                         'actual generator/template module was not loaded from its bound runtime file')
                continue
            need(set(row)=={'path','sha256','kind'},'import binding fields')
            path=Path(row['path']);tiny=name=='model_names' or name.startswith(('tiny_llm_ref','extensions_ref'))
            if tiny: need(path.is_relative_to(original_payload/'tiny-source'),'tiny import escaped snapshot')
            if row['kind']=='copied-payload':
                need(path.is_relative_to(original_payload),'copied import escaped payload');relative=str(path.relative_to(original_payload))
                need(relative in files and row['sha256']==files[relative]['sha256'],'actual copied import hash differs')
            elif row['kind']=='external-runtime':
                need(row['path'] in external and external[row['path']]['kind']=='runtime' and row['sha256']==external[row['path']]['sha256'],'actual runtime import unbound')
            else: need(False,'unknown import source kind')
    need(all(name in after and exact(row,after[name]) for name,row in before.items()),'preloaded import disappeared/changed')
    fixture_path=root/'payload/experiments/tiny-decode-cost/fixture.json';fixture=json.loads(fixture_path.read_text())
    need(doc['inputs'].get('fixture_sha256')==sha(fixture_path),'actual fixture hash differs')
    need(doc['inputs']['a_prompt_token_ids']==fixture['a_token_ids'][:config['a_prompt_length']]
         and doc['inputs']['b_prompt_token_ids']==fixture['b_token_ids'][:config['b_prompt_length']],'inputs differ from frozen literal fixture')
    model=json.loads((root/'payload/model-metadata/config.json').read_text())
    layers=doc['setup']['events'][-1]['details']['layers'];need(layers==model['num_hidden_layers'],'model layer geometry differs')
    all_tokens=list(doc['inputs']['a_prompt_token_ids'])+doc['inputs']['b_prompt_token_ids']
    all_tokens += sum((ref['output_token_ids'] for ref in list(doc['reference'].values())+doc['partition_references']),[])
    need(all(integer(t,0,model['vocab_size']-1) for t in all_tokens),'token outside bound vocabulary')
    pool=doc['reference']['A']['final_pool']
    bytes_per_element=pool['backing_bytes']/(pool['backing_capacity_pages']*2*PAGE*model['num_key_value_heads']*model['head_dim'])
    need(bytes_per_element in (2,4),'KV storage differs from supported bound model/page geometry')
    return dict(result,run_sha256=sha(run_path),stdout_sha256=sha(raw_path),stderr_sha256=sha(raw_path.parent/'stderr.log'),
        imported_files_before=len(before),imported_files_after=len(after),kv_element_bytes=int(bytes_per_element),
        reference_path_isolation=path_isolation,
        generated_temporary_file_evidence='Actual temporary bytes/path checked during pre/post execution; independently rebuilt expected payload checked now. Temporary runtime file is not reread after process exit.',
        external_evidence='Independent full bytes verified now' if external_full_hashes else 'Manifest plus pre/post execution attestations; external bytes not independently reread by this invocation',
        source_scope='Byte consistency, actual argv and import bindings; not signed authenticity or a hermetic operating-system image')


def clone_derivation_checked(root,manifest):
    """Check archived parent bytes and the exact allowed clone transformation."""
    if 'batch_derivation' not in manifest: return dict(kind='original-prepared-attempt')
    batch=Path(root).parent;derivation=manifest['batch_derivation']
    plan_path=batch/'plan.json';plan=json.loads(plan_path.read_text());plan_sha=sha(plan_path)
    original_path=batch/'template-manifest.json';original=json.loads(original_path.read_text());original_sha=sha(original_path)
    prepared_path=batch/'template-prepared.json';prepared=json.loads(prepared_path.read_text());template=plan['template']
    need(exact(plan.get('schema_version'),1) and plan.get('kind')=='tiny-decode-cost-batch','batch plan schema')
    need(template.get('archived_manifest')=='template-manifest.json' and template.get('archived_prepared')=='template-prepared.json'
         and template.get('manifest_sha256')==original_sha==prepared.get('manifest_sha256')
         and template.get('prepared_sha256')==sha(prepared_path) and template.get('captured_at')==original['created_at'],
         'archived parent manifest/prepared identity differs')
    need(prepared.get('model_or_gpu_loaded') is False and plan.get('model_or_gpu_loaded') is False
         and plan.get('external_full_hashes_checked_at_batch_preparation') is False,'clone falsely claims model/external execution')
    need(plan.get('prepare_driver_sha256')==sha(batch/'prepare-driver.py')=='bf024bbfab8f5929dd19befdb5ef0c3a09357fe56712593db4bff9f9d1a00827',
         'clone preparation driver differs from reviewed bytes')
    if plan.get('mode')=='smoke':
        cells=[dict(cell_id=p,block=0,design_row=None,position=i,policy=p,trials=1,warmup=0) for i,p in enumerate(POLICIES)]
        need(plan.get('schedule_seed') is None,'smoke has no shuffled schedule')
    else:
        need(plan.get('mode')=='matrix' and exact(plan.get('schedule_seed'),20261029),'unsupported batch schedule')
        rows=list(range(6));random.Random(20261029).shuffle(rows)
        cells=[dict(cell_id=f'block-{b:02d}-position-{position:02d}-{POLICIES[(n+r)%6]}',block=b,design_row=r,
                    position=position,policy=POLICIES[(n+r)%6],trials=1,warmup=1)
               for b,r in enumerate(rows) for position,n in enumerate((0,1,5,2,4,3))]
    need(exact(plan.get('cells'),cells),'batch order is not the frozen six-policy/Williams design')
    cell=derivation.get('cell');need(sum(exact(cell,c) for c in cells)==1,'clone cell not uniquely in plan')
    shape=plan['shape'];need(set(shape)=={'a_prompt_length','b_prompt_length','a_decode_steps','arrival_after_a_decode'},'batch shape schema')
    expected=copy.deepcopy(original);expected['created_at']=manifest['created_at']
    expected['run_spec']=dict(shape,policy=cell['policy'],trials=cell['trials'],warmup=cell['warmup'],model=original['run_spec']['model'])
    expected['batch_derivation']=dict(template_manifest_sha256=original_sha,template_path=template['path'],original_capture_time=original['created_at'],
        plan_sha256=plan_sha,cell=cell,external_bytes_rehashed_during_clone=False)
    expected['scope'].append('This attempt copies an identified frozen template. External capture rows are inherited; execution still verifies full external bytes before and after.')
    need(exact(manifest,expected),'clone changed fields beyond spec/time/scope and declared derivation')
    index=json.loads((batch/'prepared-index.json').read_text())
    need(index.get('passed') is True and index.get('plan_sha256')==plan_sha and index.get('template_verified_before_and_after') is True
         and index.get('model_or_gpu_loaded') is False and index.get('external_full_hashes_checked') is False,'batch preparation did not finish successfully')
    indexed=index['cells'];need(len(indexed)==len(cells),'prepared index omitted/added cell')
    found=None;parent=None
    for wanted,row in zip(cells,indexed):
        need(exact({k:row.get(k) for k in wanted},wanted) and row.get('execution_exists') is False,'prepared index order/initial state differs')
        path=Path(row['path']);need(path.is_absolute() and path.name==wanted['cell_id'],'prepared cell path differs')
        parent=path.parent if parent is None else parent;need(path.parent==parent,'prepared cells have different batch parents')
        need(row.get('execute_argv')==[original['runtime']['executable'],'-B',str(path/'payload/experiments/tiny-decode-cost/attempt.py'),
             'execute','--attempt',str(path),'--timeout','300'],'prepared execute command differs')
        if wanted['cell_id']==cell['cell_id']:
            need(row['manifest_sha256']==sha(Path(root)/'manifest.json'),'selected clone manifest differs from prepared index')
            found=row
    need(found is not None,'selected clone omitted from index')
    return dict(kind='verified-batch-clone',plan_sha256=plan_sha,template_manifest_sha256=original_sha,
                archived_template_bytes_verified=True,cell=cell,original_manifest_path=str(Path(found['path'])/'manifest.json'),
                external_identity_inherited=True,external_rehash_during_preparation=False)


def analyze_attempt_set(map_path,external_full_hashes=False):
    map_path=Path(map_path);mapping=json.loads(map_path.read_text())
    need(isinstance(mapping,dict) and set(mapping)==set(POLICIES) and all(isinstance(p,str) for p in mapping.values()),'exactly six policy-to-attempt paths required')
    roots={p:(Path(mapping[p]) if Path(mapping[p]).is_absolute() else map_path.parent/mapping[p]).resolve() for p in POLICIES}
    need(len(set(roots.values()))==6,'one attempt cannot count as two policies')
    documents=[];common=None;common_reference=None
    for policy,root in roots.items():
        raw=root/'execution/stdout.json';doc=json.loads(raw.read_text());result=analyze_document(doc)
        need(doc['config']['policy']==policy,'mapped policy differs from actual run')
        result['artifacts']=artifact_audit(raw,root,external_full_hashes)
        manifest=json.loads((root/'manifest.json').read_text())
        result['derivation']=clone_derivation_checked(root,manifest)
        if result['derivation']['kind']=='verified-batch-clone':
            run=json.loads((root/'execution/run.json').read_text());argv=run['command']
            need(argv[argv.index('--attempt-manifest')+1]==result['derivation']['original_manifest_path'],'actual clone argv differs from selected prepared cell')
        identity={key:manifest[key] for key in ('payload_files','runtime','external_dependencies','generated_runtime_templates','reference_origin')}
        identity['model']=manifest['run_spec']['model'];identity['inputs']=doc['inputs']
        identity['shape']={k:v for k,v in doc['config'].items() if k not in ('policy','b_present')}
        reference={k:doc['reference'][k]['output_token_ids'] for k in ('A','B-whole')}
        if common is None: common,common_reference=identity,reference
        else:
            need(exact(identity,common),'six conditions differ in complete source/runtime/model/input/shape identities')
            need(exact(reference,common_reference),'six conditions have different isolated A/whole-B numerical targets')
        documents.append(dict(result,policy=policy,source=str(raw),raw_sha256=sha(raw)))
    return dict(passed=True,total_trials=sum(r['total_trials'] for r in documents),documents=documents,
        attempt_map_sha256=sha(map_path),mapped_attempts={k:str(v) for k,v in roots.items()},
        common=dict(payload_files=len(common['payload_files']),payload_identity_sha256=object_sha(common['payload_files']),
                    external_dependencies=len(common['external_dependencies']),external_identity_sha256=object_sha(common['external_dependencies']),
                    runtime_identity_sha256=object_sha(common['runtime']),fixture_sha256=common['inputs']['fixture_sha256'],
                    all_literal_inputs_and_isolated_targets_equal=True),
        sample_scope='Only the six explicitly mapped executed attempts count. Prepared/unexecuted sibling cells are not samples; no performance ranking.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    selection=parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--attempt',type=Path)
    selection.add_argument('--attempt-map',type=Path)
    parser.add_argument('--raw',type=Path)
    parser.add_argument('--external-full-hashes',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();raw=args.raw or (args.attempt/'execution/stdout.json' if args.attempt else args.attempt_map)
    try:
        if args.attempt_map:
            need(args.raw is None,'--raw cannot override a six-attempt map')
            result=analyze_attempt_set(args.attempt_map,args.external_full_hashes)
        else:
            result=analyze_document(json.loads(raw.read_text()))
            result['artifacts']=artifact_audit(raw,args.attempt,args.external_full_hashes)
    except (ValueError,KeyError,TypeError,IndexError,OSError,ZeroDivisionError,AttributeError,SyntaxError) as error:
        result=dict(passed=False,total_trials=0,errors=[str(error)])
    result.update(source=str(raw),raw_sha256=sha(raw) if raw.is_file() else None,checker_sha256=sha(__file__),
        external_full_hashes_requested=args.external_full_hashes,
        external_full_hashes_verified=(result.get('artifacts',{}).get('external_full_hashes_verified') is True
            or bool(result.get('documents')) and all(r['artifacts']['external_full_hashes_verified'] is True for r in result['documents'])))
    encoded=json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False)+'\n'
    if args.output:
        with args.output.open('x') as stream: stream.write(encoded)
    else: print(encoded,end='')
    return 0 if result['passed'] else 1


if __name__=='__main__': raise SystemExit(main())
