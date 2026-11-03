"""Source-directed finite lambda/body checker, not an evaluator or GPU test."""
from dataclasses import dataclass, replace
import hashlib
import json
from p8static import parser as old
from p8static.syntax import Name
from p8foundation.model import Free, Binder, RNode, Decision, FoundationError, node, require, serialize
from p8foundation.resolve import Resolver
from p8foundation.algebra import free_names, instantiate
from p8closure.schema import Schema, canonical, compare_types, validate_links, effect_parts
from p8closure.projections import parse_projections, validate_projections
from p8closure.support import ClosureContext, Caller, copyable, stable, effect_includes, support_of
from p8closure.qualified import capture_support
from p8closure.requirements import (Unsupported, SymbolicCaller, ProofContext, caller_key, ktrue,
    derive, minimum, use, prove, holding, require_proof, close_refs, kall)
from p8closure.formation import form_world, form_type, form_effect, one
from .source_schema import validate_document
from .binding import free_terms
from .inference import infer_bottom_type, NeedsBindType


def empty_effect(): return node("effect", "set", atoms=())
def effect(*atoms): return node("effect", "set", atoms=tuple(atoms))
def union(*effects):
    result = empty_effect()
    for e in effects:
        if e is not None and canonical(e) != canonical(empty_effect()):
            result = e if canonical(result) == canonical(empty_effect()) else node("effect", "union", left=result, right=e)
    return result
def base(tag): return node("type", tag)


@dataclass(frozen=True)
class Value:
    type: object
    view: object
    hold: object
    implementation: object = None

    def record(self):
        return node("value", "checked", type=self.type, view=self.view, hold=self.hold, implementation=self.implementation)


@dataclass
class State:
    names: dict
    slots: dict
    pc: ProofContext
    origins: tuple = ()
    source_names: object = None
    generations: tuple = ()
    execution: str = "ordinary"

    def clone(self):
        return State(dict(self.names),dict(self.slots),self.pc,self.origins,self.source_names,self.generations,self.execution)
    def snapshot(self):
        return tuple(node("slot", "remaining", binder=k, value=v.record()) for k,v in sorted(self.slots.items(), key=lambda kv:repr(kv[0]))
                     if not copyable(v.type))
    def affine(self): return frozenset(k for k,v in self.slots.items() if not copyable(v.type))


@dataclass(frozen=True)
class Normal:
    value: Value
    state: State
    drops: tuple = ()
    frames: tuple = ()
    def record(self):
        return node("exit", "normal", value=self.value.record(), remaining=self.state.snapshot(), drops=self.drops, frames=self.frames)


@dataclass(frozen=True)
class Exceptional:
    state: State
    origin: object
    abandoned: tuple = ()
    frames: tuple = ()
    def record(self):
        return node("exit", "exceptional", origin=self.origin, abandoned=self.abandoned,
                    remaining=self.state.snapshot(), frames=self.frames)


@dataclass(frozen=True)
class Body:
    type: object
    effect: object
    normal: tuple
    exceptional: tuple
    derivation: object


@dataclass(frozen=True)
class SourceResult:
    verdict: str
    derivation: object = None
    diagnostic: object = None
    premises: tuple = ()


