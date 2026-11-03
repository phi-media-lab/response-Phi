#!/usr/bin/env python3
"""CM2 finite source constructors; imports only pinned, pure CM1/A2 helpers.

No candidate checker/runtime/worker import. Source bytes and site metadata are
materialized only when root executes this frozen generator for the first time.
"""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent
BASE=HERE/"build_cm1_programs.py"
BASE_SHA="36e3e2ea41400de2c271a6df938325ae98c6ca5ac2232155ffd082a266d120e0"
if hashlib.sha256(BASE.read_bytes()).hexdigest()!=BASE_SHA: raise RuntimeError("CM1 pure helper pin")
spec=importlib.util.spec_from_file_location("cm2_pinned_cm1_data",BASE)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
t,v,c,var,unit,ret,nm,rel,atom,eff,union,nu,letv,letproj,lam=(getattr(m,x) for x in
    ("t","v","c","var","unit","ret","nm","rel","atom","eff","union","nu","letv","letproj","lam"))
bind,pair,hide=m.bind,m.pair,m.hide
PROFILE="cm2-mlx-prefill-fork-owner1-v1"
SCHEMA=m.SCHEMA
WTYPE=t("Pair",left=t("Sum",left=t("Word"),right=t("Unit")),right=t("Word"))


def terminal(job): return eff(atom("Need",job=job,mode="terminal"),atom("Raise"))
def throw_body():
    return c("let_e",name="errorPayload",expression={"expression":"op","name":"body_error","arguments":[unit()]},
             next=c("throw",payload=var("errorPayload")))
def call(name): return c("call",function=var(name),argument=unit())
def drop(name,body): return c("drop",value=var(name),next=body)
def word(n): return v("word",literal=n)
def fn_type(mode="U",requirement=None):
    return t("Fn",mode=mode,foot={"support":"all","items":[]},projections={"selectors":[],"entries":[]},
             requires={"requires":"true"} if requirement is None else requirement,links=[],
             parameter=t("Unit"),result=t("Word"),effect=eff())
def pure_fn(mode="U",requirement=None):
    value=lam("unused",t("Unit"),t("Word"),ret(word(17)),eff(),[])
    value.update(mode=mode,requires={"requires":"true"} if requirement is None else requirement,links=[])
    return value
def fork_parts(prefix,scope,region,result):
    names=[nm(prefix+suffix,sort) for suffix,sort in (("P","Job"),("Task","Task"),("I","Invocation"),("R","Region"))]
    facts=[rel("job",job=prefix+"P",task=prefix+"Task",scope=scope,result=deepcopy(result)),
           rel("result",job=prefix+"P",result=deepcopy(result)),rel("jobRegion",job=prefix+"P",region=prefix+"R"),
           rel("bodyRegion",job=prefix+"P",role="ordinary",region=prefix+"R"),rel("outlives",inner=prefix+"R",outer=region)]
    return names,facts
def fork(prefix,target,scope,region,result,body,captures,next,body_effect=None,next_effect=None):
    names,facts=fork_parts(prefix,scope,region,result)
    command=c("fork",scope=var(target),names=names,job={"captures":list(captures),"result":deepcopy(result),"body":body},
              result_name=prefix+"H",control_name=prefix+"K",next=next)
    effect=nu(names,facts,union(body_effect or eff(),next_effect or eff(),eff(atom("Register",scope=scope),atom("Raise"))))
    return command,effect
def scope(prefix,body,effect,*,target_name=None,edges=()):
    names=[nm(prefix+suffix,sort) for suffix,sort in (("S","Scope"),("R","Region"),("I","Invocation"))]
    facts=[rel("scope_shape",scope=prefix+"S",region=prefix+"R",kind="plain")]
    facts += [rel("outlives",inner=prefix+"R",outer=r) for r in edges]
    return (c("scope",names=names,name=target_name or prefix+"Scope",body=body),
            union(eff(atom("Raise")),nu(names,facts,union(effect,eff(atom("Enter",scope=prefix+"S"),atom("Drain",scope=prefix+"S"),atom("Raise"))))))
def closed(body,effect,result=None): return {"schema":SCHEMA,"main":{"result":result or t("Unit"),"effect":effect,"body":body}}
def in_scope(body,effect,result=None): return closed(*scope("Test",body,effect),result)


