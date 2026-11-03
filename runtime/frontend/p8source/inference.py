"""Finite monomorphic constraints for an unannotated nonreturning LET-C.

This collects type information only. The source checker subsequently rechecks
both first and continuation with the completed type; no proof or value is
produced here. Unknown dependent interfaces require an explicit bind_type.
"""
from dataclasses import dataclass
from p8static import parser as old
from p8foundation.model import Binder, RNode, Free, node, require
from p8foundation.resolve import Resolver
from p8foundation.algebra import instantiate
from p8closure.schema import Schema, canonical, compare_types
from p8closure.support import ClosureContext
from p8closure.qualified import capture_support
from p8closure.requirements import Unsupported, SymbolicCaller, ProofContext, derive, ktrue, kall


class NeedsBindType(Unsupported):
    pass


@dataclass(eq=False)
class Hole:
    binding: object = None


@dataclass(eq=False)
class Arrow:
    parameter: object
    result: object
    concrete: object = None
    alias: object = None


def deref(t):
    while True:
        if isinstance(t,Hole) and t.binding is not None: t=t.binding
        elif isinstance(t,Arrow) and t.alias is not None: t=t.alias
        else: break
    return t


class Constraints:
    def __init__(self, checker, state):
        self.checker=checker; self.state=state; self.deferred=[]; self.defaulted=False
        self.dependent=[]
        self.contexts={};self.context_serial=0

    def context_path(self,tag):
        self.context_serial+=1
        return tuple(self.checker.path)+( "elaboration",tag,self.context_serial)

    def refresh(self,state):
        # Deferred capture constraints may strengthen a lambda's OWN kappa.
        # Rebuild descendant inference worlds from that context before using
        # their scope coverage edges. Never retain the creator's caller/kappa
        # as the lambda body context, or export these provisional records.
        entry=self.contexts.get(id(state))
        if entry is not None:
            retained,rebuild=entry
            require(retained is state,"inference context identity mismatch")
            current=rebuild()
            state.pc=current.pc;state.source_names=current.source_names
            state.origins=current.origins;state.generations=current.generations
            state.execution=current.execution
        return state

    def context(self,rebuild):
        state=rebuild()
        # Retain the state itself: a discarded child's Python id may otherwise
        # be reused by a later context during the same constraint collection.
        self.contexts[id(state)]=(state,rebuild)
        return state

    def known_type_shape(self,t):
        """A non-binding support approximation, never an inferred Fn result.

        Unknown leaves contribute no known lifetime. Arrow parameter/result
        information is retained without inventing a capture/effect contract.
        Full materialization and lambda_signature still run separately.
        """
        t=deref(t)
        if isinstance(t,Hole): return node("type","Unit")
        if isinstance(t,Arrow):
            if t.concrete is not None: return t.concrete
            return node("ctype","Fn",mode="U",chi=node("support","zero"),
                        projections=node("projection","contract",selectors=(),entries=()),
                        kappa=ktrue(),parameter=self.known_type_shape(t.parameter),
                        result=self.known_type_shape(t.result),effect=node("effect","set",atoms=()),links=())
        if isinstance(t,RNode) and t.tag in ("Pair","Sum"):
            return node("type",t.tag,left=self.known_type_shape(t.get("left")),
                        right=self.known_type_shape(t.get("right")))
        return t

    def known_lambda_kappa(self,raw,captured,state,a,b):
        # Derived requirements already justified by known capture components,
        # the declared parameter/result, and the declared effect must remain
        # available while other capture holes are solved from the body.
        # These temporary shapes never bind holes or escape in a derivation.
        chi,pi,_=capture_support(tuple(self.known_type_shape(t) for t in captured))
        provisional=node("ctype","Fn",mode=raw["mode"],chi=chi,projections=pi,kappa=ktrue(),
                         parameter=a,result=b,effect=self.checker.schema(state).effect(raw["effect"]),links=())
        return derive(provisional,state.pc.world,True)[0]

    def resolved(self,t):
        t=deref(t)
        if isinstance(t,Hole): return False
        if isinstance(t,Arrow):
            # Solved A/B are not a solved closure contract. Delay default chi /
            # kappa until every obtainable complete lambda signature has flowed.
            return t.concrete is not None
        if isinstance(t,RNode): return all(self.resolved(v) for _,v in t.fields)
        if isinstance(t,tuple): return all(self.resolved(v) for v in t)
        return True

    def occurs(self,h,t):
        t=deref(t)
        if t is h: return True
        if isinstance(t,Arrow): return self.occurs(h,t.parameter) or self.occurs(h,t.result)
        if isinstance(t,RNode): return any(self.occurs(h,v) for _,v in t.fields)
        if isinstance(t,tuple): return any(self.occurs(h,v) for v in t)
        return False

    def unify(self,a,b):
        if b is None: return a
        a,b=deref(a),deref(b)
        if a is b: return a
        if isinstance(a,Hole):
            require(not self.occurs(a,b),"bottom binding occurs-check conflict")
            a.binding=b; return b
        if isinstance(b,Hole): return self.unify(b,a)
        if isinstance(a,Arrow) or isinstance(b,Arrow):
            if not isinstance(a,Arrow): a,b=b,a
            require(isinstance(b,Arrow) or isinstance(b,RNode) and b.tag=="Fn","bottom binding function constraint conflict")
            bp=b.parameter if isinstance(b,Arrow) else b.get("parameter")
            br=b.result if isinstance(b,Arrow) else b.get("result")
            self.unify(a.parameter,bp);self.unify(a.result,br)
            concrete=b.concrete if isinstance(b,Arrow) else b
            if a.concrete is not None and concrete is not None: self.unify(a.concrete,concrete)
            if concrete is not None: a.concrete=concrete
            # Share the whole signature variable, not just its A/B holes. A
            # later known Fn constraint must reach every alias of this arrow.
            if isinstance(b,Arrow): b.alias=a
            return a
        if isinstance(a,RNode) and isinstance(b,RNode) and a.tag==b.tag and a.tag in ("Pair","Sum"):
            self.unify(a.get("left"),b.get("left"));self.unify(a.get("right"),b.get("right"));return a
        require(type(a) is type(b) and (not isinstance(a,RNode) or a.tag==b.tag),"bottom binding type constraint conflict")
        require(compare_types(a,b,self.refresh(self.state).pc.world).yes,"bottom binding type constraint conflict")
        return a

    def shape(self,t,tag):
        t=deref(t)
        if isinstance(t,Hole):
            t.binding=node("type",tag,left=Hole(),right=Hole());t=t.binding
        require(isinstance(t,RNode) and t.tag==tag,"bottom binding "+tag+" shape conflict")
        return t

    def parse_type(self,raw,state): return self.checker.schema(self.refresh(state)).type(raw)

    def value(self,raw,env,state,expected=None):
        self.refresh(state)
        tag=raw["value"]
        if tag=="var":
            require(raw["name"] in env,"unbound term in bottom continuation")
            t=env[raw["name"]]
        elif tag in ("unit","bool","nat","word"):
            t=node("type",{"unit":"Unit","bool":"Bool","nat":"Nat","word":"Word"}[tag])
        elif tag=="pair":
            guide=deref(expected) if expected is not None else None
            if isinstance(guide,Hole): guide=self.shape(guide,"Pair")
            left=guide.get("left") if isinstance(guide,RNode) and guide.tag=="Pair" else None
            right=guide.get("right") if isinstance(guide,RNode) and guide.tag=="Pair" else None
            t=node("type","Pair",left=self.value(raw["left"],env,state,left),right=self.value(raw["right"],env,state,right))
        elif tag in ("inl","inr"):
            t=self.parse_type(raw["sum_type"],state)
            require(isinstance(t,RNode) and t.tag=="Sum","bottom injection needs Sum")
            self.value(raw["payload"],env,state,t.get("left" if tag=="inl" else "right"))
        elif tag=="pack":
            t=self.parse_type(raw["exists_type"],state);require(isinstance(t,Binder),"bottom PACK needs Exists")
            require(len(raw["witnesses"])==len(t.sorts),"bottom PACK witness arity")
            refs=tuple(self.checker.witness(w,s,state) for w,s in zip(raw["witnesses"],t.sorts))
            _,payload=instantiate(t,refs,free_world=state.pc.world.allowed)
            self.value(raw["payload"],env,state,payload)
        elif tag=="lambda":
            a=self.parse_type(raw["parameter"]["type"],state);b=self.parse_type(raw["result"],state)
            require(all(n in env for n in raw["captures"]),"unbound capture in bottom continuation")
            captured=tuple(env[n] for n in raw["captures"])
            t=Hole()
            caller=SymbolicCaller(self.checker.site(self.context_path("lambda")))
            signature={"fn":None}
            def lambda_context():
                creator=self.refresh(state)
                fn=signature["fn"]
                if fn is None and all(self.resolved(x) for x in captured):
                    types=tuple(self.materialize(x,creator) for x in captured)
                    fn,_,_=self.checker.lambda_signature(raw,types,creator)
                if fn is not None:
                    k=fn.get("kappa")
                elif raw["requires"]!={"derive":"support"}:
                    k=self.checker.schema(creator).kappa(raw["requires"])
                else:
                    # Preserve known requirements while unknown captures are
                    # solved by this body's structural constraints. No creator
                    # assumption is borrowed and no hole is defaulted here.
                    k=self.known_lambda_kappa(raw,captured,creator,a,b)
                local=creator.clone();local.names={};local.slots={}
                local.pc=ProofContext(creator.pc.world,caller,k)
                return local
            local=self.context(lambda_context)
            inner=dict(env);inner[raw["parameter"]["name"]]=a
            self.command(raw["body"],inner,local,b)
            # A/B alone are constraints, never a replacement for this lambda's
            # full public contract. Construct that contract once capture types
            # resolve; actual creation/body/caller checking still happens later.
            def complete_lambda(default=False):
                if not default and not all(self.resolved(x) for x in captured): return False
                creator=self.refresh(state)
                types=tuple(self.materialize(x,creator) for x in captured)
                fn,_,_=self.checker.lambda_signature(raw,types,creator)
                signature["fn"]=fn
                self.refresh(local)
                self.state=state;self.unify(t,fn)
                return True
            self.deferred.append((complete_lambda,"lambda capture types need constraints",True))
        else: raise NeedsBindType("cannot infer this bottom value; provide bind_type")
        self.state=state
        self.unify(t,expected)
        return t

    def expression(self,raw,env,state):
        if raw["expression"]=="value": return self.value(raw["value"],env,state)
        if raw["expression"]=="op":
            parameters,result=self.checker.sigma.pure(raw["name"])
            require(len(parameters)==len(raw["arguments"]),"pure operation argument arity")
            for source,typ in zip(raw["arguments"],parameters): self.value(source,env,state,typ)
            return result
        pair=self.shape(self.value(raw["value"],env,state),"Pair")
        return pair.get("left" if raw["expression"]=="fst" else "right")

    def command(self,raw,env,state,expected=None):
        self.refresh(state)
        tag=raw["command"]; result=expected if expected is not None else Hole()
        if tag=="return": return self.value(raw["value"],env,state,result)
        if tag=="drop": self.value(raw["value"],env,state);return self.command(raw["next"],env,state,result)
        if tag in ("let_e","let_c"):
            bound=self.expression(raw["expression"],env,state) if tag=="let_e" else self.command(raw["first"],env,state,
                 self.parse_type(raw["bind_type"],state) if "bind_type" in raw else None)
            inner=dict(env);inner[raw["name"]]=bound
            return self.command(raw["next"],inner,state,result)
        if tag=="call":
            f=self.value(raw["function"],env,state); ft=deref(f)
            if isinstance(ft,Hole):
                ft.binding=Arrow(Hole(),result);ft=ft.binding
            require(isinstance(ft,Arrow) or isinstance(ft,RNode) and ft.tag=="Fn","bottom CALL needs Fn")
            a=ft.parameter if isinstance(ft,Arrow) else ft.get("parameter")
            b=ft.result if isinstance(ft,Arrow) else ft.get("result")
            self.value(raw["argument"],env,state,a);self.state=state;self.unify(b,result);return result
        if tag=="if":
            self.value(raw["condition"],env,state,node("type","Bool"))
            self.command(raw["then"],dict(env),state,result);self.command(raw["else"],dict(env),state,result);return result
        if tag=="case":
            summ=self.shape(self.value(raw["value"],env,state),"Sum")
            for arm in ("left","right"):
                inner=dict(env);inner[raw[arm+"_name"]]=summ.get(arm)
                self.command(raw[arm],inner,state,result)
            return result
        if tag in ("throw","rethrow"):
            self.value(raw["payload" if tag=="throw" else "failure"],env,state,node("type","ErrorPayload" if tag=="throw" else "Failure"))
            return result
        if tag=="try":
            self.command(raw["body"],dict(env),state,result)
            inner=dict(env);inner[raw["failure_name"]]=node("type","Failure")
            self.command(raw["handler"],inner,state,result);return result
        if tag=="request_cancel":
            control=self.value(raw["control"],env,state);self.dependent.append(control)
            def known_control(default=False):
                typ=deref(control)
                if isinstance(typ,Hole): return False
                require(isinstance(typ,RNode) and typ.tag=="Control","REQUEST-CANCEL needs Control")
                return True
            self.deferred.append((known_control,"CANCEL needs complete Control job",False))
            return self.command(raw["next"],env,state,result)
        if tag=="fork":
            from .fork import fork_world
            scope=self.value(raw["scope"],env,state);self.dependent.append(scope)
            path=self.context_path("fork");job=raw["job"]
            require(all(name in env for name in job["captures"]),"unbound fork capture")
            def known_fork(default=False):
                typ=deref(scope)
                if isinstance(typ,Hole): return False
                def parent_context(): return fork_world(self.checker,raw,self.refresh(state),path,typ)[0]
                def child_context(): return fork_world(self.checker,raw,self.refresh(state),path,typ)[1]
                parent=self.context(parent_context);child=self.context(child_context)
                _,_,refs,_,result_type,_=fork_world(self.checker,raw,self.refresh(state),path,typ)
                self.command(job["body"],{name:env[name] for name in job["captures"]},child,result_type)
                nxt=dict(env);nxt[raw["result_name"]]=node("type","Shared",job=refs[0],payload=result_type)
                nxt[raw["control_name"]]=node("type","Control",job=refs[0])
                self.command(raw["next"],nxt,parent,result)
                return True
            self.deferred.append((known_fork,"FORK needs complete Scope",False))
            return result
        if tag=="produce":
            from .producer import producer_world
            scope=self.value(raw["scope"],env,state);self.dependent.append(scope)
            parameter,_=self.checker.sigma.kernel(raw["kernel"])
            self.value(raw["argument"],env,state,parameter)
            path=self.context_path("produce")
            cleanup=raw["cleanup"]
            require(all(name in env for name in cleanup["captures"]),"unbound cleanup capture")
            def known_producer(default=False):
                typ=deref(scope)
                if isinstance(typ,Hole): return False
                def parent_context():
                    return producer_world(self.checker,raw,self.refresh(state),path,typ)[0]
                def child_context():
                    return producer_world(self.checker,raw,self.refresh(state),path,typ)[1]
                parent=self.context(parent_context);child=self.context(child_context)
                _,_,refs,_,result_type,read,_,_=producer_world(self.checker,raw,self.refresh(state),path,typ)
                local={name:env[name] for name in cleanup["captures"]}
                local[cleanup["parameter"]["name"]]=read
                self.command(cleanup["body"],local,child,result_type)
                nxt=dict(env);nxt[raw["result_name"]]=node("type","Shared",job=refs[0],payload=result_type)
                nxt[raw["control_name"]]=node("type","Control",job=refs[0])
                self.command(raw["next"],nxt,parent,result)
                return True
            self.deferred.append((known_producer,"PRODUCE needs complete owned Scope",False))
            return result
        if tag in ("await","await_cancel"):
            shared=self.value(raw["shared"],env,state);self.dependent.append(shared)
            def known_shared(default=False):
                typ=deref(shared)
                if isinstance(typ,Hole): return False
                label="AWAIT?" if tag=="await_cancel" else "AWAIT"
                require(isinstance(typ,RNode) and typ.tag=="Shared","bottom "+label+" needs Shared")
                payload=typ.get("payload")
                output=node("type","Sum",left=payload,right=node("type","Unit")) if tag=="await_cancel" else payload
                self.state=self.refresh(state);self.unify(result,output)
                return True
            label="AWAIT?" if tag=="await_cancel" else "AWAIT"
            self.deferred.append((known_shared,label+" needs complete Shared job and payload",False))
            return result
        if tag in ("call_service","call_tool"):
            require(self.checker.sigma.parent,"parent command requires fixed parent Sigma")
            if tag=="call_service":
                scope=self.value(raw["scope"],env,state);self.dependent.append(scope)
                parameter,output=self.checker.sigma.host_service(raw["name"])
                self.value(raw["tokens"],env,state,parameter)
                def check_scope(default=False):
                    t=deref(scope)
                    if isinstance(t,Hole):return False
                    require(isinstance(t,RNode) and t.tag=="Scope" and t.get("kind")=="plain",
                            "bottom CALL_SERVICE needs plain Scope")
                    return True
                self.deferred.append((check_scope,"CALL_SERVICE needs complete Scope",False))
            else:
                parameter,output=self.checker.sigma.tool(raw["name"])
                self.value(raw["argument"],env,state,parameter)
            # Collection is not authority. Completed body is checked again,
            # including exact private caller/current scope and effect/frame.
            self.unify(result,output);return result
        if tag=="receive_request":
            owner=self.value(raw["owner"],env,state);self.dependent.append(owner)
            self.unify(result,self.checker.sigma.host_input(raw["name"]))
            def check_owner(default=False):
                t=deref(owner)
                if isinstance(t,Hole): return False
                require(isinstance(t,RNode) and t.tag=="Owner","bottom RECEIVE_REQUEST needs Owner")
                return True
            # This is type-only collection. The completed continuation is
            # checked again by body.py, including its private INIT caller proof.
            self.deferred.append((check_owner,"RECEIVE_REQUEST needs complete Owner",False));return result
        if tag=="offer_response":
            owner=self.value(raw["owner"],env,state);self.dependent.append(owner)
            payload,output=self.checker.sigma.host_response(raw["name"])
            self.value(raw["token"],env,state,payload)
            self.unify(result,output)
            def check_response_owner(default=False):
                t=deref(owner)
                if isinstance(t,Hole): return False
                require(isinstance(t,RNode) and t.tag=="Owner","bottom OFFER_RESPONSE needs Owner")
                return True
            # Constraints infer types only; body.py rechecks INIT authority,
            # effects and frame accounting with the completed continuation.
            self.deferred.append((check_response_owner,"OFFER_RESPONSE needs complete Owner",False));return result
        if tag=="load":
            read=self.value(raw["read"],env,state)
            self.dependent.append(read)
            self.value(raw["index"],env,state,node("type","Nat"))
            self.unify(result,node("type","Word"))
            def check_read(default=False):
                t=deref(read)
                if isinstance(t,Hole): return False
                require(isinstance(t,RNode) and t.tag=="Read","bottom LOAD Read constraint conflict")
                return True
            self.deferred.append((check_read,"LOAD needs complete Read indices",False));return result
        if tag=="unpack":
            package=self.value(raw["package"],env,state)
            self.dependent.append(package)
            def open_known(default=False):
                t=deref(package)
                if isinstance(t,Hole): return False
                require(isinstance(t,Binder) and t.kind=="exists","bottom UNPACK needs Exists")
                names=tuple(old.name(n,"unpack.name") for n in raw["names"])
                require(tuple(n.sort for n in names)==t.sorts,"bottom UNPACK binder sorts")
                refs=tuple(Free(n.sort,n.text) for n in names)
                facts,payload=instantiate(t,refs,free_world=state.pc.world.allowed|frozenset(refs))
                def opened_context():
                    parent=self.refresh(state)
                    world=parent.pc.world.extend(names,facts)
                    local=parent.clone()
                    # Match actual UNPACK's value-specific atomic assumptions.
                    # Hidden binders / inactive Sum arms remain opaque under
                    # point_requirements; this is no universal package proof.
                    point=self.checker.point_requirements(payload,world)
                    local.pc=ProofContext(world,parent.pc.caller,
                                          kall((parent.pc.assumptions,point)),parent.pc.evidence)
                    if local.source_names is not None: local.source_names+=names
                    return local
                local=self.context(opened_context)
                inner=dict(env);inner[raw["name"]]=payload
                self.command(raw["body"],inner,local,result)
                return True
            self.deferred.append((open_known,"UNPACK needs complete Exists Phi and payload",False));return result
        if tag=="new_owner":
            from .generation import new_owner_world
            self.value(raw["value"],env,state,node("type","Nat"))
            path=self.context_path("new_owner")
            def new_context():
                return new_owner_world(self.checker,raw,self.refresh(state),path)[0]
            local=self.context(new_context)
            refs=tuple(Free(n["sort"],n["name"]) for n in raw["names"])
            inner=dict(env);inner[raw["name"]]=node("type","Owner",owner=refs[1],size=refs[0])
            self.command(raw["body"],inner,local,result)
            return result
        if tag in ("scope","run"):
            from .generation import scope_world
            owner=self.value(raw["owner"],env,state) if tag=="run" else None
            path=self.context_path(tag)
            def scoped(default=False):
                t=deref(owner) if owner is not None else None
                if isinstance(t,(Hole,Arrow)): return False
                if t is not None: require(isinstance(t,RNode) and t.tag=="Owner","bottom RUN needs Owner")
                def scoped_context():
                    return scope_world(self.checker,raw,self.refresh(state),path,t)[0]
                local=self.context(scoped_context)
                origin=local.pc.caller.origin
                inner=dict(env);inner[raw["name"]]=node("type","Scope",scope=origin.get("scope"),
                                                       region=origin.get("region"),kind=origin.get("kind"))
                self.command(raw["body"],inner,local,result)
                return True
            self.deferred.append((scoped,"RUN needs complete Owner indices" if tag=="run" else "SCOPE body constraints",False))
            return result
        raise NeedsBindType("bottom continuation needs a type annotation")

    def solve_deferred(self):
        pending=self.deferred;self.deferred=[]
        while pending:
            next_pending=[];progress=False
            for action,reason,may_default in pending:
                if action(): progress=True
                else: next_pending.append((action,reason,may_default))
            next_pending.extend(self.deferred);self.deferred=[]
            if next_pending and not progress:
                # Only unconstrained structural leaves may receive the Unit
                # witness. A hole used as a dependent Read/Exists is protected.
                defaults=[item for item in next_pending if item[2]]
                if not defaults:
                    raise NeedsBindType("insufficient inference information: "+next_pending[0][1]+"; provide bind_type")
                next_pending=[item for item in next_pending if not item[2]]
                for action,_,_ in defaults: action(True)
                next_pending.extend(self.deferred);self.deferred=[]
            pending=next_pending

    def materialize(self,t,state):
        t=deref(t)
        if isinstance(t,Hole):
            if any(deref(x) is t for x in self.dependent):
                raise NeedsBindType("insufficient inference information for dependent interface; provide bind_type")
            self.defaulted=True;t.binding=node("type","Unit");return t.binding
        if isinstance(t,Arrow):
            if t.concrete is not None: return t.concrete
            a=self.materialize(t.parameter,state);b=self.materialize(t.result,state)
            fn=node("ctype","Fn",mode="U",chi=node("support","zero"),
                    projections=node("projection","contract",selectors=(),entries=()),kappa=ktrue(),
                    parameter=a,result=b,effect=node("effect","set",atoms=()),links=())
            k,links,_=derive(fn,state.pc.world,True)
            return RNode("ctype","Fn",tuple((key,k if key=="kappa" else links if key=="links" else value) for key,value in fn.fields))
        if isinstance(t,RNode) and t.tag in ("Pair","Sum"):
            return node("type",t.tag,left=self.materialize(t.get("left"),state),right=self.materialize(t.get("right"),state))
        return t


def infer_bottom_type(checker, state, name, continuation, expected):
    root=Hole();constraints=Constraints(checker,state)
    env={n:state.slots[b].type for n,b in state.names.items() if b in state.slots}
    env[name]=root
    constraints.command(continuation,env,state,expected)
    constraints.solve_deferred()
    return constraints.materialize(root,state)
