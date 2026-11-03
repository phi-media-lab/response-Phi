"""Source-generated interfaces and lexical exits; no allocation is performed."""
from dataclasses import replace
from p8static import parser as old
from p8foundation.model import Free, LiteralSize, Binder, RNode, FoundationError, node, require
from p8foundation.algebra import free_names
from p8closure.formation import form_world, form_type, form_effect
from p8closure.requirements import close_refs, caller_key, Unsupported


def fresh_names(raw,state,sorts):
    names=tuple(old.name(n,"generation.name") for n in old.sequence(raw["names"],"generation.names"))
    require(tuple(n.sort for n in names)==tuple(sorts),"generation ordered name sorts")
    require(len({n.text for n in names})==len(names),"duplicate generation name")
    require(not {n.text for n in names}&{n.text for n in state.pc.world.names},"generation names must be fresh")
    return names,tuple(Free(n.sort,n.text) for n in names)


def close_interface(kind,refs,facts,body):
    return Binder(kind,tuple(r.sort for r in refs),tuple(close_refs(f,refs) for f in facts),close_refs(body,refs))


def new_owner_world(checker,raw,state,path):
    names,refs=fresh_names(raw,state,("Size","Owner"))
    n,rho=refs
    operand=raw["value"]
    literal=operand["literal"] if operand["value"]=="nat" else None
    route="zero" if literal==0 else "positive-literal" if literal is not None else "dynamic-nat"
    facts=(node("relation","positiveSize",size=n),node("relation","owner",owner=rho,size=n))
    if route=="positive-literal": facts+=(node("relation","sizeEqual",left=n,right=LiteralSize(literal)),)
    world=state.pc.world.extend(names,facts);form_world(world)
    inner=state.clone();inner.pc=state.pc.in_world(world)
    if inner.source_names is not None: inner.source_names+=names
    if route=="zero": inner.execution="unreachable-success-template"
    interface=close_interface("generation-new",refs,facts,node("type","Unit"))
    origin=node("generation","new-owner",site=checker.site(path),route=route,execution=inner.execution,
                witnesses=refs,interface=interface,caller=caller_key(state.pc.caller),runtime_realized=False)
    inner.generations+=tuple((ref,origin,i) for i,ref in enumerate(refs))
    return inner,names,refs,facts,origin


def close_generated_body(checker,body,outer,refs,origin,path,expected,*,retarget_caller=True):
    """Close histories, never treat closed nominal names as current authority."""
    normals=[];exceptional=[];hidden=frozenset(refs)
    if body.type is not None:
        require(not free_names(body.type)&hidden,"generation result name escape")
        if expected is not None: checker.same(body.type,expected,outer,"generation result type")
    for normal in body.normal:
        checker.check_hold(normal.value,normal.state)
        require(not free_names(normal.value.type)&hidden,"generation result name escape")
        state=normal.state.clone()
        require(not any(free_names(v.type)&hidden for v in state.slots.values()),"generation remaining slot escape")
        if retarget_caller: state.pc=outer.pc
        state.origins=outer.origins;state.source_names=outer.source_names
        state.generations=outer.generations;state.execution=outer.execution
        view=node("view","ClosedGeneration",site=checker.site(path),generation=origin,body=normal.value.view)
        value=checker.made(normal.value.type,view,state,"generation-lexical-close",(normal.value.hold,),
                           node("implementation","closed-generation",generation=origin,body=normal.value.implementation))
        normals.append(replace(normal,value=value,state=state))
    for ex in body.exceptional:
        state=ex.state.clone()
        require(not any(free_names(v.type)&hidden for v in state.slots.values()),"generation exceptional slot escape")
        state.pc=outer.pc;state.origins=outer.origins;state.source_names=outer.source_names
        state.generations=outer.generations;state.execution=outer.execution
        frames=ex.frames;abandoned=ex.abandoned
        if free_names(frames)&hidden: frames=(node("frame","ClosedGeneration",generation=origin,frames=frames),)
        if free_names(abandoned)&hidden: abandoned=(node("abandon","ClosedGeneration",generation=origin,values=abandoned),)
        exceptional.append(replace(ex,state=state,frames=frames,abandoned=abandoned,
                            origin=node("failure","closed-generation",generation=origin,origin=ex.origin)))
    return replace(body,normal=tuple(normals),exceptional=tuple(exceptional))