def targets():
    plain,pe=fork("Plain","TestScope","TestS","TestR",t("Unit"),ret(),[],ret())
    owned,oe=fork("Owned","AScope","AS","AR",t("Unit"),ret(),[],ret())
    rn,rf=m.run_parts("A")
    run=c("run",owner=var("owner"),names=rn,name="AScope",body=owned)
    re=union(eff(atom("Raise")),nu(rn,rf,union(oe,eff(atom("Enter",scope="AS"),atom("Drain",scope="AS"),atom("Raise")))))
    sc,se=scope("Test",plain,pe)
    return m.with_owner(bind("plainDone",sc,run),union(se,re),t("Unit"))


def full_stable():
    interface={"names":[nm("HiddenJob","Job")],"facts":[rel("result",job="HiddenJob",result=t("Word"))]}
    package=t("Exists",interface=interface,body=t("Shared",job="HiddenJob",payload=t("Word")))
    result=t("Pair",left=fn_type(),right=package)
    packaged=v("pack",exists_type=package,witnesses=[{"name":"SeedP"}],payload=var("SeedH"))
    historical=bind("historicalWord",m.await_payload("openedShared","OldSeed"),ret(pair(var("actualFnWord"),var("historicalWord"))))
    opened=c("unpack",package=var("deliveredPackage"),names=[nm("OpenedJob","Job")],name="openedShared",body=historical)
    finish=bind("actualFnWord",call("deliveredFn"),opened)
    consume=bind("delivered",m.await_payload("FullH","FullResult"),
        letproj("deliveredFn","delivered","fst",letproj("deliveredPackage","delivered","snd",finish)))
    consume_effect=union(m.need("FullP"),hide(interface,m.need("HiddenJob")))
    full,fe=fork("Full","TestScope","TestS","TestR",result,ret(pair(pure_fn(),packaged)),["SeedH"],consume,next_effect=consume_effect)
    seed,se=fork("Seed","TestScope","TestS","TestR",t("Word"),ret(word(5)),[],full,next_effect=fe)
    return in_scope(seed,se,t("Pair",left=t("Word"),right=t("Word")))


def compound_scope():
    scope_type=t("Scope",scope="TestS",region="TestR",kind="plain")
    sum_type=t("Sum",left=scope_type,right=t("Unit"))
    scoped_fn=lam("unused",t("Unit"),t("Unit"),drop("TestScope",ret()),eff(),["TestScope"])
    body=drop("scopePair",bind("sumDone",c("case",value=var("scopeSum"),left_name="chosenScope",left=drop("chosenScope",ret()),
          right_name="empty",right=ret()),call("scopeFn")))
    command,effect=fork("Compound","TestScope","TestS","TestR",t("Unit"),body,["scopePair","scopeSum","scopeFn"],ret())
    return in_scope(letv("scopePair",pair(var("TestScope"),word(1)),
        letv("scopeSum",v("inl",sum_type=sum_type,payload=var("TestScope")),letv("scopeFn",scoped_fn,command))),effect)


def hidden_scope(strong):
    facts=[rel("scope_shape",scope="HiddenS",region="HiddenR",kind="plain")]
    if strong: facts.append(rel("outlives",inner="TestR",outer="HiddenR"))
    interface={"names":[nm("HiddenS","Scope"),nm("HiddenR","Region")],"facts":facts}
    typ=t("Exists",interface=interface,body=t("Scope",scope="HiddenS",region="HiddenR",kind="plain"))
    package=v("pack",exists_type=typ,witnesses=[{"name":"TestS"},{"name":"TestR"}],payload=var("TestScope"))
    job=c("unpack",package=var("hiddenScope"),names=[nm("OpenedS","Scope"),nm("OpenedR","Region")],name="openedScope",body=drop("openedScope",ret()))
    command,effect=fork("Hidden","TestScope","TestS","TestR",t("Unit"),job,["hiddenScope"],ret(),body_effect=hide(interface,eff()))
    return in_scope(letv("hiddenScope",package,command),effect)


def shorter_scope():
    command,effect=fork("Short","TestScope","TestS","TestR",t("Unit"),drop("InnerScope",ret()),["InnerScope"],ret())
    inner,ie=scope("Inner",command,effect)
    return in_scope(inner,ie)


