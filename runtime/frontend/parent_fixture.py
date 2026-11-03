"""Pure SRC1 B checked-parent source builders; no checking or execution on import.

Tokens are original A-batch B8 input words, not a claimed B output/receipt.
Actual parent evaluation and child request correspondence must be re-observed.
"""
from copy import deepcopy

B8=(2127,19010,19287,13098,1724,7344,1626,72)

def t(tag,**fields):return {"type":tag,**fields}
def v(tag,**fields):return {"value":tag,**fields}
def c(tag,**fields):return {"command":tag,**fields}
def var(name):return v("var",name=name)
def word(value):return v("word",literal=value)
def ret(value=None):return c("return",value=v("unit") if value is None else value)
def atom(tag,**fields):return {"atom":tag,**fields}
def eff(*atoms):return {"effect":"set","atoms":list(atoms)}
def pair_type(left,right):return t("Pair",left=left,right=right)
def words_type(n):
    value=t("Word")
    for _ in range(n-1):value=pair_type(t("Word"),value)
    return value

def tokens_value(tokens=B8):
    value=word(tokens[-1])
    for token in reversed(tokens[:-1]):value=v("pair",left=word(token),right=value)
    return value

def tool_info_type():return pair_type(t("Nat"),words_type(4))
def result_type():return pair_type(t("Word"),tool_info_type())
def service(scope="s",tokens=None):
    return c("call_service",name="B",scope=var(scope),tokens=var("requestTokens") if tokens is None else tokens)
def tool(argument=None):
    return c("call_tool",name="tokenizer.vocab_symbol",argument=var("b") if argument is None else argument)

def in_scope(body,body_effect,result=None,prefix="Parent",name="s"):
    names=[{"name":prefix+suffix,"sort":sort} for suffix,sort in (("S","Scope"),("R","Region"),("I","Invocation"))]
    facts=[{"relation":"scope_shape","scope":prefix+"S","region":prefix+"R","kind":"plain"}]
    local={"effect":"union","left":eff(atom("Enter",scope=prefix+"S"),atom("Drain",scope=prefix+"S"),atom("Raise")),"right":body_effect}
    return {"schema":"p8-runtime-closed-source-v0.1","main":{
        "result":t("Unit") if result is None else result,
        "effect":{"effect":"nu","interface":{"names":deepcopy(names),"facts":facts},"body":local},
        "body":c("scope",names=names,name=name,body=body)}}

def source():
    tail=ret(v("pair",left=var("b"),right=var("info")))
    body=c("let_e",name="requestTokens",expression={"expression":"value","value":tokens_value()},
        next=c("let_c",name="b",first=service(),next=c("let_c",name="info",first=tool(),next=tail)))
    effects=eff(atom("HostService",scope="ParentS",service="B"),atom("LocalRead",capability="tokenizer_vocab_v1"),atom("Raise"))
    return in_scope(body,effects,result_type())

def service_node(raw):return raw["main"]["body"]["body"]["next"]["first"]
def tool_node(raw):return raw["main"]["body"]["body"]["next"]["next"]["first"]
def declared_atoms(raw):return raw["main"]["effect"]["body"]["right"]["atoms"]