def check_new_owner(checker,raw,state,path,expected):
    from .body import base, effect, union, Exceptional
    operand,state,operand_d=checker.value(raw["value"],state,path+("value",))
    require(operand.type==base("Nat"),"NEW operand must be Nat")
    inner,names,refs,facts,origin=new_owner_world(checker,raw,state,path)
    n,rho=refs
    owner_type=node("type","Owner",owner=rho,size=n);form_type(owner_type,inner.pc.world)
    view=node("view","GeneratedOwner",site=checker.site(path),generation=origin)
    owner=checker.made(owner_type,view,inner,"new-owner-success-assumption",(origin,))
    binder,prior=checker.bind(inner,raw["name"],owner,path,"owner")
    body_path=path+("body",)
    try:
        body=checker.command(raw["body"],inner,body_path,None)
        if not body.normal and expected is not None:
            body=checker.command(raw["body"],inner,body_path,expected)
        body=checker.close_binding(body,raw["name"],binder,prior)
        checker.path=body_path
        closed=close_generated_body(checker,body,state,refs,origin,path,expected)
    except (FoundationError,old.ParseError,Unsupported) as error:
        if origin.get("route")=="zero":
            # Keep the original class and source path; this is a body failure,
            # never a static rejection of Nat zero itself.
            error.args=("unreachable success template body: "+str(error),)
        raise
    eps=union(effect(node("atom","Alloc"),node("atom","Raise")),close_interface("nu",refs,facts,body.effect))
    form_effect(eps,state.pc.world)
    frame=node("frame","GenBind",site=checker.site(path),caller=caller_key(state.pc.caller),
               interface=origin.get("interface"),remaining=state.snapshot(),
               success="move-once-to-body",failure="unwind-without-installing-binders")
    suspended=state.clone()
    for bid in suspended.affine(): del suspended.slots[bid]
    route=origin.get("route")
    causes=("parameter",) if route=="zero" else ("allocation",) if route=="positive-literal" else ("parameter","allocation")
    pre_failures=tuple(Exceptional(suspended.clone(),node("failure","new-owner-"+cause,
                       site=checker.site(path),caller=caller_key(state.pc.caller)),(),(frame,)) for cause in causes)
    ghost=None
    if route=="zero":
        ghost=node("derivation","unreachable-success-template",site=checker.site(path),
                   interface=origin.get("interface"),body=body.derivation,
                   exports_normal=False,exports_premises=False,exports_frames=False)
        normals=();exceptional=pre_failures;children=(operand_d,)
    else:
        normals=closed.normal;exceptional=pre_failures+closed.exceptional;children=(operand_d,body.derivation)
    detail=node("generation","checked-new",origin=origin,interface=origin.get("interface"),
                ghost_child=ghost,success_realized=False)
    return checker.finish("new_owner",path,expected or body.type,eps,normals,exceptional,children,(frame,),detail)