class Checker:
    def __init__(self, digest, sigma=None):
        from .sigma import fixed_sigma,check_sigma
        self.digest = digest
        self.path = ("entry",)
        self.premises = []
        self.sigma=check_sigma(fixed_sigma() if sigma is None else sigma)
        self.annotation_stack=[]

    def site(self, path): return (self.digest,) + tuple(path)
    def source_resolver(self,state):
        return Resolver(state.pc.world.names if state.source_names is None else state.source_names)
    def schema(self, state): return Schema(self.source_resolver(state))
    def type(self, raw, state):
        t = self.schema(state).type(raw); form_type(t, state.pc.world); return t
    def same(self, a, b, state, message="type mismatch"):
        require(compare_types(a,b,state.pc.world).yes, message)
    def reshold(self, t, view, state, rule, premises=()):
        return node("proof", "ResHold", rule=rule, caller=caller_key(state.pc.caller), type=t, view=view, premises=tuple(premises))
    def made(self, t, view, state, rule, premises=(), implementation=None):
        return Value(t,view,self.reshold(t,view,state,rule,premises),implementation)
    def check_hold(self, value, state):
        require(value.hold.get("caller") == caller_key(state.pc.caller), "ResHold caller mismatch")
        require(value.hold.get("view") == value.view and value.hold.get("type") == value.type, "ResHold value correspondence")
    def nominal_origins(self,value,state):
        refs=free_names(value)
        return tuple(node("origin","nominal",reference=ref,opening=opening,slot=slot)
                     for ref,opening,slot in state.origins if ref in refs)
    def generation_origins(self,value,state):
        refs=free_names(value)
        return tuple(node("origin","generated",reference=ref,generation=g,slot=slot)
                     for ref,g,slot in state.generations if ref in refs)
    def bind(self, state, name, value, path, role="term"):
        binder = self.site(path) + (role,)
        require(binder not in state.slots, "duplicate term binder site")
        prior = state.names.get(name)
        state.names[name] = binder; state.slots[binder] = value
        from .annotations import bound
        bound(self,state,name,binder,value)
        return binder, prior

    def point_requirements(self, t, world):
        # Specific opened/result values justify atomic facts at these exact
        # references. Never lift a hidden package ResHold into forall, and do
        # not assume the inactive Sum arm exists. No facts are added to Psi.
        if isinstance(t,Binder) or t.tag=="Sum" or t.tag=="Shared": return ktrue()
        if t.tag=="Pair": return kall(self.point_requirements(t.get(k),world) for k in ("left","right"))
        def atoms(s):
            if isinstance(s,Binder): return ktrue()
            if s.tag=="all": return kall(atoms(x) for x in s.get("items"))
            return use(s,world)
        return atoms(support_of(t))
    def close_binding(self, result, name, binder, prior):
        normals, exceptional = [], []
        def close(state):
            state = state.clone(); dropped = ()
            if binder in state.slots:
                value = state.slots.pop(binder)
                if not copyable(value.type): dropped = (node("drop", "local", binder=binder, view=value.view),)
            if prior is None: state.names.pop(name, None)
            else: state.names[name] = prior
            return state,dropped
        for n in result.normal:
            state,drops=close(n.state); normals.append(replace(n,state=state,drops=n.drops+drops))
        for x in result.exceptional:
            state,drops=close(x.state); exceptional.append(replace(x,state=state,abandoned=x.abandoned+drops))
        return replace(result,normal=tuple(normals),exceptional=tuple(exceptional))
    def value_derivation(self, rule, path, value, children=(), detail=None):
        return node("derivation",rule,site=self.site(path),type=value.type,effect=empty_effect(),children=tuple(children),
                    normal=(),exceptional=(),frames=(),detail=detail or value.record())
    def finish(self, rule, path, t, effects, normals, exceptional, children=(), frames=(), detail=None):
        normals, exceptional = tuple(normals),tuple(exceptional)
        d=node("derivation",rule,site=self.site(path),type=t,effect=effects,children=tuple(children),
               normal=tuple(n.record() for n in normals),exceptional=tuple(e.record() for e in exceptional),
               frames=tuple(frames),detail=detail)
        return Body(t,effects,normals,exceptional,d)

    def exception(self, state, origin, frames=(), abandoned=()):
        # Active affine locals are abandoned, outstanding frames retain their
        # own disjoint slots. This is an unwind recipe, not physical release.
        state=state.clone(); locals_=state.snapshot()
        for binder in state.affine(): del state.slots[binder]
        return Exceptional(state,origin,tuple(abandoned)+locals_,tuple(frames))

    def value(self, raw, state, path):
        from .annotations import checked
        return checked(self,raw,"value",state,path,lambda:self._value(raw,state,path))

    def _value(self, raw, state, path):
        self.path=path
        tag=raw["value"]; state=state.clone(); children=[]
        if tag=="var":
            name=raw["name"]
            require(name in state.names, "unbound term " + name)
            binder=state.names[name]
            require(binder in state.slots, "moved/unavailable term " + name)
            prior=state.slots[binder]; self.check_hold(prior,state)
            is_copy=copyable(prior.type)
            if not is_copy: del state.slots[binder]
            view=node("view","Alias" if is_copy else "Move",site=self.site(path),binder=binder,parent=prior.view)
            value=self.made(prior.type,view,state,"copy" if is_copy else "move",(prior.hold,),prior.implementation)
        elif tag in ("unit","bool","nat","word"):
            t=base({"unit":"Unit","bool":"Bool","nat":"Nat","word":"Word"}[tag])
            view=node("view","Literal",site=self.site(path),literal=raw.get("literal"))
            value=self.made(t,view,state,"literal")
        elif tag=="pair":
            a,state,da=self.value(raw["left"],state,path+("left",))
            b,state,db=self.value(raw["right"],state,path+("right",)); children=[da,db]
            t=node("type","Pair",left=a.type,right=b.type)
            view=node("view","Pair",site=self.site(path),left=a.view,right=b.view)
            value=self.made(t,view,state,"pair",(a.hold,b.hold),node("implementation","pair",left=a.implementation,right=b.implementation))
        elif tag in ("inl","inr"):
            t=self.type(raw["sum_type"],state); require(isinstance(t,RNode) and t.tag=="Sum","injection needs Sum")
            arm="left" if tag=="inl" else "right"
            a,state,da=self.value(raw["payload"],state,path+("payload",)); children=[da]
            self.same(a.type,t.get(arm),state,"Sum payload type")
            view=node("view","Inl" if arm=="left" else "Inr",site=self.site(path),payload=a.view)
            value=self.made(t,view,state,"sum-active-arm",(a.hold,),node("implementation","sum",arm=arm,payload=a.implementation))
        elif tag=="pack":
            t=self.type(raw["exists_type"],state); require(isinstance(t,Binder) and t.kind=="exists","PACK needs Exists")
            old.sequence(raw["witnesses"],"PACK witnesses")
            witnesses=tuple(self.witness(w,sort,state) for w,sort in zip(raw["witnesses"],t.sorts))
            require(len(raw["witnesses"])==len(t.sorts),"PACK witness arity")
            facts,payload=instantiate(t,witnesses,free_world=state.pc.world.allowed)
            proof=state.pc.world.all(facts); require_proof(proof,"PACK interface facts")
            a,state,da=self.value(raw["payload"],state,path+("payload",)); children=[da]
            self.same(a.type,payload,state,"PACK payload mismatch")
            view=node("view","Pack",site=self.site(path),interface=t,witnesses=witnesses,payload=a.view,relations=proof,
                      witness_origins=self.nominal_origins((t,witnesses),state),signature_origins=self.nominal_origins(t,state),
                      witness_generations=self.generation_origins((t,witnesses),state),signature_generations=self.generation_origins(t,state))
            value=self.made(t,view,state,"pack-specific-witness",(a.hold,proof),node("implementation","pack",payload=a.implementation))
        elif tag=="lambda":
            return self.lambda_value(raw,state,path)
        else:
            raise Unsupported("value " + tag)
        self.path=path
        return value,state,self.value_derivation(tag,path,value,children)

    def witness(self, raw, sort, state):
        require(type(raw) is dict,"structured witness")
        resolver=self.source_resolver(state)
        if set(raw)=={"name"}: return resolver.ref(old.ident(raw["name"],"witness"),sort)
        require(set(raw)=={"size"} and sort=="Size","witness fields/sort")
        return resolver.resolve(old.size(raw["size"],"witness.size"))

    def expression(self, raw, state, path):
        from .annotations import checked
        return checked(self,raw,"expression",state,path,lambda:self._expression(raw,state,path))

    def _expression(self, raw, state, path):
        self.path=path
        if raw["expression"]=="value":
            value,state,d=self.value(raw["value"],state,path+("value",))
            return value,state,self.value_derivation("expression-value",path,value,(d,))
        if raw["expression"]=="op":
            parameters,result=self.sigma.pure(raw["name"])
            require(len(raw["arguments"])==len(parameters),"pure operation argument arity")
            arguments=[];children=[]
            for i,(source,typ) in enumerate(zip(raw["arguments"],parameters)):
                value,state,d=self.value(source,state,path+("arguments",i))
                self.same(value.type,typ,state,"pure operation argument type")
                arguments.append(value.view);children.append(d)
            value=self.made(result,node("view","PureOperation",site=self.site(path),name=raw["name"],arguments=tuple(arguments)),state,"fixed-pure-operation")
            return value,state,self.value_derivation("op",path,value,children)
        a,state,da=self.value(raw["value"],state,path+("value",))
        require(isinstance(a.type,RNode) and a.type.tag=="Pair","projection needs Pair")
        arm="left" if raw["expression"]=="fst" else "right"
        view=node("view","Project",site=self.site(path),parent=a.view,path=(arm,))
        value=self.made(a.type.get(arm),view,state,"pair-projection",(a.hold,),
                        node("implementation","project",parent=a.implementation,path=(arm,)))
        return value,state,self.value_derivation(raw["expression"],path,value,(da,))

    def lambda_signature(self, raw, capture_types, state):
        # Shared by actual lambda checking and bottom elaboration. Keeping the
        # complete signature here prevents the latter from dropping chi, pi,
        # explicit kappa, links, mode, or effects while collecting A/B constraints.
        chi,pi,maps=capture_support(tuple(capture_types))
        schema=self.schema(state)
        a=schema.type(raw["parameter"]["type"]); b=schema.type(raw["result"]); eps=schema.effect(raw["effect"])
        form_type(a,state.pc.world); form_type(b,state.pc.world); form_effect(eps,state.pc.world)
        provisional=node("ctype","Fn",mode=raw["mode"],chi=chi,projections=pi,kappa=ktrue(),
                         parameter=a,result=b,effect=eps,links=())
        annotated_chi=chi if raw["foot"]=={"derive":"captures"} else schema.support(raw["foot"])
        annotated_pi=pi if raw["projections"]=={"derive":"captures"} else parse_projections(raw["projections"])
        annotation=RNode("ctype","Fn",tuple((k,annotated_chi if k=="chi" else annotated_pi if k=="projections" else v)
                                            for k,v in provisional.fields))
        validate_projections(annotation)
        require(compare_types(provisional,annotation,state.pc.world).yes,"capture chi/projections annotation mismatch")
        if raw["requires"]=={"derive":"support"}:
            require(raw["links"]=={"derive":"support"},"derived requires needs derived links")
            k,links,l0=derive(provisional,state.pc.world,True)
        else:
            k=schema.kappa(raw["requires"])
            links=tuple(schema.link(x) for x in old.sequence(raw["links"],"links"))
            _,_,l0=derive(provisional,state.pc.world,True)
        fn=RNode("ctype","Fn",tuple((key,k if key=="kappa" else links if key=="links" else value)
                                    for key,value in provisional.fields))
        form_type(fn,state.pc.world)
        return fn,maps,l0

    def lambda_value(self, raw, outer, path):
        self.path=path
        expected_fv=free_terms(raw["body"])-{raw["parameter"]["name"]}
        captures=tuple(raw["captures"])
        require(len(captures)==len(set(captures)),"duplicate capture")
        require(set(captures)==set(expected_fv),"exact free captures mismatch")
        state=outer.clone(); captured=[]; capture_ids=[]; capture_children=[]
        for i,name in enumerate(captures):
            require(name in state.names and state.names[name] in state.slots,"capture unavailable: "+name)
            original=state.slots[state.names[name]]
            require(raw["mode"]=="A" or copyable(original.type),"U cannot capture affine value")
            capture_ids.append(state.names[name])
            value,state,d=self.value({"value":"var","name":name},state,path+("captures",i))
            captured.append(value); capture_children.append(d)
        self.path=path
        fn,maps,l0=self.lambda_signature(raw,tuple(v.type for v in captured),state)
        a,b,k,eps=(fn.get(key) for key in ("parameter","result","kappa","effect"))
        kmin,lfn,minproof=minimum(fn,state.pc.world)
        # Capture ResHold is about these actual values. It cannot be replaced
        # by demanding a universal weak-existential interface at creation.
        # Only the complete free lifetimes are additionally checked here.
        creation_pc=ProofContext(state.pc.world,state.pc.caller,
                                kall((state.pc.assumptions,)+tuple(self.point_requirements(v.type,state.pc.world) for v in captured)),
                                state.pc.evidence+tuple(v.hold for v in captured))
        lifetime_proof=require_proof(prove(use(lfn,state.pc.world),creation_pc),"closure creation lifetime coverage")
        creation=node("proof","lambda-creation-hold",captures=tuple(v.hold for v in captured),lifetimes=lifetime_proof)
        caller=SymbolicCaller(self.site(path)+( "caller",))
        inner=State({}, {}, ProofContext(state.pc.world,caller,k),state.origins,
                    state.source_names,state.generations,state.execution)
        entry_records=[]
        for i,(name,value) in enumerate(zip(captures,captured)):
            p=holding(value.type,inner.pc)
            view=node("view","CaptureEntry",site=self.site(path),index=i,actual=value.view)
            v=self.made(value.type,view,inner,"lambda-capture-entry",(p,minproof),value.implementation)
            bid,_=self.bind(inner,name,v,path+("entry","capture",i),"capture")
            entry_records.append(node("capture","checked",name=name,binder=bid,outer_binder=capture_ids[i],value=v.record()))
        p=holding(a,inner.pc)
        pview=node("view","ParameterEntry",site=self.site(path))
        pv=self.made(a,pview,inner,"lambda-parameter-entry",(p,minproof),node("implementation","parameter",type=a))
        self.bind(inner,raw["parameter"]["name"],pv,path+("entry","parameter"),"parameter")
        body=self.command(raw["body"],inner,path+("body",),b)
        self.path=path+("body",)
        for n in body.normal: require(not n.state.affine(),"lambda normal affine remainder requires explicit drop")
        self.path=path+("effect",)
        match=effect_includes(body.effect,eps); require_proof(match,"lambda effect upper bound")
        capture_map=tuple(node("capture","support",support_path=s.support_path,type_path=s.source_path,
                               qualification=s.qualification,guards=s.guards) for s in maps)
        certificate=node("certificate","lambda",site=self.site(path),fn=fn,captures=tuple(entry_records),capture_map=capture_map,
                         caller=caller_key(caller),kappa_min=kmin,l0=l0,lfn=lfn,minimum_proof=minproof,creation_hold=creation,
                         body=body.derivation,effect_match=match,open_premises=tuple(self.premises))
        view=node("view","Closure",site=self.site(path),captures=tuple(v.view for v in captured),certificate=certificate)
        value=self.made(fn,view,state,"checked-lambda",(creation,),node("implementation","local-lambda",certificate=certificate))
        self.path=path
        return value,state,self.value_derivation("lambda",path,value,tuple(capture_children)+(body.derivation,),certificate)

    def command(self, raw, state, path, expected=None):
        from .annotations import checked
        return checked(self,raw,"command",state,path,lambda:self._command(raw,state,path,expected))

    def _command(self, raw, state, path, expected=None):
        self.path=path; tag=raw["command"]; state=state.clone()
        if tag=="return":
            v,state,d=self.value(raw["value"],state,path+("value",)); self.check_hold(v,state)
            if expected is not None: self.same(v.type,expected,state,"RETURN type mismatch")
            return self.finish(tag,path,v.type,empty_effect(),(Normal(v,state),),(),(d,))
        if tag=="drop":
            v,state,d=self.value(raw["value"],state,path+("value",))
            body=self.command(raw["next"],state,path+("next",),expected)
            return self.finish(tag,path,body.type,body.effect,body.normal,body.exceptional,(d,body.derivation),
                               detail=node("drop","explicit",view=v.view))
        if tag=="let_e":
            v,state,d=self.expression(raw["expression"],state,path+("expression",))
            binder,prior=self.bind(state,raw["name"],v,path)
            body=self.command(raw["next"],state,path+("next",),expected)
            body=self.close_binding(body,raw["name"],binder,prior)
            return self.finish(tag,path,body.type,body.effect,body.normal,body.exceptional,(d,body.derivation))
        if tag=="let_c": return self.let_command(raw,state,path,expected)
        if tag=="call": return self.call(raw,state,path,expected)
        if tag=="await": return self.await_shared(raw,state,path,expected)
        if tag=="await_cancel": return self.await_cancel_shared(raw,state,path,expected)
        if tag in ("call_service","call_tool"):
            from p8closure.caller import InScope
            require(self.sigma.parent,"parent command requires fixed parent Sigma")
            # Only an actual lexical ScopeStep beneath private INIT carries
            # authority. Symbolic lambda callers and inherited worlds do not.
            current=state.pc.caller;original=current
            while isinstance(original,InScope): original=original.parent
            origin=state.pc.world.init_origin
            require(origin is not None and isinstance(original,Caller)
                    and original.job==origin.get("main") and original.task==origin.get("task"),
                    "parent command requires original INIT main Job")
            require(isinstance(current,InScope) and current.origin.get("kind")=="plain",
                    "parent command requires current plain scope")
            children=[];holds=[]
            if tag=="call_service":
                scope,state,d=self.value(raw["scope"],state,path+("scope",));children.append(d)
                require(isinstance(scope.type,RNode) and scope.type.tag=="Scope"
                        and scope.type.get("kind")=="plain","CALL_SERVICE needs plain Scope")
                require(scope.type.get("scope")==current.origin.get("scope")
                        and scope.type.get("region")==current.origin.get("region"),
                        "CALL_SERVICE requires current innermost scope")
                self.check_hold(scope,state);holds.append(scope.hold)
                operand,state,d=self.value(raw["tokens"],state,path+("tokens",));children.append(d)
                parameter,result=self.sigma.host_service(raw["name"])
                self.same(operand.type,parameter,state,"CALL_SERVICE tokens must be Tokens8")
                eps=effect(node("atom","HostService",scope=current.origin.get("scope"),service="B"),node("atom","Raise"))
                detail=self.sigma.host_service_boundary()
            else:
                operand,state,d=self.value(raw["argument"],state,path+("argument",));children.append(d)
                parameter,result=self.sigma.tool(raw["name"])
                self.same(operand.type,parameter,state,"CALL_TOOL argument must be Word")
                eps=effect(node("atom","LocalRead",capability="tokenizer_vocab_v1"),node("atom","Raise"))
                detail=self.sigma.tool_boundary()
            self.check_hold(operand,state);holds.append(operand.hold)
            view=node("view","ParentHostResult",site=self.site(path),command=tag,
                      scope=current.origin.get("scope"),argument=operand.view)
            value=self.made(result,view,state,"parent-host-result",tuple(holds))
            if expected is not None:self.same(result,expected,state,"parent command result type")
            failure=node("failure","current-caller",caller=caller_key(state.pc.caller),site=self.site(path))
            # No scope completion/response provenance/Task is synthesized here.
            return self.finish(tag,path,result,eps,(Normal(value,state),),
                               (self.exception(state,failure),),tuple(children),detail=detail)
        if tag=="receive_request":
            # Possessing an inherited world, Owner or caller requirements is not
            # evidence of being the private INIT main. In particular ordinary
            # child Jobs and cleanup Jobs inherit init_origin but not its caller.
            origin=state.pc.world.init_origin
            require(origin is not None and isinstance(state.pc.caller,Caller)
                    and state.pc.caller.job==origin.get("main")
                    and state.pc.caller.task==origin.get("task"),
                    "RECEIVE_REQUEST requires original INIT main Job")
            owner,state,derivation=self.value(raw["owner"],state,path+("owner",))
            require(isinstance(owner.type,RNode) and owner.type.tag=="Owner","RECEIVE_REQUEST needs Owner")
            self.check_hold(owner,state)
            result=self.sigma.host_input(raw["name"])
            # A stable input value and an identity association only: no access,
            # epoch, Read, Lease, Write, producer or storage grant is introduced.
            view=node("view","ExternalRequestInput",site=self.site(path),port=raw["name"],owner=owner.view)
            value=self.made(result,view,state,"receive-request-result",(owner.hold,))
            if expected is not None: self.same(value.type,expected,state,"RECEIVE_REQUEST result type")
            eps=effect(node("atom","HostInput",owner=owner.type.get("owner")),node("atom","Raise"))
            failure=node("failure","current-caller",caller=caller_key(state.pc.caller),site=self.site(path))
            return self.finish(tag,path,value.type,eps,(Normal(value,state),),
                               (self.exception(state,failure),),(derivation,),
                               detail=self.sigma.host_input_boundary())
        if tag=="offer_response":
            # This authority belongs to INIT's concrete Job/Task, not to an
            # inherited world, lexical closure or the current invocation.
            # Existing synchronous ScopeStep creates InScope with the same
            # Job/Task. Only unwrap that private provenance; a child Job or a
            # symbolic closure caller never becomes the original main.
            from p8closure.caller import InScope
            original_caller=state.pc.caller
            while isinstance(original_caller,InScope):
                original_caller=original_caller.parent
            origin=state.pc.world.init_origin
            require(origin is not None and isinstance(original_caller,Caller)
                    and original_caller.job==origin.get("main")
                    and original_caller.task==origin.get("task"),
                    "OFFER_RESPONSE requires original INIT main Job")
            owner,state,owner_derivation=self.value(raw["owner"],state,path+("owner",))
            token,state,token_derivation=self.value(raw["token"],state,path+("token",))
            require(isinstance(owner.type,RNode) and owner.type.tag=="Owner","OFFER_RESPONSE needs Owner")
            payload,result=self.sigma.host_response(raw["name"])
            self.same(token.type,payload,state,"OFFER_RESPONSE token must be Word")
            self.check_hold(owner,state);self.check_hold(token,state)
            # An intent only. No access/scope safety, response, publication or
            # outstanding-frame discharge is derived by this static rule.
            view=node("view","ExternalResponseIntent",site=self.site(path),port=raw["name"],
                      owner=owner.view,token=token.view)
            value=self.made(result,view,state,"offer-response-result",(owner.hold,token.hold))
            if expected is not None: self.same(value.type,expected,state,"OFFER_RESPONSE result type")
            eps=effect(node("atom","HostResponse",owner=owner.type.get("owner")),node("atom","Raise"))
            failure=node("failure","current-caller",caller=caller_key(state.pc.caller),site=self.site(path))
            return self.finish(tag,path,value.type,eps,(Normal(value,state),),
                               (self.exception(state,failure),),(owner_derivation,token_derivation),
                               detail=self.sigma.host_response_boundary())
        if tag=="load":
            r,state,dr=self.value(raw["read"],state,path+("read",))
            index,state,di=self.value(raw["index"],state,path+("index",))
            require(isinstance(r.type,RNode) and r.type.tag=="Read","LOAD needs Read")
            require(index.type==base("Nat"),"LOAD index must be Nat")
            self.check_hold(r,state)
            access=one(state.pc.world,"access",access=r.type.get("access"),owner=r.type.get("owner"),
                       epoch=r.type.get("epoch"),region=r.type.get("region"))
            if isinstance(state.pc.caller,Caller):
                require(access.get("job")==state.pc.caller.job,"LOAD caller job mismatch")
            view=node("view","Load",site=self.site(path),read=r.view,index=index.view)
            v=self.made(base("Word"),view,state,"load-result",(r.hold,))
            if expected is not None: self.same(v.type,expected,state,"LOAD result type")
            eps=effect(node("atom","Read",owner=r.type.get("owner")),node("atom","Raise"))
            origin=node("failure","current-caller",caller=caller_key(state.pc.caller),site=self.site(path))
            return self.finish(tag,path,v.type,eps,(Normal(v,state),),(self.exception(state,origin),),(dr,di))
        if tag in ("throw","rethrow"):
            field="payload" if tag=="throw" else "failure"
            v,state,d=self.value(raw[field],state,path+(field,))
            require(v.type==base("ErrorPayload" if tag=="throw" else "Failure"),tag+" requires "+("ErrorPayload" if tag=="throw" else "Failure"))
            require(stable(v.type),"failure payload must be Stable")
            origin=node("failure","new" if tag=="throw" else "rethrow",caller=caller_key(state.pc.caller),value=v.view,site=self.site(path))
            return self.finish(tag,path,expected,effect(node("atom","Raise")),(),(self.exception(state,origin),),(d,))
        if tag in ("if","case"): return self.branch(raw,state,path,expected)
        if tag=="try": return self.try_command(raw,state,path,expected)
        if tag=="unpack": return self.unpack(raw,state,path,expected)
        if tag=="new_owner":
            from .generation import check_new_owner
            return check_new_owner(self,raw,state,path,expected)
        if tag in ("scope","run"):
            from .generation import check_scope
            return check_scope(self,raw,state,path,expected)
        if tag=="produce":
            from .producer import check_produce
            return check_produce(self,raw,state,path,expected)
        if tag=="fork":
            from .fork import check_fork
            return check_fork(self,raw,state,path,expected)
        if tag=="request_cancel":
            from .producer import check_cancel
            return check_cancel(self,raw,state,path,expected)
        raise Unsupported("command "+tag)

    def await_shared(self, raw, state, path, expected):
        shared,state,d=self.value(raw["shared"],state,path+("shared",))
        require(isinstance(shared.type,RNode) and shared.type.tag=="Shared","AWAIT needs Shared")
        self.check_hold(shared,state);form_type(shared.type,state.pc.world)
        job=shared.type.get("job");payload=shared.type.get("payload")
        require(stable(payload),"AWAIT payload must be Stable")
        relation=one(state.pc.world,"result",job=job)
        self.same(payload,relation.get("result"),state,"AWAIT Shared/result mismatch")
        if expected is not None: self.same(payload,expected,state,"AWAIT result type")
        # A conditional delivered value, not a projection of a current Shared
        # payload. Its implementation is opaque until actual terminal delivery;
        # later CALL still proves this complete Fn's own kappa independently.
        view=node("view","AwaitResult",site=self.site(path),shared=shared.view,job=job,
                  payload=payload,result_relation=relation,
                  signature_origins=self.nominal_origins(payload,state),
                  signature_generations=self.generation_origins(payload,state))
        post=node("proof","await-terminal-postcondition",site=self.site(path),shared=shared.record(),
                  job=job,type=payload,result_relation=relation,caller=caller_key(state.pc.caller),
                  mode="terminal",conditional=True,runtime_executed=False)
        implementation=node("implementation","await-delivered",postcondition=post)
        value=self.made(payload,view,state,"AWAIT-NORMAL-POST",(shared.hold,post),implementation)
        normal=state.clone()
        point=self.point_requirements(payload,state.pc.world)
        normal.pc=ProofContext(state.pc.world,state.pc.caller,kall((state.pc.assumptions,point)),
                               state.pc.evidence+(value.hold,))
        origin=node("failure","await-target",site=self.site(path),shared=shared.view,job=job,
                    mode="terminal",identity_policy="preserve-target-faults",origin_policy="opaque-target-origin")
        eps=effect(node("atom","Need",job=job,mode="terminal"),node("atom","Raise"))
        return self.finish("await",path,payload,eps,(Normal(value,normal),),
                           (self.exception(state,origin),),(d,),detail=post)

    def await_cancel_shared(self, raw, state, path, expected):
        shared,state,d=self.value(raw["shared"],state,path+("shared",))
        require(isinstance(shared.type,RNode) and shared.type.tag=="Shared","AWAIT? needs Shared")
        self.check_hold(shared,state);form_type(shared.type,state.pc.world)
        job=shared.type.get("job");payload=shared.type.get("payload")
        require(stable(payload),"AWAIT? payload must be Stable")
        relation=one(state.pc.world,"result",job=job)
        self.same(payload,relation.get("result"),state,"AWAIT? Shared/result mismatch")
        result_type=node("type","Sum",left=payload,right=base("Unit"))
        if expected is not None: self.same(result_type,expected,state,"AWAIT? result type")

        # These are the two conditional normal outcomes, not an evaluation of
        # the producer or a claim that the left payload currently exists.
        # Complete Fn kappa and Exists Phi stay inside the left type. No left
        # arm requirement is added to the caller's ambient assumptions.
        delivered_view=node("view","AwaitResult",site=self.site(path),shared=shared.view,job=job,
                            payload=payload,result_relation=relation,mode="publication",
                            signature_origins=self.nominal_origins(payload,state),
                            signature_generations=self.generation_origins(payload,state))
        delivered_post=node("proof","await-publication-postcondition",site=self.site(path),
                            shared=shared.record(),job=job,type=payload,result_relation=relation,
                            caller=caller_key(state.pc.caller),mode="publication",
                            conditional=True,runtime_executed=False)
        delivered=self.made(payload,delivered_view,state,"AWAIT?-DELIVERED-POST",
                            (shared.hold,delivered_post),
                            node("implementation","await-delivered",postcondition=delivered_post))
        left=self.made(result_type,node("view","Inl",site=self.site(path),payload=delivered.view),
                       state,"sum-active-arm",(delivered.hold,),
                       node("implementation","sum",arm="left",payload=delivered.implementation))
        cancelled_post=node("proof","await-cancelled-postcondition",site=self.site(path),
                            caller=caller_key(state.pc.caller),type=base("Unit"),
                            conditional=True,runtime_executed=False)
        cancelled=self.made(base("Unit"),node("view","Literal",site=self.site(path),literal=None),
                            state,"AWAIT?-CANCELLED-POST",(cancelled_post,))
        right=self.made(result_type,node("view","Inr",site=self.site(path),payload=cancelled.view),
                        state,"sum-active-arm",(cancelled.hold,),
                        node("implementation","sum",arm="right",payload=cancelled.implementation))
        view=node("view","Choice",site=self.site(path),edges=((0,left.view),(1,right.view)))
        post=node("proof","await-cancel-postcondition",site=self.site(path),shared=shared.record(),
                  job=job,type=result_type,payload=payload,result_relation=relation,
                  caller=caller_key(state.pc.caller),mode="publication",
                  delivered=delivered_post,cancelled=cancelled_post,
                  conditional=True,runtime_executed=False)
        value=self.made(result_type,view,state,"AWAIT?-NORMAL-BY-CASES",(left.hold,right.hold,post),
                        node("implementation","choice",selector=self.site(path),
                             branches=(left.implementation,right.implementation)))
        normal=state.clone()
        normal.pc=ProofContext(state.pc.world,state.pc.caller,state.pc.assumptions,
                               state.pc.evidence+(value.hold,))
        origin=node("failure","await-target",site=self.site(path),shared=shared.view,job=job,
                    mode="publication",identity_policy="preserve-target-faults",origin_policy="opaque-target-origin")
        eps=effect(node("atom","Need",job=job,mode="publication"),
                   node("atom","WaitDetach",job=job),node("atom","Raise"))
        return self.finish("await_cancel",path,result_type,eps,(Normal(value,normal),),
                           (self.exception(state,origin),),(d,),detail=post)

    def call(self, raw, state, path, expected):
        f,state,df=self.value(raw["function"],state,path+("function",))
        require(isinstance(f.type,RNode) and f.type.tag=="Fn","CALL needs Fn")
        a,state,da=self.value(raw["argument"],state,path+("argument",))
        self.same(a.type,f.type.get("parameter"),state,"CALL argument type/projections mismatch")
        require(f.implementation is not None,"CALL missing function implementation premise")
        form_type(f.type,state.pc.world)
        p=prove(f.type.get("kappa"),state.pc); require_proof(p,"CALL kappa caller requirement")
        b=f.type.get("result")
        if expected is not None: self.same(b,expected,state,"CALL result type")
        frame=node("frame","CallReturn",site=self.site(path),caller=caller_key(state.pc.caller),remaining=state.snapshot())
        view=node("view","Result",site=self.site(path),function=f.view,argument=a.view,
                  signature_origins=self.nominal_origins(b,state),signature_generations=self.generation_origins(b,state))
        from .calls import normal_binding
        binding=normal_binding(f,a,state.pc.caller,self.site(path))
        post=node("proof","normal-postcondition",type=b,callee=f.implementation,caller=caller_key(state.pc.caller),call=p,binding=binding)
        value=self.made(b,view,state,"CALL-NORMAL-POST",(post,),node("implementation","normal-result",callee=f.implementation,type=b,view=view,postcondition=post))
        eps=f.type.get("effect")
        exceptional=()
        if self.may_raise(eps):
            from .calls import exception_postcondition
            origin=exception_postcondition(f,a,state.pc.caller,self.site(path))
            suspended=state.clone()
            for binder in suspended.affine(): del suspended.slots[binder]
            abandoned=tuple(node("abandon","callee-input",value=v.record()) for v in (f,a) if not copyable(v.type))
            exceptional=(Exceptional(suspended,origin,abandoned,(frame,)),)
        return self.finish("call",path,b,eps,(Normal(value,state),),exceptional,(df,da),(frame,),post)

    def may_raise(self, eps):
        atoms,blocks=effect_parts(eps)
        return any(a.tag=="Raise" for a in atoms) or any(self.may_raise(b.body) for b in blocks)

    def let_command(self, raw, state, path, expected):
        # A direct CALL owns its unique CallReturn. Other command expressions
        # receive only the affine slots they mention; Bind owns the complement.
        direct_call=raw["first"]["command"] in ("call","try")
        reserved={}
        active=state.clone()
        if not direct_call:
            used=free_terms(raw["first"])
            used_ids={state.names[n] for n in used if n in state.names}
            reserved={b:state.slots[b] for b in state.affine()-used_ids}
            for b in reserved: del active.slots[b]
        frame_state=State({},reserved,state.pc)
        frame=() if direct_call else (node("frame","Bind",site=self.site(path),remaining=frame_state.snapshot()),)
        self.path=path+("bind_type",) if "bind_type" in raw else path
        bind_type=self.type(raw["bind_type"],state) if "bind_type" in raw else None
        first=self.command(raw["first"],active,path+("first",),bind_type)
        hypothetical=state.clone()
        if not direct_call:
            for b in state.affine()-set(reserved): del hypothetical.slots[b]
        if not first.normal:
            inferred=bind_type or first.type
            if inferred is None:
                self.path=path
                inferred=infer_bottom_type(self,hypothetical,raw["name"],raw["next"],expected)
            form_type(inferred,state.pc.world)
            # THROW/RETHROW and every first branch are now actually checked at
            # the solved type, rather than relabelling an untyped proof.
            first=self.command(raw["first"],active,path+("first",),inferred)
            require(not first.normal,"bottom recheck introduced a normal exit")
        children=[first.derivation]; normals=[]
        exceptional=[replace(e,frames=e.frames+frame) for e in first.exceptional]
        effects=[first.effect]; types=[]
        for n in first.normal:
            nxt=n.state.clone()
            for b,v in reserved.items():
                require(b not in nxt.slots,"duplicate Bind restoration")
                nxt.slots[b]=v
            binder,prior=self.bind(nxt,raw["name"],n.value,path)
            body=self.command(raw["next"],nxt,path+("next",),expected)
            body=self.close_binding(body,raw["name"],binder,prior)
            children.append(body.derivation); effects.append(body.effect); normals.extend(body.normal); exceptional.extend(body.exceptional)
            if body.type is not None: types.append(body.type)
        if not first.normal:
            # An unreachable continuation is still checked in an explicit bottom
            # context. Its hypothetical binder is not a normal Result/ValueView.
            ghost=self.made(first.type,node("view","UnreachableBinder",site=self.site(path),type=first.type),hypothetical,"bottom-context",(),
                            node("implementation","unreachable-assumption",type=first.type))
            nxt=hypothetical.clone(); binder,prior=self.bind(nxt,raw["name"],ghost,path)
            nxt.execution="unreachable-continuation"
            body=self.command(raw["next"],nxt,path+("next",),expected)
            children.append(node("derivation","unreachable-continuation",site=self.site(path),body=body.derivation))
            if body.type is not None: types.append(body.type)
        t=expected or (types[0] if types else None)
        for other in types:
            if t is not None: self.same(other,t,state,"LET-C continuation type")
        return self.finish("let_c",path,t,union(*effects),normals,exceptional,children,frame)

    def branch(self, raw, state, path, expected):
        tag=raw["command"]; field="condition" if tag=="if" else "value"
        value,state,d=self.value(raw[field],state,path+(field,))
        if tag=="if": require(value.type==base("Bool"),"IF requires Bool")
        else: require(isinstance(value.type,RNode) and value.type.tag=="Sum","CASE requires Sum")
        bodies=[]
        for arm in (("then","else") if tag=="if" else ("left","right")):
            branch=state.clone()
            if tag=="case":
                view=node("view","Project",site=self.site(path),parent=value.view,path=(arm,),guard=node("guard","sum",selector=value.view,arm=arm))
                v=self.made(value.type.get(arm),view,branch,"case-active-arm",(value.hold,),node("implementation","project",parent=value.implementation,path=(arm,)))
                binder,prior=self.bind(branch,raw[arm+"_name"],v,path+(arm,),"case")
            body=self.command(raw[arm],branch,path+(arm,),expected)
            if tag=="case": body=self.close_binding(body,raw[arm+"_name"],binder,prior)
            bodies.append(body)
        types=[b.type for b in bodies if b.type is not None]; t=expected or (types[0] if types else None)
        for other in types:
            if t is not None: self.same(other,t,state,"branch result type/projections mismatch")
        normals=[n for b in bodies for n in b.normal]
        if normals:
            required=normals[0].state.affine()
            require(all(n.state.affine()==required for n in normals),"branch affine remainder identity mismatch")
            # One correlated whole-value choice; projection never selects pieces.
            edges=tuple((i,n.value.view) for i,n in enumerate(normals))
            view=node("view","Choice",site=self.site(path),edges=edges)
            result=self.made(t,view,normals[0].state,"branch-proof-by-cases",tuple(n.value.hold for n in normals),
                             node("implementation","choice",selector=self.site(path),branches=tuple(n.value.implementation for n in normals)))
            normals=[Normal(result,normals[0].state,drops=tuple(d for n in normals for d in n.drops))]
        return self.finish(tag,path,t,union(*(b.effect for b in bodies)),normals,
                           tuple(e for b in bodies for e in b.exceptional),(d,)+(tuple(b.derivation for b in bodies)))

    def try_command(self, raw, state, path, expected):
        affine=state.affine(); forbidden={name for name,b in state.names.items() if b in affine}
        require(not (free_terms(raw["body"])| (free_terms(raw["handler"])-{raw["failure_name"]})) & forbidden,
                "TRY body/handler cannot capture outer affine frame")
        isolated=state.clone()
        for binder in affine: del isolated.slots[binder]
        frame=node("frame","TryRestore",site=self.site(path),remaining=state.snapshot())
        body=self.command(raw["body"],isolated,path+("body",),expected)
        handler_state=isolated.clone()
        unwinds=tuple(node("frame","UnwindToTry",site=self.site(path),frames=e.frames,abandoned=e.abandoned,
                           remaining=e.state.snapshot()) for e in body.exceptional)
        fview=node("view","FailureInput",site=self.site(path),origins=tuple(e.origin for e in body.exceptional),unwinds=unwinds)
        failure=self.made(base("Failure"),fview,handler_state,"try-handler")
        binder,prior=self.bind(handler_state,raw["failure_name"],failure,path,"failure")
        handler=self.command(raw["handler"],handler_state,path+("handler",),expected or body.type)
        handler=self.close_binding(handler,raw["failure_name"],binder,prior)
        t=expected or body.type or handler.type
        for result in (body,handler):
            if result.type is not None and t is not None: self.same(result.type,t,state,"TRY handler type")
        normals=list(body.normal)
        if body.exceptional: normals.extend(handler.normal)
        restored=[]
        for n in normals:
            s=n.state.clone()
            for bid in affine: require(bid not in s.slots,"duplicate TryRestore frame"); s.slots[bid]=state.slots[bid]
            restored.append(replace(n,state=s))
        exceptional=tuple(replace(e,frames=e.frames+(frame,)) for e in handler.exceptional) if body.exceptional else ()
        # Catch absorbs only body's Raise atoms, preserving Read and hide blocks.
        def caught(e):
            if isinstance(e,Binder): return Binder(e.kind,e.sorts,e.facts,caught(e.body))
            if e.tag=="set": return effect(*(a for a in e.get("atoms") if a.tag!="Raise"))
            return union(caught(e.get("left")),caught(e.get("right")))
        eps=union(caught(body.effect),handler.effect)
        if restored:
            require(all(n.state.affine()==restored[0].state.affine() for n in restored),"TRY normal affine remainder identity")
            view=node("view","Choice",site=self.site(path),edges=tuple((i,n.value.view) for i,n in enumerate(restored)))
            v=self.made(t,view,restored[0].state,"try-proof-by-cases",tuple(n.value.hold for n in restored),
                        node("implementation","choice",selector=self.site(path),branches=tuple(n.value.implementation for n in restored)))
            restored=[Normal(v,restored[0].state,drops=tuple(d for n in restored for d in n.drops))]
        return self.finish("try",path,t,eps,restored,exceptional,(body.derivation,handler.derivation),(frame,),
                           node("proof","try-unwind",unwinds=unwinds))

    def unpack(self, raw, state, path, expected):
        package,state,d=self.value(raw["package"],state,path+("package",))
        require(isinstance(package.type,Binder) and package.type.kind=="exists","UNPACK requires actual Exists")
        names=tuple(old.name(n,"unpack.name") for n in old.sequence(raw["names"],"unpack.names"))
        require(tuple(n.sort for n in names)==package.type.sorts,"UNPACK ordered name sorts")
        require(len({n.text for n in names})==len(names) and not {n.text for n in names}&{n.text for n in state.pc.world.names},
                "UNPACK names must be fresh")
        refs=tuple(Free(n.sort,n.text) for n in names)
        facts,payload=instantiate(package.type,refs,free_world=state.pc.world.allowed|frozenset(refs))
        world=state.pc.world.extend(names,facts); form_world(world,False)
        inner=state.clone(); inner.pc=state.pc.in_world(world)
        if inner.source_names is not None: inner.source_names+=names
        ancestors=list(self.opening_ancestors(package.view))
        for origin in self.nominal_origins(package.type,state):
            ancestor=origin.get("opening")
            for item in ancestor.get("ancestors")+(ancestor,):
                if item not in ancestors: ancestors.append(item)
        opening=node("opening","checked",site=self.site(path),package=package.view,witnesses=refs,interface=package.type,
                     ancestors=tuple(ancestors))
        inner.origins=state.origins+tuple((ref,opening,i) for i,ref in enumerate(refs))
        view=node("view","Open",site=self.site(path),package=package.view,opening=opening)
        value=self.made(payload,view,inner,"unpack-specific-witness",(package.hold,),node("implementation","open",parent=package.implementation,opening=opening))
        point=self.point_requirements(payload,world)
        inner.pc=ProofContext(world,inner.pc.caller,kall((inner.pc.assumptions,point)),inner.pc.evidence+(value.hold,))
        binder,prior=self.bind(inner,raw["name"],value,path,"unpack")
        body=self.command(raw["body"],inner,path+("body",),None)
        if not body.normal and expected is not None:
            # Infer normal results before escape checking, but instantiate a
            # wholly exceptional body at the actual enclosing target as well.
            # Merely labelling the UNPACK node must not leave its THROW/LET
            # children checked at an unrelated/default target type.
            body=self.command(raw["body"],inner,path+("body",),expected)
        body=self.close_binding(body,raw["name"],binder,prior)
        closed=[]; exceptional=[]
        for n in body.normal:
            require(not free_names(n.value.type)&frozenset(refs),"UNPACK result skolem escape")
            if expected is not None: self.same(n.value.type,expected,state,"UNPACK result type")
            s=n.state.clone()
            require(not any(free_names(v.type)&frozenset(refs) for v in s.slots.values()),"UNPACK remaining slot skolem escape")
            s.pc=state.pc
            s.origins=state.origins
            s.source_names=state.source_names
            # Close history under the opening, without pretending references
            # in a legal re-PACK recipe are currently executable values.
            v=self.made(n.value.type,node("view","ClosedOpening",site=self.site(path),opening=opening,body=n.value.view),
                        s,"lexical-close",(n.value.hold,),node("implementation","closed-opening",opening=opening,body=n.value.implementation))
            closed.append(replace(n,value=v,state=s))
        for e in body.exceptional:
            s=e.state.clone(); s.pc=state.pc; s.origins=state.origins; s.source_names=state.source_names
            require(not any(free_names(v.type)&frozenset(refs) for v in s.slots.values()),"UNPACK exceptional slot escape")
            frames=e.frames
            abandoned=e.abandoned
            if free_names(frames)&frozenset(refs):
                frames=(node("frame","ClosedOpening",opening=opening,frames=frames),)
            if free_names(abandoned)&frozenset(refs):
                abandoned=(node("abandon","ClosedOpening",opening=opening,values=abandoned),)
            exceptional.append(replace(e,state=s,frames=frames,abandoned=abandoned,
                                       origin=node("failure","closed-opening",opening=opening,origin=e.origin)))
        eps=body.effect; hide=None
        if free_names(eps)&frozenset(refs):
            eps=Binder("hide",package.type.sorts,package.type.facts,close_refs(eps,refs))
            hide=node("hide","instance",site=self.site(path),opening=opening,effect=eps)
        return self.finish("unpack",path,expected or body.type,eps,closed,exceptional,(d,body.derivation),detail=node("lexical","closed",opening=opening,hide=hide))

    def opening_ancestors(self, view, projection=()):
        """Interpret the typed projection; never scan an arbitrary child field.

        Choice is one correlated whole-value selector, represented as a finite
        conditional ancestry recipe when its edges differ. Closed history is
        retained in package.view, not re-opened as current witness authority.
        """
        if not isinstance(view,RNode): return ()
        tag=view.tag
        if tag in ("Alias","Move"):
            return self.opening_ancestors(view.get("parent"),projection)
        if tag=="ClosedGeneration":
            return self.opening_ancestors(view.get("body"),projection)
        if tag in ("ScopeEntry","ScopeResult"):
            return self.opening_ancestors(view.get("parent"),projection)
        if tag=="CaptureEntry":
            return self.opening_ancestors(view.get("actual"),projection)
        if tag=="Project":
            return self.opening_ancestors(view.get("parent"),view.get("path")+projection)
        if tag=="Pair" and projection:
            arm=projection[0]; require(arm in ("left","right"),"Pair ancestry projection")
            return self.opening_ancestors(view.get(arm),projection[1:])
        if tag in ("Inl","Inr") and projection:
            actual="left" if tag=="Inl" else "right"
            if projection[0]!=actual:
                return (node("ancestry","guarded-inactive",selector=view,arm=projection[0],path=projection[1:]),)
            return self.opening_ancestors(view.get("payload"),projection[1:])
        if tag=="Choice":
            edges=tuple((edge,self.opening_ancestors(value,projection)) for edge,value in view.get("edges"))
            if not any(route for _,route in edges): return ()
            return (node("ancestry","Choice",selector=view.get("site"),path=projection,edges=edges),)
        if tag=="Open":
            opening=view.get("opening")
            return opening.get("ancestors")+(opening,)
        if tag in ("Pack","Result","AwaitResult"):
            # Re-PACK can reuse witnesses without retaining the old package as
            # an enclosing binder. Its witness_origins retain that history;
            # only still-free signature names determine current ancestors.
            origins=dict(view.fields).get("signature_origins",())
            result=[]
            for origin in origins:
                opening=origin.get("opening")
                for ancestor in opening.get("ancestors")+(opening,):
                    if ancestor not in result: result.append(ancestor)
            return tuple(result)
        if tag=="ClosedOpening": return ()
        # Inputs/parameters/captures have abstract configuration-backed values.
        # Any outer lexical name is separately resolved by nominal_origins;
        # their actual witness recipe remains the complete source view.
        return ()


