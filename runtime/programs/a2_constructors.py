"""Four real closed source constructions, not checked-program envelopes.

Running this file only writes source JSON. It does not check, authorize, or
execute a program. Source preparation has not run this constructor yet.
"""
from copy import deepcopy
import json
from pathlib import Path

MODES=("normal","cancel","drop","throw")
def t(tag,**fields): return {"type":tag,**fields}
def v(tag,**fields): return {"value":tag,**fields}
def c(tag,**fields): return {"command":tag,**fields}
def var(name): return v("var",name=name)
def unit(): return v("unit")
def ret(value=None): return c("return",value=unit() if value is None else value)
def nm(name,sort): return {"name":name,"sort":sort}
def rel(tag,**fields): return {"relation":tag,**fields}
def atom(tag,**fields): return {"atom":tag,**fields}
def eff(*atoms): return {"effect":"set","atoms":list(atoms)}
def union(*effects):
    result=eff()
    for e in effects: result={"effect":"union","left":result,"right":e}
    return result
def nu(names,facts,effect): return {"effect":"nu","interface":{"names":deepcopy(names),"facts":deepcopy(facts)},"body":effect}
def letv(name,value,next): return c("let_e",name=name,expression={"expression":"value","value":value},next=next)
def letproj(name,arg,arm,next): return c("let_e",name=name,expression={"expression":arm,"value":var(arg)},next=next)
def call(function,argument=None): return c("call",function=var(function),argument=unit() if argument is None else argument)
def drop(name,next): return c("drop",value=var(name),next=next)
def lam(name,parameter,result,body,effect,captures):
    return v("lambda",mode="U",captures=captures,foot={"derive":"captures"},projections={"derive":"captures"},
             requires={"derive":"support"},links={"derive":"support"},effect=effect,
             parameter={"name":name,"type":parameter},result=result,body=body)

def words_type():
    result=t("Word")
    for _ in range(7): result=t("Pair",left=t("Word"),right=result)
    return result
def words_value():
    result=var("w7")
    for i in reversed(range(7)): result=v("pair",left=var("w"+str(i)),right=result)
    return result
def mode_type():
    return t("Sum",left=t("Unit"),right=t("Sum",left=t("Unit"),right=t("Sum",left=t("Unit"),right=t("Unit"))))
def mode_value(mode):
    if mode=="normal": return v("inl",payload=unit(),sum_type=mode_type())
    second=mode_type()["right"]
    if mode=="cancel": payload=v("inl",payload=unit(),sum_type=second)
    else:
        last=second["right"]
        payload=v("inr",sum_type=second,payload=v("inl" if mode=="drop" else "inr",payload=unit(),sum_type=last))
    return v("inr",payload=payload,sum_type=mode_type())

def owner_interface():
    return {"names":[nm("packN","Size"),nm("packOwner","Owner")],"facts":[
        rel("positiveSize",size={"witness":"packN"}),rel("owner",owner="packOwner",size={"witness":"packN"}),
        rel("sizeEqual",left={"witness":"packN"},right=8)]}
def owner_type(): return t("Exists",interface=owner_interface(),body=t("Owner",owner="packOwner",size={"witness":"packN"}))

def producer_parts():
    names=[nm(n,s) for n,s in (("p","Job"),("task","Task"),("invocation","Invocation"),("jobR","Region"),
                              ("writeA","Access"),("readA","Access"),("cleanupR","Region"))]
    facts=[rel("job",job="p",task="task",scope="runS",result=words_type()),rel("result",job="p",result=words_type()),
           rel("jobRegion",job="p",region="jobR"),rel("bodyRegion",job="p",role="cleanup",region="cleanupR"),
           rel("producerOf",job="p",scope="runS",owner="openedOwner",epoch="runG"),
           rel("access",access="writeA",job="p",owner="openedOwner",epoch="runG",region="jobR"),
           rel("access",access="readA",job="p",owner="openedOwner",epoch="runG",region="cleanupR"),
           rel("outlives",inner="jobR",outer="runR"),rel("outlives",inner="cleanupR",outer="jobR")]
    return names,facts
def cleanup_body():
    body=ret(words_value())
    for i in reversed(range(8)):
        body=c("let_c",name="w"+str(i),first=c("load",read=var("a"),index=v("nat",literal=i)),next=body)
    read_lambda=lam("unused",t("Unit"),words_type(),body,eff(atom("Read",owner="openedOwner"),atom("Raise")),["a"])
    return letv("readAll",read_lambda,call("readAll"))