def scope_world(checker,raw,state,path,owner_type=None):
    from p8closure.caller import scope_step, transport_assumptions
    from p8closure.requirements import ProofContext, prove
    run=raw["command"]=="run"
    sorts=("Epoch","Scope","Region","Invocation") if run else ("Scope","Region","Invocation")
    names,refs=fresh_names(raw,state,sorts)
    epoch_origin=None
    if run:
        require(isinstance(owner_type,RNode) and owner_type.tag=="Owner","RUN needs complete Owner type")
        epoch,scope,region,invocation=refs;rho=owner_type.get("owner");kind=(rho,epoch)
        epoch_origin=node("origin","fresh-epoch",site=checker.site(path),epoch=epoch,owner=rho,
                          parent=caller_key(state.pc.caller),execution=state.execution,
                          acquire_success_required=True,runtime_realized=False,
                          realization=node("obligation","epoch-realization",active_before="none",ordinal="lastOrdinal+1",
                                           fresh_brand_each_activation=True,atomic_scope_commit=True,busy_creates=False))
        facts=(node("relation","epoch_owner",epoch=epoch,owner=rho),)
    else:
        scope,region,invocation=refs;kind="plain";facts=()
    facts+=(node("relation","scope_shape",scope=scope,region=region,kind=kind),)
    caller,origin,step=scope_step(state.pc,checker.site(path),scope,region,invocation,kind,state.execution)
    # Only lexical nominal targets with an actual parent proof get a local
    # persistent edge. Hidden universal skolems are not exported from prove.
    edges=[]
    for target in sorted((r for r in state.pc.world.allowed if r.sort=="Region"),key=lambda r:r.name):
        goal=node("kappa","covered",region=target)
        proof=prove(goal,state.pc)
        if proof.yes:
            relation=node("relation","outlives",inner=region,outer=target)
            edges.append(node("proof","ScopeCoverageEdge",relation=relation,step=step,
                              parent=node("proof","KAt",subject=caller_key(state.pc.caller),requirement=goal,evidence=proof)))
    world=state.pc.world.extend(names,facts+tuple(p.get("relation") for p in edges),
                               scope_origins=(origin,),epoch_origins=(epoch_origin,) if run else (),region_origins=tuple(edges))
    form_world(world)
    lifted=transport_assumptions(state.pc,caller,step)
    inner=state.clone();inner.pc=ProofContext(world,caller,state.pc.assumptions,(lifted,step))
    if inner.source_names is not None: inner.source_names+=names
    inner.generations+=tuple((ref,origin,i) for i,ref in enumerate(refs))
    return inner,names,refs,facts,origin,step,epoch_origin


def scope_effect_export(checker,body_effect,outer,inner,origin,step):
    """E1: deterministic public projection of this entry's proved edges only."""
    order=lambda r:(r.sort,r.name)
    references=tuple(sorted(free_names(body_effect),key=order))
    mappings=tuple(inner.pc.world.scope_mapping(r) for r in references if r.sort=="Scope")
    relevant=frozenset(r for r in references if r.sort=="Region")|frozenset(m.get("region") for m in mappings)
    region=origin.get("region")
    visible=frozenset(Free(n.sort,n.text) for n in checker.source_resolver(outer).names)
    candidates=tuple(p for p in inner.pc.world.region_origins
                     if p.get("step")==step and p.get("relation").get("inner")==region)
    candidates=tuple(sorted(candidates,key=lambda p:order(p.get("relation").get("outer"))))
    selected=[];relations=[]
    if region in relevant:
        for edge in candidates:
            relation=edge.get("relation");target=relation.get("outer")
            if target not in relevant or target not in outer.pc.world.allowed: continue
            # A public Scope may indirectly mention a private lifetime. Never
            # silently omit an E1-relevant proven edge or reveal an INIT name.
            require(target in visible,"scope effect interface gap: mapped external Region is not source-visible")
            if relation not in relations:
                selected.append(edge);relations.append(relation)
    return node("proof","ScopeEffectExport",policy="E1",body_effect=body_effect,
                free_references=references,scope_mappings=mappings,
                relevant_regions=tuple(sorted(relevant,key=order)),candidate_edges=candidates,
                selected_edges=tuple(selected),relations=tuple(relations))


