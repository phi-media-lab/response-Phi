"""Finite caller provenance. A synchronous scope step is never a child job."""
from dataclasses import dataclass
from p8foundation.model import Free, node, require
from .support import Caller


@dataclass(frozen=True)
class SymbolicCaller:
    binder: tuple


@dataclass(frozen=True)
class InScope:
    parent: object
    origin: object


def field(caller,name):
    require(name in ("job","task","invocation","region","scope_stack"),"caller field")
    if isinstance(caller,Caller): return getattr(caller,name)
    if isinstance(caller,SymbolicCaller):
        return node("context","field",binder=caller.binder,field=name)
    require(isinstance(caller,InScope),"formed execution context required")
    if name in ("job","task"): return field(caller.parent,name)
    if name in ("region","invocation"): return caller.origin.get(name)
    return node("context","stack-cons",scope=caller.origin.get("scope"),tail=field(caller.parent,"scope_stack"))


def key(caller):
    if isinstance(caller,SymbolicCaller): return node("caller","symbolic",binder=caller.binder)
    if isinstance(caller,Caller):
        return node("caller","actual",job=caller.job,task=caller.task,invocation=caller.invocation,
                    region=caller.region,scope_stack=caller.scope_stack)
    require(isinstance(caller,InScope),"formed execution context required")
    return node("caller","in-scope",parent=key(caller.parent),origin=caller.origin,
                job=field(caller,"job"),task=field(caller,"task"),invocation=field(caller,"invocation"),
                region=field(caller,"region"),scope_stack=field(caller,"scope_stack"))


def scope_step(pc,site,scope,region,invocation,kind,execution):
    require(scope.sort=="Scope" and region.sort=="Region" and invocation.sort=="Invocation","scope step sorts")
    require(all(isinstance(r,Free) and r not in pc.world.allowed for r in (scope,region,invocation)),
            "scope step requires source-fresh nominal identities")
    parent=key(pc.caller)
    origin=node("origin","scope",site=site,scope=scope,region=region,invocation=invocation,kind=kind,
                parent=parent,parent_region=field(pc.caller,"region"),parent_invocation=field(pc.caller,"invocation"),
                job=field(pc.caller,"job"),task=field(pc.caller,"task"),parent_stack=field(pc.caller,"scope_stack"),
                execution=execution,runtime_entered=False)
    child=InScope(pc.caller,origin)
    step=node("proof","ScopeStep",parent=parent,child=key(child),origin=origin,
              same_job=field(child,"job")==field(pc.caller,"job"),
              same_task=field(child,"task")==field(pc.caller,"task"),direction="child-covered-by-parent")
    require(step.get("same_job") and step.get("same_task"),"synchronous step changes job/Task")
    return child,origin,step


def transport_assumptions(pc,child,step):
    require(step.get("parent")==key(pc.caller) and step.get("child")==key(child),"ScopeStep subject mismatch")
    return node("proof","KAt",subject=key(child),requirement=pc.assumptions,
                evidence=node("proof","LiftKappa",step=step,parent=pc.at(pc.assumptions)))


def substitute_caller_record(value,binder,replacement):
    """Substitute free context references in inspectable records, not nominal IDs.

    Nested self binders have different source identities. Their own fields stay
    rigid while free references to this outer binder inside captures/origins
    are traversed. Generated runtime-name realization remains a separate step.

    The checked records form an immutable DAG. Memoize only within this one
    fixed binder/replacement invocation, by retained input identity. Repeated
    edges reuse a complete transformed value; equal distinct inputs do not
    merge. No proof field, child edge, or nested binder is skipped.
    """
    from p8foundation.model import RNode, Binder, Decision
    memo={}
    def visit(item):
        if not isinstance(item,(RNode,Binder,Decision,tuple)): return item
        previous=memo.get(id(item))
        if previous is not None:
            if previous[0] is not item: raise RuntimeError("caller substitution memo lost retained input")
            return previous[1]
        if isinstance(item,RNode):
            fields=dict(item.fields)
            if item.family=="caller" and item.tag=="symbolic" and fields.get("binder")==binder:
                result=key(replacement)
            elif item.family=="context" and item.tag=="field" and fields.get("binder")==binder:
                result=field(replacement,fields["field"])
            else: result=RNode(item.family,item.tag,tuple((k,visit(v)) for k,v in item.fields))
        elif isinstance(item,Binder):
            result=Binder(item.kind,item.sorts,tuple(visit(f) for f in item.facts),visit(item.body))
        elif isinstance(item,Decision):
            result=Decision(item.outcome,item.rule,visit(item.details),visit(item.premises))
        else: result=tuple(visit(v) for v in item)
        memo[id(item)]=(item,result)
        return result
    try: return visit(value)
    finally: memo.clear()