def hidden_read(kind):
    name="ARead"; setup=lambda body:body
    if kind=="closure":
        name="readFn"
        function=lam("unused",t("Unit"),t("Word"),c("load",read=var("ARead"),index=v("nat",literal=0)),m.read_effect(),["ARead"])
        setup=lambda body:letv(name,function,body)
    if kind=="exists":
        name="readPackage"
        interface={"names":[nm("HiddenAccess","Access"),nm("HiddenRegion","Region")],"facts":[
            rel("access",access="HiddenAccess",job="AP",owner="O",epoch="AG",region="HiddenRegion")]}
        typ=t("Exists",interface=interface,body=t("Read",access="HiddenAccess",owner="O",epoch="AG",region="HiddenRegion"))
        package=v("pack",exists_type=typ,witnesses=[{"name":"AReadA"},{"name":"ACleanupR"}],payload=var("ARead"))
        setup=lambda body:letv(name,package,body)
    command,ce=fork("Forbidden","AScope","AS","AR",t("Unit"),drop(name,ret()),[name],ret(word(0)))
    run=m.producer_run("A","Prefill32",m.tokens_value([word(x) for x in m.A32]),ret(),cleanup=setup(command))
    run["body"]["cleanup"]["captures"]=["AScope"]
    return m.with_owner(run,m.run_effect("A",t("Word"),ce,eff()),t("Unit"))


def affine(captured):
    body=drop("affineFn",ret()) if captured else ret()
    next=ret() if captured else bind("consumed",call("affineFn"),ret())
    command,effect=fork("Affine","TestScope","TestS","TestR",t("Unit"),body,["affineFn"] if captured else [],next)
    return in_scope(letv("affineFn",pure_fn("A"),command),effect)


def caller_kappa(call_in_child):
    requirement={"requires":"same_job","job":"ParentP"}
    child_result=t("Word") if call_in_child else fn_type(requirement=requirement)
    child_body=call("parentFn") if call_in_child else ret(var("parentFn"))
    consume=bind("received",m.await_payload("ChildH","ChildResult"),ret(var("received")) if call_in_child else call("received"))
    child,ce=fork("Child","TestScope","TestS","TestR",child_result,child_body,["parentFn"],consume,next_effect=m.need("ChildP"))
    parent_body=letv("parentFn",pure_fn(requirement=requirement),child)
    parent,pe=fork("Parent","TestScope","TestS","TestR",t("Word"),parent_body,["TestScope"],
        m.await_payload("ParentH","ParentResult"),body_effect=ce,next_effect=m.need("ParentP"))
    return in_scope(parent,pe,t("Word"))


def exact_capture(duplicate):
    command,effect=fork("Exact","TestScope","TestS","TestR",t("Word"),ret(var("outerWord")),
                        ["outerWord","outerWord"] if duplicate else [],ret())
    return in_scope(letv("outerWord",word(1),command),effect)
def freshness(escape):
    command,effect=fork("Fresh","TestScope","TestS","TestR",t("Word"),ret(word(1)),[],ret(var("FreshH")) if escape else ret())
    if not escape: command["names"][0]["name"]="TestS"
    # No expected Unit propagated into the first command: the lexical fresh
    # escape check is reached before LET-C discards the resulting Shared.
    if escape: command=bind("escaped",command,ret())
    return in_scope(command,effect)
def missing_effect():
    missing,me=fork("Missing","TestScope","TestS","TestR",t("Sum",left=t("Word"),right=t("Unit")),
        c("await_cancel",shared=var("SeedH")),["SeedH"],ret())
    seed,se=fork("Seed","TestScope","TestS","TestR",t("Word"),ret(word(1)),[],missing,next_effect=me)
    return in_scope(seed,se)
def bottom(unreachable):
    command,effect=fork("Bottom","TestScope","TestS","TestR",t("Word"),throw_body(),[],ret(),body_effect=eff(atom("Raise")))
    if unreachable:
        command=c("let_c",name="never",bind_type=t("Unit"),first=throw_body(),next=command)
        effect=eff(atom("Raise"))
    return in_scope(command,effect)