def check_scope(checker,raw,state,path,expected):
    from .body import base, effect, union, Normal, Exceptional
    from .binding import free_terms
    from p8closure.support import copyable, stable
    run=raw["command"]=="run"
    children=[];owner_type=None
    if run:
        owner,state,owner_d=checker.value(raw["owner"],state,path+("owner",))
        owner_type=owner.type
        require(isinstance(owner_type,RNode) and owner_type.tag=="Owner","RUN operand must be Owner")
        form_type(owner_type,state.pc.world);children.append(owner_d)
    affine=state.affine()
    forbidden={name for name,bid in state.names.items() if bid in affine}
    require(not (free_terms(raw["body"])-{raw["name"]})&forbidden,"scope body cannot capture outer affine frame")
    inner,names,refs,facts,origin,step,epoch_origin=scope_world(checker,raw,state,path,owner_type)
    for bid in affine: del inner.slots[bid]
    transported=[]
    for bid,value in tuple(inner.slots.items()):
        checker.check_hold(value,state)
        require(copyable(value.type),"scope entry copies only Gamma")
        view=node("view","ScopeEntry",parent=value.view,origin=origin,step=step)
        transport=node("proof","ScopeHoldTransport",step=step,source=value.record(),
                       type=value.type,source_view=value.view,target_view=view)
        result=checker.made(value.type,view,inner,"scope-hold-transport",(value.hold,transport),value.implementation)
        inner.slots[bid]=result
        transported.append(node("transport","entry",binder=bid,source=value.record(),target=result.record(),proof=transport))
    scope=origin.get("scope");region=origin.get("region");kind=origin.get("kind")
    scope_type=node("type","Scope",scope=scope,region=region,kind=kind);form_type(scope_type,inner.pc.world)
    scope_value=checker.made(scope_type,node("view","GeneratedScope",origin=origin),inner,"source-generated-scope",(step,))
    binder,prior=checker.bind(inner,raw["name"],scope_value,path,"scope")
    body=checker.command(raw["body"],inner,path+("body",),None)
    if not body.normal and expected is not None: body=checker.command(raw["body"],inner,path+("body",),expected)
    body=checker.close_binding(body,raw["name"],binder,prior)
    checker.path=path+("body",)
    if body.type is not None: require(stable(body.type),"scope result must be Stable")
    for normal in body.normal:
        require(not normal.state.affine(),"scope body affine remainder requires explicit drop")
    closed=close_generated_body(checker,body,state,refs,origin,path,expected,retarget_caller=False)
    coverage_export=scope_effect_export(checker,body.effect,state,inner,origin,step)
    facts+=coverage_export.get("relations")
    interface=close_interface("generation-run" if run else "generation-scope",refs,facts,base("Unit"))
    frame=node("frame","ScopeExit",site=checker.site(path),scope=scope,parent=caller_key(state.pc.caller),
               body_caller=caller_key(inner.pc.caller),remaining=state.snapshot(),
               copy_slots=tuple(node("slot","copy",binder=bid,value=value.record()) for bid,value in state.slots.items() if copyable(value.type)),
               prerequisite="drain-ownjoin-and-backend-return" if run else "drain-and-joined",
               backend_return_contract=checker.sigma.backend_return() if run else None,
               owned_release=epoch_origin,completed=False,interface=interface)
    normals=[]
    for n in closed.normal:
        restored=state.clone()
        view=node("view","ScopeResult",parent=n.value.view,origin=origin,exit_plan=frame)
        proof=node("proof","StableScopeResult",type=n.value.type,source=n.value.record(),exit_plan=frame,
                   requires_completed_exit=True)
        value=checker.made(n.value.type,view,restored,"scope-result-after-exit",(n.value.hold,proof),n.value.implementation)
        normals.append(Normal(value,restored,n.drops))
    # Each exceptional alternative keeps its one ScopeExit until actual drain.
    # Closing names packages history; it does not discharge this frame.
    pending=node("frame","ClosedGeneration",generation=origin,frames=(frame,))
    exceptional=[]
    for ex in closed.exceptional:
        suspended=state.clone()
        for bid in affine: del suspended.slots[bid]
        exceptional.append(replace(ex,state=suspended,frames=ex.frames+(pending,)))
    if body.normal:
        suspended=state.clone()
        for bid in affine: del suspended.slots[bid]
        failure=node("failure","scope-finalize-existing",generation=origin,creates_fault=False,
                     source="body-or-registered-obligations",unknown_returns=False)
        exceptional.append(Exceptional(suspended,failure,(),(pending,)))
    if run:
        busy=node("failure","admission-busy",site=checker.site(path),caller=caller_key(state.pc.caller),
                  owner=owner_type.get("owner"),creates_epoch=False,creates_scope=False)
        # Failed acquire never entered the new scope and has no ScopeExit. Its
        # current affine residuals unwind, distinct from successful entry.
        exceptional.insert(0,checker.exception(state,busy))
    local=union(effect(node("atom","Enter",scope=scope),node("atom","Drain",scope=scope),node("atom","Raise")),body.effect)
    eps=close_interface("nu",refs,facts,local)
    if run: eps=union(effect(node("atom","Raise")),eps)
    form_effect(eps,state.pc.world)
    detail=node("generation","checked-scope",origin=origin,interface=interface,step=step,
                entry_holds=tuple(transported),exit_plan=frame,epoch_origin=epoch_origin,coverage_export=coverage_export)
    return checker.finish(raw["command"],path,expected or body.type,eps,normals,exceptional,
                          tuple(children)+(body.derivation,),(frame,),detail)
