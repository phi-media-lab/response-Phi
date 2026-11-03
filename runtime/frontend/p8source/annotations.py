"""Compact source records attached only to returned derivations, never a global log."""
from dataclasses import replace
from p8foundation.model import RNode,node
from p8closure.support import copyable

INIT_LABELS=("main","task","main_invocation","initial_invocation","host_region","main_region","initial_scope","initial_region")

def nominal_context(state):
    entries=[]
    init=state.pc.world.init_origin
    if init is not None:
        entries.extend((init.get(k),init.get("site")[:1]+("main","body"),"init-private",i) for i,k in enumerate(INIT_LABELS))
    for ref,opening,slot in state.origins:
        entries.append((ref,opening.get("site"),"unpack",slot))
    for ref,origin,slot in state.generations:
        # RUN scopes share a ScopeOrigin; the source discriminator decides its
        # role. No display spelling or execution counter is used as identity.
        entries.append((ref,origin.get("site"),"source-generation",slot))
    return tuple(entries)

def begin(checker,raw,family,state,path):
    row={"raw":raw,"family":family,"path":path,"bindings":[],"nominals":list(nominal_context(state)),
         "environment":tuple((name,bid,state.slots[bid].type) for name,bid in state.names.items() if bid in state.slots),
         "execution":state.execution}
    checker.annotation_stack.append(row)
    return row

def bound(checker,state,name,binder,value):
    if checker.annotation_stack:
        row=checker.annotation_stack[-1]
        row["bindings"].append((name,binder,value.type))
        for item in nominal_context(state):
            if item not in row["nominals"]: row["nominals"].append(item)

def complete(checker,row,result):
    family=row["family"];raw=row["raw"]
    if family=="command":
        d=result.derivation
        variants=tuple(tuple(sorted(n.state.affine(),key=repr)) for n in result.normal)
        remaining=variants[0] if variants else None
    else:
        value,state,d=result;remaining=tuple(sorted(state.affine(),key=repr));variants=(remaining,)
    copy=[];move=[]
    env={name:(bid,t) for name,bid,t in row["environment"]}
    direct=[]
    if family=="value" and raw[family]=="var": direct=[raw["name"]]
    if family=="value" and raw[family]=="lambda": direct=raw["captures"]
    if family=="command" and raw[family]=="produce": direct=raw["cleanup"]["captures"]
    if family=="command" and raw[family]=="fork": direct=raw["job"]["captures"]
    for name in direct:
        bid,typ=env[name];(copy if copyable(typ) else move).append(bid)
    record=node("annotation","source",site=checker.site(row["path"]),node_kind=family+":"+raw[family],
                execution=row["execution"],bindings=tuple(row["bindings"]),nominals=tuple(row["nominals"]),
                environment=row["environment"],copy=tuple(copy),move=tuple(move),remaining=remaining,remaining_variants=variants)
    derived=RNode(d.family,d.tag,d.fields+(("source_record",record),))
    return replace(result,derivation=derived) if family=="command" else (value,state,derived)

def checked(checker,raw,family,state,path,action):
    row=begin(checker,raw,family,state,path)
    try: return complete(checker,row,action())
    finally:
        if not checker.annotation_stack or checker.annotation_stack[-1] is not row:
            raise RuntimeError("source annotation stack identity")
        checker.annotation_stack.pop()