def waiter_source():
    """One exact source for N/D/C3/C7; no source gate or host token."""
    choice=t("Sum",left=t("Word"),right=t("Unit"))
    selected=c("case",value=var("rawChoice"),left_name="deliveredA",left=ret(v("inl",sum_type=choice,payload=var("deliveredA"))),
               right_name="detached",right=ret(v("inr",sum_type=choice,payload=var("detached"))))
    job=bind("rawChoice",c("await_cancel",shared=var("AH")),bind("selected",selected,ret(pair(var("selected"),word(7001)))))
    after=c("request_cancel",control=var("WK"),next=c("request_cancel",control=var("WK"),next=c("await",shared=var("WH"))))
    wf,we=fork("W","WaiterScope","WaiterS","WaiterR",WTYPE,job,["AH"],after,body_effect=m.need("AP"),
        next_effect=union(terminal("WP"),eff(atom("RequestCancel",job="WP"))))
    ws,wse=scope("Waiter",wf,we)
    apack=v("pack",exists_type=m.result_package(),witnesses=[{"name":"AP"}],payload=pair(var("AH"),var("firstAToken")))
    anext=bind("waiterResult",ws,bind("firstAToken",m.await_payload("AH","AFirst"),ret(pair(var("waiterResult"),apack))))
    arun=m.producer_run("A","Prefill32",m.tokens_value([word(x) for x in m.A32]),anext)
    brun=m.producer_run("B","Prefill8",m.tokens_value([word(x) for x in m.B_PREFIX7]+[var("aToken")]),m.await_payload("BH","BFirst"))
    final=ret(pair(var("waiterResult"),pair(var("aToken"),pair(var("bToken"),var("oldAToken")))))
    opened=c("unpack",package=var("aPackage"),names=[nm("AJob","Job")],name="aPair",
        body=letproj("aShared","aPair","fst",letproj("aToken","aPair","snd",bind("bToken",brun,
            bind("oldAToken",m.await_payload("aShared","HistoricalA"),final)))))
    body=bind("aRunResult",arun,letproj("waiterResult","aRunResult","fst",letproj("aPackage","aRunResult","snd",opened)))
    ae=m.run_effect("A",t("Word"),m.read_effect(),union(wse,m.need("AP")))
    be=m.run_effect("B",t("Word"),m.read_effect(),m.need("BP"))
    result=t("Pair",left=WTYPE,right=t("Pair",left=t("Word"),right=t("Pair",left=t("Word"),right=t("Word"))))
    return m.with_owner(body,union(ae,hide(m.result_interface(),union(be,m.need("ResultJob")))),result)


def open_storage(write):
    names=[nm(n,s) for n,s in (("N","Size"),("O","Owner"),("G","Epoch"),("S","Scope"),("R","Region"),
        ("I","Invocation"),("P","Job"),("Task","Task"),("L","Lease"))]
    facts=[rel("positiveSize",size={"witness":"N"}),rel("owner",owner="O",size={"witness":"N"}),rel("epoch_owner",epoch="G",owner="O"),
        rel("scope_shape",scope="S",region="R",kind={"owner":"O","epoch":"G"}),
        rel("job",job="P",task="Task",scope="S",result=t("Unit")),rel("result",job="P",result=t("Unit")),
        rel("jobRegion",job="P",region="R"),rel("bodyRegion",job="P",role="ordinary",region="R")]
    typ=t("Write",owner="O",epoch="G") if write else t("Lease",scope="S",owner="O",epoch="G",lease="L",region="R")
    body,effect=fork("Storage","target","S","R",t("Unit"),drop("storage",ret()),["storage"],ret())
    return {"schema":"p8-runtime-open-source-v0.1","world":{"names":names,"facts":facts},"entry":{
        "caller":{"job":"P","task":"Task","invocation":"I","region":"R","scope_stack":["S"]},
        "inputs":[{"name":"target","type":t("Scope",scope="S",region="R",kind={"owner":"O","epoch":"G"})},{"name":"storage","type":typ}],
        "result":t("Unit"),"effect":effect,"body":body}}


METHOD_CASES=(
 ("test_targets",("targets",)), ("test_full_stable_result",("full_stable",)),
 ("test_compound_scope_capture",("compound_scope",)), ("test_hidden_scope_interface",("hidden_scope_strong","hidden_scope_weak")),
 ("test_shorter_scope",("shorter_scope",)), ("test_hidden_read",("read_direct","read_closure","read_exists")),
 ("test_affine_and_frame",("affine_capture","affine_frame")), ("test_caller_kappa",("kappa_hold","kappa_call")),
 ("test_exact_captures",("missing_capture","duplicate_capture")), ("test_freshness_escape",("fresh_reuse","bare_shared")),
 ("test_full_effect",("missing_effect",)), ("test_bottom_paths",("bottom_child","bottom_continuation")),
 ("test_actual_waiter_source",("normal",)), ("test_lease_write_boundary",("open_lease","open_write")))
