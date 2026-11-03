"""Source PRODUCE/CANCEL: checked fresh interfaces, never runtime registration."""
from p8foundation.model import LiteralSize, RNode, node, require
from p8foundation.resolve import Resolver
from p8closure.schema import Schema
from p8closure.formation import form_world, form_type, form_effect, one
from p8closure.support import Caller, copyable, stable
from p8closure.requirements import ProofContext, caller_key, ktrue, holding, require_proof
from .binding import free_terms
from .generation import fresh_names, close_interface, close_generated_body

SORTS=("Job","Task","Invocation","Region","Access","Access","Region")

def kernel_write_config(checker,world,origin,refs):
    """Private kernel-context formation; no source constructor can call this."""
    p,t,i,rP,aW,_,_=refs;rho=origin.get("owner");gamma=origin.get("epoch")
    parameter,size=checker.sigma.kernel(origin.get("kernel"))
    one(world,"producerOf",job=p,scope=origin.get("scope"),owner=rho,epoch=gamma)
    one(world,"access",access=aW,job=p,owner=rho,epoch=gamma,region=rP)
    require(world.epoch_association(gamma).get("owner")==rho,"kernel Write epoch owner mismatch")
    require(world.size_equal(one(world,"owner",owner=rho).get("size"),LiteralSize(size)).yes,"kernel Write size mismatch")
    write=node("type","Write",owner=rho,epoch=gamma)
    return node("kernel","KernelWriteConfig",site=origin.get("site"),kernel=origin.get("kernel"),
                caller=caller_key(Caller(p,t,i,rP,())),access=aW,type=write,parameter=parameter,
                size=LiteralSize(size),origin=origin,source_constructible=False,runtime_issued=False)

def producer_world(checker,raw,state,path,scope_type):
    require(isinstance(scope_type,RNode) and scope_type.tag=="Scope","PRODUCE needs Scope")
    s,rs,kind=(scope_type.get(k) for k in ("scope","region","kind"))
    require(type(kind) is tuple and len(kind)==2,"PRODUCE needs owned Scope")
    rho,gamma=kind
    parameter,size=checker.sigma.kernel(raw["kernel"])
    owner=one(state.pc.world,"owner",owner=rho)
    require(state.pc.world.size_equal(owner.get("size"),LiteralSize(size)).yes,"kernel requires static Owner size="+str(size))
    names,refs=fresh_names(raw,state,SORTS)
    p,t,i,rP,aW,aC,rC=refs
    source_names=state.pc.world.names if state.source_names is None else state.source_names
    result=Schema(Resolver(source_names+names)).type(raw["cleanup"]["result"])
    facts=(node("relation","job",job=p,task=t,boundary=node("boundary","ByScope",scope=s),result=result),
           node("relation","result",job=p,result=result),node("relation","jobRegion",job=p,region=rP),
           node("relation","bodyRegion",job=p,role="cleanup",region=rC),
           node("relation","producerOf",job=p,scope=s,owner=rho,epoch=gamma),
           node("relation","access",access=aW,job=p,owner=rho,epoch=gamma,region=rP),
           node("relation","access",access=aC,job=p,owner=rho,epoch=gamma,region=rC),
           node("relation","outlives",inner=rP,outer=rs),node("relation","outlives",inner=rC,outer=rP))
    world=state.pc.world.extend(names,facts);form_world(world);form_type(result,world)
    require(stable(result),"PRODUCE cleanup result must be Stable")
    child=Caller(p,t,i,rC,(s,));child.validate(world)
    interface=close_interface("generation-produce",refs,facts,node("type","Unit"))
    origin=node("generation","producer",site=checker.site(path),interface=interface,
                parent=caller_key(state.pc.caller),child=caller_key(child),scope=s,owner=rho,epoch=gamma,
                kernel=raw["kernel"],captures=tuple(raw["cleanup"]["captures"]),
                execution=state.execution,runtime_realized=False)
    parent=state.clone();parent.pc=state.pc.in_world(world)
    if parent.source_names is not None: parent.source_names+=names
    parent.generations+=tuple((ref,origin,index) for index,ref in enumerate(refs))
    inner=parent.clone();inner.names={};inner.slots={}
    inner.pc=ProofContext(world,child,ktrue())
    read=node("type","Read",access=aC,owner=rho,epoch=gamma,region=rC);form_type(read,world)
    return parent,inner,refs,facts,result,read,origin,parameter

