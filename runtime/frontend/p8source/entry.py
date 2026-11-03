"""Public closed/open source entries. Static INIT is not a runtime startup."""
import hashlib
import json
from p8static import parser as old
from p8static.syntax import Name
from p8foundation.model import Free, Decision, FoundationError, node, require
from p8foundation.resolve import Resolver
from p8closure.schema import Schema
from p8closure.support import ClosureContext, Caller, stable, effect_includes
from p8closure.formation import form_type, form_effect, form_world
from p8closure.requirements import ProofContext, Unsupported, ktrue, caller_key, require_proof
from .body import Checker, State, SourceResult, check_document, effect, union
from .inference import NeedsBindType
from .source_schema import validate_closed_document


def source_spellings(value):
    if type(value) is str: return frozenset((value,))
    if type(value) is dict:
        return frozenset(value).union(*(source_spellings(v) for v in value.values()))
    if type(value) is list:
        return frozenset().union(*(source_spellings(v) for v in value))
    return frozenset()


def initial_world(source,result,site):
    """Private abstract names avoid every source spelling, plus resolver fencing.

    The recipe binds abstract identities only. It has no Task, allocation,
    NameEnv or completion observation and cannot be submitted as source.
    """
    labels=(("main","Job"),("task","Task"),("main_invocation","Invocation"),
            ("initial_invocation","Invocation"),("host_region","Region"),
            ("main_region","Region"),("initial_scope","Scope"),("initial_region","Region"))
    used=set(source_spellings(source));names=[];refs={}
    for label,sort in labels:
        spelling="__p8_init_"+label
        while spelling in used: spelling+="_"
        used.add(spelling);names.append(Name(spelling,sort));refs[label]=Free(sort,spelling)
    m,t,im,i0,rh,rm,s0,r0=(refs[k] for k,_ in labels)
    registration=node("boundary","HostRoot")
    origin=node("initialization","origin",site=site,**refs,registration=registration,
                runtime_started=False)
    facts=(node("relation","job",job=m,task=t,boundary=registration,result=result),
           node("relation","result",job=m,result=result),
           node("relation","jobRegion",job=m,region=rm),
           node("relation","bodyRegion",job=m,role="ordinary",region=rm),
           node("relation","scope",scope=s0,region=r0,kind="plain",parent_invocation=im),
           node("relation","outlives",inner=r0,outer=rm),
           node("relation","outlives",inner=rm,outer=rh))
    world=ClosureContext(tuple(names),facts,init_origin=origin);form_world(world)
    parent=Caller(m,t,im,rm,());caller=Caller(m,t,i0,r0,(s0,))
    parent.validate(world);caller.validate(world)
    parent_key=caller_key(parent);caller_key_=caller_key(caller)
    exit_plan=node("frame","ScopeExit",site=site+("implicit-scope-exit",),scope=s0,
                   parent=parent_key,body_caller=caller_key_,remaining=(),copy_slots=(),
                   prerequisite="drain-and-joined",owned_release=None,completed=False)
    finish=node("frame","JobFinish",site=site+("job-finish",),job=m,task=t,role="ordinary",
                source_region=rm,job_region=rm,runtime_exit=parent_key,remaining=(),completed=False)
    recipe=node("initialization","recipe",**refs,registration=registration,
                origin=origin,world_facts=facts,caller=caller_key_,parent_caller=parent_key,
                region_parents=((rh,None),(rm,rh),(r0,rm)),
                invocations=(node("initialization","invocation",invocation=im,job=m,task=t,role="job",scope=None),
                             node("initialization","invocation",invocation=i0,job=m,task=t,role="scopeBody",scope=s0)),
                scope_parent_invocation=im,synchronous_children=(s0,),local_job_roots=(),
                requires=((node("requires","Job",job=m),node("requires","Scope",scope=s0)),),
                obligation=node("obligation","Job",job=m,issuer=registration,state="Active-template"),
                host_root=node("root","host-task-required",job=m,task=t,join_observed=False,delivered=False),
                stack=(exit_plan,finish),source_scope_variable=None,runtime_started=False)
    return world,caller,recipe,exit_plan