CASES=tuple(case for _,cases in METHOD_CASES for case in cases)
BUILDERS={"targets":targets,"full_stable":full_stable,"compound_scope":compound_scope,
    "hidden_scope_strong":lambda:hidden_scope(True),"hidden_scope_weak":lambda:hidden_scope(False),"shorter_scope":shorter_scope,
    "read_direct":lambda:hidden_read("direct"),"read_closure":lambda:hidden_read("closure"),"read_exists":lambda:hidden_read("exists"),
    "affine_capture":lambda:affine(True),"affine_frame":lambda:affine(False),"kappa_hold":lambda:caller_kappa(False),"kappa_call":lambda:caller_kappa(True),
    "missing_capture":lambda:exact_capture(False),"duplicate_capture":lambda:exact_capture(True),"fresh_reuse":lambda:freshness(False),
    "bare_shared":lambda:freshness(True),"missing_effect":missing_effect,"bottom_child":lambda:bottom(False),"bottom_continuation":lambda:bottom(True),
    "normal":waiter_source,"open_lease":lambda:open_storage(False),"open_write":lambda:open_storage(True)}
MESSAGES={"hidden_scope_weak":"FORK captured lifetime: no-outlives-path","shorter_scope":"FORK captured lifetime: no-outlives-path",
    "read_direct":"FORK capture contains forbidden storage support","read_closure":"FORK capture contains forbidden storage support",
    "read_exists":"FORK capture contains forbidden storage support","affine_capture":"FORK cannot capture affine value",
    "kappa_call":"CALL kappa caller requirement: caller-job-mismatch","missing_capture":"exact fork free captures mismatch",
    "duplicate_capture":"duplicate fork capture","fresh_reuse":"generation names must be fresh","bare_shared":"generation result name escape",
    "missing_effect":"main source effect upper bound: no-injective-hide-nu-match","open_lease":"FORK capture contains forbidden storage support",
    "open_write":"Write kernel-context formation"}
EXPECTATIONS={name:({"route":"open" if name.startswith("open_") else "closed","status":"static-rejected","frontend_exit":1,
    "diagnostic_phase":"static","diagnostic_code":"unsupported" if name=="open_write" else "semantic-no","diagnostic_message":MESSAGES[name]}
    if name in MESSAGES else {"route":"closed","status":"ready","frontend_exit":0,"profile":PROFILE}) for name in CASES}
def source(case): return BUILDERS[case]()
def encode(value): return (json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(",",":"))+"\n").encode()
def source_bytes(case): return encode(source(case))
def nodes(raw,path=()):
    if isinstance(raw,dict):
        yield path,raw
        for k,value in raw.items(): yield from nodes(value,path+(k,))
    elif isinstance(raw,list):
        for i,value in enumerate(raw): yield from nodes(value,path+(i,))
def source_sites(raw):
    """Fixed policy selectors are resolved from the frozen source, never a log."""
    found={}
    for path,node in nodes(raw):
        if node.get("command")=="produce" and node.get("result_name")=="AH": found["a_produce"]="/"+"/".join(map(str,path))
        if node.get("command")=="fork" and node.get("result_name")=="WH": found["w_fork"]="/"+"/".join(map(str,path))
        if node.get("command")=="scope" and node.get("name")=="WaiterScope": found["waiter_scope"]="/"+"/".join(map(str,path))
        if node.get("command")=="await_cancel" and node.get("shared")==var("AH") and "job" in path: found["w_await"]="/"+"/".join(map(str,path))
    return found
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--output-dir",type=Path,required=True);args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=False);records=[]
    for name in CASES:
        raw=source(name);data=encode(raw);(args.output_dir/(name+".json")).write_bytes(data)
        records.append({"case":name,"file":name+".json","bytes":len(data),"sha256":hashlib.sha256(data).hexdigest(),
            "canonical_sha256":hashlib.sha256(json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest(),
            "expected":EXPECTATIONS[name],"sites":source_sites(raw)})
    result={"schema":"cm2-source-generation-v1","checked":False,"runtime_started":False,
        "generator_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"cm1_helper_sha256":BASE_SHA,
        "a2_helper_sha256":m.HELPER_SHA256,"fixed_input_origin":m.INPUT_ORIGIN,"methods":METHOD_CASES,"cases":records}
    (args.output_dir/"manifest.json").write_text(json.dumps(result,indent=2)+"\n");print(json.dumps(result))
if __name__=="__main__": main()