def check_produce(checker,raw,state,path,expected):
    from .body import effect,union,Exceptional
    scope,state,ds=checker.value(raw["scope"],state,path+("scope",))
    argument,state,da=checker.value(raw["argument"],state,path+("argument",))
    checker.check_hold(scope,state);checker.check_hold(argument,state)
    parent,inner,refs,facts,result,read_type,origin,parameter=producer_world(checker,raw,state,path,scope.type)
    kernel_config=kernel_write_config(checker,inner.pc.world,origin,refs)
    checker.same(argument.type,parameter,state,"kernel argument type")
    require(stable(argument.type),"kernel argument must be Stable")
    cleanup=raw["cleanup"];captures=tuple(cleanup["captures"]);a=cleanup["parameter"]["name"]
    require(len(captures)==len(set(captures)),"duplicate cleanup capture")
    require(set(captures)==set(free_terms(cleanup["body"])-{a}),"exact cleanup free captures mismatch")
    capture_records=[]
    for index,name in enumerate(captures):
        require(name in state.names and state.names[name] in state.slots,"cleanup capture unavailable: "+name)
        source=state.slots[state.names[name]];checker.check_hold(source,state)
        require(copyable(source.type),"cleanup cannot capture affine value")
        direct_scope=isinstance(source.type,RNode) and source.type.tag=="Scope"
        require(stable(source.type) or direct_scope,"cleanup capture must be Stable or direct Scope")
        lifetime=()
        if direct_scope:
            proof=inner.pc.world.entails(node("relation","outlives",inner=refs[3],outer=source.type.get("region")))
            require_proof(proof,"cleanup captured Scope lifetime");lifetime=(proof,)
        proof=holding(source.type,inner.pc)
        view=node("view","ProducerCaptureEntry",site=checker.site(path),index=index,actual=source.view,origin=origin)
        value=checker.made(source.type,view,inner,"producer-capture-entry",(source.hold,proof)+lifetime,source.implementation)
        binder,_=checker.bind(inner,name,value,path+("cleanup","entry","capture",index),"capture")
        capture_records.append(node("capture","checked",name=name,binder=binder,
                                    outer_binder=state.names[name],value=value.record()))
    read=checker.made(read_type,node("view","ProducerReadEntry",site=checker.site(path),origin=origin),
                      inner,"producer-cleanup-read",(origin,))
    checker.bind(inner,a,read,path+("cleanup","entry","parameter"),"cleanup-parameter")
    body=checker.command(cleanup["body"],inner,path+("cleanup","body"),result)
    for normal in body.normal:
        checker.check_hold(normal.value,normal.state)
        require(stable(normal.value.type),"cleanup result must be Stable")
        require(not normal.state.affine(),"cleanup normal affine remainder requires explicit drop")
    p=refs[0];bindings=[]
    for name,tag in ((raw["result_name"],"Shared"),(raw["control_name"],"Control")):
        typ=node("type",tag,job=p,**({"payload":result} if tag=="Shared" else {}));form_type(typ,parent.pc.world)
        value=checker.made(typ,node("view","Produced"+tag,site=checker.site(path),origin=origin),parent,
                           "producer-registration-success",(origin,))
        bid,prior=checker.bind(parent,name,value,path,tag.lower());bindings.append((name,bid,prior))
    nxt=checker.command(raw["next"],parent,path+("next",),expected)
    for name,bid,prior in reversed(bindings): nxt=checker.close_binding(nxt,name,bid,prior)
    closed=close_generated_body(checker,nxt,state,refs,origin,path,expected)
    frame=node("frame","GenBind",site=checker.site(path),caller=caller_key(state.pc.caller),
               interface=origin.get("interface"),remaining=state.snapshot(),success="move-once-to-next",
               failure="unwind-without-installing-binders")
    suspended=state.clone()
    for bid in suspended.affine(): del suspended.slots[bid]
    failure=Exceptional(suspended,node("failure","registration",site=checker.site(path),caller=caller_key(state.pc.caller)),(),(frame,))
    atoms=effect(node("atom","Register",scope=origin.get("scope")),node("atom","Issue",owner=origin.get("owner")),
                 node("atom","Read",owner=origin.get("owner")),node("atom","Raise"))
    eps=close_interface("nu",refs,facts,union(body.effect,nxt.effect,atoms));form_effect(eps,state.pc.world)
    finish=node("frame","JobFinish",site=checker.site(path),job=p,task=refs[1],role="cleanup",
                source_region=refs[6],job_region=refs[3],runtime_exit=caller_key(Caller(p,refs[1],refs[2],refs[3],())),
                remaining=(),completed=False)
    detail=node("generation","checked-produce",origin=origin,interface=origin.get("interface"),
                captures=tuple(capture_records),job_finish=finish,kernel_config=kernel_config,child_outcomes=tuple(e.record() for e in body.exceptional),
                success_realized=False)
    return checker.finish("produce",path,nxt.type,eps,closed.normal,(failure,)+closed.exceptional,
                          (ds,da,body.derivation,nxt.derivation),(frame,),detail)

def check_cancel(checker,raw,state,path,expected):
    from .body import effect,union
    control,state,d=checker.value(raw["control"],state,path+("control",))
    require(isinstance(control.type,RNode) and control.type.tag=="Control","REQUEST-CANCEL needs Control")
    checker.check_hold(control,state)
    body=checker.command(raw["next"],state,path+("next",),expected)
    return checker.finish("request_cancel",path,body.type,
        union(effect(node("atom","RequestCancel",job=control.type.get("job"))),body.effect),
        body.normal,body.exceptional,(d,body.derivation))