def check_document(raw,sigma=None):
    checker=Checker("",sigma)
    try:
        validate_document(raw)
        digest=hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
        checker=Checker(digest,sigma)
        checker.path=("world",)
        names=tuple(old.name(n,"world.name") for n in old.sequence(raw["world"]["names"],"world.names"))
        schema=Schema(Resolver(names))
        facts=tuple(schema.relation(f) for f in old.sequence(raw["world"]["facts"],"world.facts"))
        world=ClosureContext(names,facts); form_world(world)
        c=raw["entry"]["caller"]; resolver=Resolver(names)
        caller=Caller(*(resolver.ref(c[k],sort) for k,sort in (("job","Job"),("task","Task"),("invocation","Invocation"),("region","Region"))),
                      tuple(resolver.ref(s,"Scope") for s in old.sequence(c["scope_stack"],"scope_stack")))
        caller.validate(world)
        state=State({}, {}, ProofContext(world,caller,ktrue()))
        inputs=raw["entry"]["inputs"]
        require(len({i["name"] for i in inputs})==len(inputs),"duplicate entry input")
        for i,item in enumerate(inputs):
            checker.path=("entry","inputs",i)
            t=checker.type(item["type"],state); p=holding(t,state.pc)
            assumption=node("premise","external-configuration",site=checker.site(checker.path),name=item["name"],type=t)
            checker.premises.append(assumption)
            view=node("view","Input",site=checker.site(checker.path),premise=assumption)
            v=checker.made(t,view,state,"external-input",(p,assumption),node("implementation","external",premise=assumption))
            checker.bind(state,item["name"],v,checker.path,"input")
        expected=checker.type(raw["entry"]["result"],state)
        upper=schema.effect(raw["entry"]["effect"]); form_effect(upper,world)
        body=checker.command(raw["entry"]["body"],state,("entry","body"),expected)
        for n in body.normal: require(not n.state.affine(),"entry affine remainder requires explicit drop")
        checker.path=("entry","effect")
        match=effect_includes(body.effect,upper); require_proof(match,"entry effect upper bound")
        derivation=node("derivation","entry",site=(digest,"entry"),type=expected,effect=body.effect,children=(body.derivation,),
                        normal=tuple(n.record() for n in body.normal),exceptional=tuple(e.record() for e in body.exceptional),frames=(),
                        detail=node("proof","entry",caller=caller_key(caller),effect_match=match,premises=tuple(checker.premises)))
        return SourceResult("checked-source-fragment-under-explicit-premises",derivation,None,tuple(checker.premises))
    except NeedsBindType as error:
        return SourceResult("unsupported",diagnostic={"code":"needs-bind-type","path":list(checker.path),
                            "message":str(error),"details":{"phase":"elaboration","reason":"insufficient type information"}})
    except Unsupported as error:
        return SourceResult("unsupported",diagnostic={"code":"unsupported","path":list(getattr(error,"source_path",checker.path)),"message":str(error)})
    except (FoundationError, old.ParseError) as error:
        return SourceResult("semantic-no",diagnostic={"code":"semantic-no","path":list(getattr(error,"source_path",checker.path)),"message":str(error)})
    except Exception as error:
        return SourceResult("infrastructure-error",diagnostic={"code":type(error).__name__,"path":list(checker.path),"message":str(error)})


def verify_derivation(raw, derivation, sigma=None):
    """Reconstruct every rule from source; supplied proof fields are never trusted."""
    result=check_document(raw,sigma)
    return Decision("yes" if result.verdict=="checked-source-fragment-under-explicit-premises" and result.derivation==derivation else "no",
                    "source-reconstruction-correspondence",(result.verdict,))