def action_body():
    # All four arms are actual source code checked and interpreted normally.
    cancel=c("request_cancel",control=var("k"),next=c("request_cancel",control=var("k"),next=ret()))
    explicit_drop=drop("h",drop("k",ret()))
    throw=c("let_e",name="payload",expression={"expression":"op","name":"body_error","arguments":[unit()]},
            next=c("throw",payload=var("payload")))
    return c("case",value=var("mode"),left_name="normalArg",left=ret(),right_name="nextMode",
             right=c("case",value=var("nextMode"),left_name="cancelArg",left=cancel,right_name="lastMode",
                     right=c("case",value=var("lastMode"),left_name="dropArg",left=explicit_drop,right_name="throwArg",right=throw)))
def run_parts():
    names=[nm("runG","Epoch"),nm("runS","Scope"),nm("runR","Region"),nm("runI","Invocation")]
    facts=[rel("epoch_owner",epoch="runG",owner="openedOwner"),
           rel("scope_shape",scope="runS",region="runR",kind={"owner":"openedOwner","epoch":"runG"})]
    return names,facts
def run_effect():
    pnames,pfacts=producer_parts();rnames,rfacts=run_parts()
    producer=nu(pnames,pfacts,eff(atom("Read",owner="openedOwner"),atom("Raise"),
                                atom("RequestCancel",job="p"),atom("Register",scope="runS"),atom("Issue",owner="openedOwner")))
    return union(eff(atom("Raise")),nu(rnames,rfacts,union(eff(atom("Enter",scope="runS"),atom("Drain",scope="runS"),atom("Raise")),producer)))
def step_lambda():
    names,_=producer_parts()
    producer=c("produce",scope=var("scopeHandle"),kernel="Fill8",argument=var("seed"),names=names,
               cleanup={"captures":[],"parameter":{"name":"a"},"result":words_type(),"body":cleanup_body()},
               result_name="h",control_name="k",next=action_body())
    run=c("run",owner=var("ownerHandle"),names=run_parts()[0],name="scopeHandle",body=producer)
    body=letproj("seed","input","fst",letproj("mode","input","snd",run))
    return lam("input",t("Pair",left=t("Word"),right=mode_type()),t("Unit"),body,run_effect(),["ownerHandle"])
def caught(effect):
    result=deepcopy(effect)
    if result["effect"]=="set": result["atoms"]=[a for a in result["atoms"] if a["atom"]!="Raise"]
    elif result["effect"] in ("nu","hide"): result["body"]=caught(result["body"])
    else: result["left"]=caught(result["left"]);result["right"]=caught(result["right"])
    return result

def source(mode):
    if mode not in MODES: raise ValueError("unknown source mode")
    new_names=[nm("newN","Size"),nm("newOwner","Owner")]
    new_facts=[rel("positiveSize",size={"witness":"newN"}),rel("owner",owner="newOwner",size={"witness":"newN"}),
               rel("sizeEqual",left={"witness":"newN"},right=8)]
    allocate=c("new_owner",value=v("nat",literal=8),names=new_names,name="newHandle",body=ret(
        v("pack",exists_type=owner_type(),witnesses=[{"name":"newN"},{"name":"newOwner"}],payload=var("newHandle"))))
    first=c("try",failure_name="aFailure",body=call("step",v("pair",left=v("word",literal=17),right=mode_value(mode))),handler=ret())
    second=call("step",v("pair",left=v("word",literal=29),right=mode_value("normal")))
    repeated=letv("step",step_lambda(),c("let_c",name="aResult",first=first,next=second))
    opened=c("unpack",package=var("package"),names=[nm("openedN","Size"),nm("openedOwner","Owner")],name="ownerHandle",body=repeated)
    body=c("let_c",name="package",first=allocate,next=opened)
    # The hide binder uses package slots; the body is written with those same
    # names, not the UNPACK implementation's fresh skolem spellings.
    hidden=union(caught(run_effect()),run_effect())
    def rename(value):
        if type(value) is str: return {"openedN":"packN","openedOwner":"packOwner"}.get(value,value)
        if type(value) is list: return [rename(x) for x in value]
        if type(value) is dict: return {k:rename(v) for k,v in value.items()}
        return value
    upper=union(eff(atom("Alloc"),atom("Raise")),nu(new_names,new_facts,eff()),
                {"effect":"hide","interface":owner_interface(),"body":rename(hidden)})
    return {"schema":"p8-runtime-closed-source-v0.1","main":{"result":t("Unit"),"effect":upper,"body":body}}

def write_sources(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    for mode in MODES:
        path=directory/(mode+".json")
        with path.open("x",encoding="utf-8") as stream: json.dump(source(mode),stream,ensure_ascii=False,indent=2);stream.write("\n")

if __name__=="__main__": write_sources(Path(__file__).resolve().parent)