def check_closed_main(trusted_sigma,source):
    checker=Checker("")
    try:
        checker.path=("main",)
        validate_closed_document(source)
        from .sigma import check_sigma, validate_source_profile
        check_sigma(trusted_sigma)
        validate_source_profile(trusted_sigma,source,closed=True)
        sigma=trusted_sigma.checked()
        digest=hashlib.sha256(json.dumps(source,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
        checker=Checker(digest,trusted_sigma)
        # Parse source result before private INIT names exist. The same source
        # resolver fence remains on every nested source type and witness.
        checker.path=("main","result")
        empty_world=ClosureContext((),())
        schema=Schema(Resolver(()));expected=schema.type(source["main"]["result"])
        form_type(expected,empty_world);require(stable(expected),"closed main result must be Stable")
        world,caller,init,exit_plan=initial_world(source,expected,checker.site(("main",)))
        state=State({}, {}, ProofContext(world,caller,ktrue()),source_names=())
        checker.path=("main","effect")
        upper=checker.schema(state).effect(source["main"]["effect"]);form_effect(upper,world)
        body=checker.command(source["main"]["body"],state,("main","body"),expected)
        for normal in body.normal:
            checker.check_hold(normal.value,normal.state)
            require(stable(normal.value.type),"closed main normal result must be Stable")
            require(not normal.state.affine(),"closed main affine remainder requires explicit drop")
        checker.path=("main","effect")
        match=effect_includes(body.effect,upper);require_proof(match,"main source effect upper bound")
        s0=init.get("initial_scope")
        implicit=effect(node("atom","Enter",scope=s0),node("atom","Drain",scope=s0),node("atom","Raise"))
        detail=node("initialization","checked-main",init=init,body_effect=body.effect,implicit_effect=implicit,
                    exit_plan=exit_plan,sigma=sigma,effect_match=match,runtime_started=False)
        derivation=node("derivation","closed_main",site=checker.site(("main",)),type=expected,
                        effect=union(body.effect,implicit),children=(body.derivation,),
                        normal=tuple(n.record() for n in body.normal),exceptional=tuple(e.record() for e in body.exceptional),
                        frames=init.get("stack"),detail=detail)
        return SourceResult("checked-closed-main-source",derivation,None,())
    except NeedsBindType as error:
        return SourceResult("unsupported",diagnostic={"code":"needs-bind-type","path":list(checker.path),
                            "message":str(error),"details":{"phase":"elaboration","reason":"insufficient type information"}})
    except Unsupported as error:
        return SourceResult("unsupported",diagnostic={"code":"unsupported","path":list(getattr(error,"source_path",checker.path)),"message":str(error)})
    except (FoundationError,old.ParseError) as error:
        return SourceResult("semantic-no",diagnostic={"code":"semantic-no","path":list(getattr(error,"source_path",checker.path)),"message":str(error)})
    except Exception as error:
        return SourceResult("infrastructure-error",diagnostic={"code":type(error).__name__,"path":list(checker.path),"message":str(error)})


def check_open_fragment(trusted_sigma,source):
    from .sigma import check_sigma, validate_source_profile
    try:
        check_sigma(trusted_sigma)
        validate_source_profile(trusted_sigma,source,closed=False)
    except FoundationError as error:
        return SourceResult("semantic-no",diagnostic={"code":"semantic-no","path":[],"message":str(error)})
    return check_document(source,trusted_sigma)


def verify_closed_derivation(trusted_sigma,source,derivation):
    result=check_closed_main(trusted_sigma,source)
    return Decision("yes" if result.verdict=="checked-closed-main-source" and result.derivation==derivation else "no",
                    "closed-source-reconstruction-correspondence",(result.verdict,))


def verify_open_derivation(trusted_sigma,source,derivation):
    result=check_open_fragment(trusted_sigma,source)
    return Decision("yes" if result.verdict=="checked-source-fragment-under-explicit-premises" and result.derivation==derivation else "no",
                    "source-reconstruction-correspondence",(result.verdict,))
