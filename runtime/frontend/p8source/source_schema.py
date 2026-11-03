"""Strict finite source syntax. Proof objects cannot be supplied as source."""
from p8static import parser as old
from p8foundation.model import require, FoundationError
from p8closure.requirements import Unsupported

VERSION = "p8-runtime-open-source-v0.1"
CLOSED_VERSION = "p8-runtime-closed-source-v0.1"
FIELDS = {
 "value": {"var":("name",), "unit":(), "bool":("literal",), "nat":("literal",), "word":("literal",),
   "pair":("left","right"), "inl":("payload","sum_type"), "inr":("payload","sum_type"),
   "pack":("exists_type","witnesses","payload"),
   "lambda":("mode","captures","foot","projections","requires","links","effect","parameter","result","body")},
 "expression": {"value":("value",), "fst":("value",), "snd":("value",), "op":("name","arguments")},
 "command": {"return":("value",), "let_e":("name","expression","next"), "let_c":("name","first","next"),
   "call":("function","argument"), "if":("condition","then","else"),
   "case":("value","left_name","left","right_name","right"),
   "unpack":("package","names","name","body"), "drop":("value","next"),
   "throw":("payload",), "rethrow":("failure",), "try":("body","failure_name","handler"),
   "load":("read","index"), "receive_request":("name","owner"),
   "offer_response":("name","owner","token"),
   "call_service":("name","scope","tokens"), "call_tool":("name","argument"),
   "await":("shared",), "await_cancel":("shared",),
   "new_owner":("value","names","name","body"),
   "scope":("names","name","body"), "run":("owner","names","name","body"),
   "produce":("scope","kernel","argument","names","cleanup","result_name","control_name","next"),
   "fork":("scope","names","job","result_name","control_name","next"),
   "request_cancel":("control","next")}}

CHILDREN = {
 ("value","pair"):(("left","value"),("right","value")),
 ("value","inl"):(("payload","value"),), ("value","inr"):(("payload","value"),),
 ("value","pack"):(("payload","value"),), ("value","lambda"):(("body","command"),),
 ("expression","value"):(("value","value"),), ("expression","fst"):(("value","value"),),
 ("expression","snd"):(("value","value"),),
 ("command","return"):(("value","value"),),
 ("command","let_e"):(("expression","expression"),("next","command")),
 ("command","let_c"):(("first","command"),("next","command")),
 ("command","call"):(("function","value"),("argument","value")),
 ("command","if"):(("condition","value"),("then","command"),("else","command")),
 ("command","case"):(("value","value"),("left","command"),("right","command")),
 ("command","unpack"):(("package","value"),("body","command")),
 ("command","drop"):(("value","value"),("next","command")),
 ("command","throw"):(("payload","value"),), ("command","rethrow"):(("failure","value"),),
 ("command","try"):(("body","command"),("handler","command")),
 ("command","load"):(("read","value"),("index","value")),
 ("command","receive_request"):(("owner","value"),),
 ("command","offer_response"):(("owner","value"),("token","value")),
 ("command","call_service"):(("scope","value"),("tokens","value")),
 ("command","call_tool"):(("argument","value"),),
 ("command","await"):(("shared","value"),),
 ("command","await_cancel"):(("shared","value"),),
 ("command","new_owner"):(("value","value"),("body","command")),
 ("command","scope"):(("body","command"),),
 ("command","run"):(("owner","value"),("body","command")),
 ("command","request_cancel"):(("control","value"),("next","command"))}

GENERATION_SORTS={"new_owner":("Size","Owner"), "scope":("Scope","Region","Invocation"),
                  "run":("Epoch","Scope","Region","Invocation"),
                  "produce":("Job","Task","Invocation","Region","Access","Access","Region"),
                  "fork":("Job","Task","Invocation","Region")}


def source_children(raw,family):
    """Actual source children, including lists; synthetic captures are not nodes."""
    tag=raw[family]
    if (family,tag)==("expression","op"):
        return tuple((("arguments",i),"value",v) for i,v in enumerate(raw["arguments"]))
    if (family,tag)==("command","produce"):
        return ((("scope",),"value",raw["scope"]),(("argument",),"value",raw["argument"]),
                (("cleanup","body"),"command",raw["cleanup"]["body"]),(("next",),"command",raw["next"]))
    if (family,tag)==("command","fork"):
        return ((("scope",),"value",raw["scope"]),(("job","body"),"command",raw["job"]["body"]),
                (("next",),"command",raw["next"]))
    return tuple(((key,),kind,raw[key]) for key,kind in CHILDREN.get((family,tag),()))


def _validate_node(raw, family, path=()):
    require(type(raw) is dict and type(raw.get(family)) is str, "tagged " + family + " required at " + repr(path))
    tag = raw[family]
    if tag not in FIELDS[family]:
        raise Unsupported("source constructor " + tag + " at " + repr(path))
    keys={family,*FIELDS[family][tag]}
    if family=="command" and tag=="let_c" and "bind_type" in raw: keys.add("bind_type")
    old.obj(raw, keys, repr(path))
    for key in ("name","left_name","right_name","failure_name","result_name","control_name"):
        if key in raw:
            if family=="command" and tag=="call_tool" and key=="name":
                require(raw[key]=="tokenizer.vocab_symbol","fixed readonly-tool name")
            else: old.ident(raw[key], repr(path + (key,)))
    if family=="command" and tag in GENERATION_SORTS:
        names=tuple(old.name(n,"generation.name") for n in old.sequence(raw["names"],"generation.names"))
        require(tuple(n.sort for n in names)==GENERATION_SORTS[tag],"generation ordered name sorts")
        require(len({n.text for n in names})==len(names),"duplicate generation name")
    if family == "value" and tag == "lambda":
        old.obj(raw["parameter"], {"name","type"}, "parameter")
        old.ident(raw["parameter"]["name"], "parameter.name")
        old.sequence(raw["captures"], "captures")
        for x in raw["captures"]: old.ident(x, "capture")
        require(raw["mode"] in ("U","A"), "lambda mode")
    if (family,tag)==("command","produce"):
        old.ident(raw["kernel"],"kernel")
        old.obj(raw["cleanup"],{"captures","parameter","result","body"},"cleanup")
        old.obj(raw["cleanup"]["parameter"],{"name"},"cleanup parameter")
        old.ident(raw["cleanup"]["parameter"]["name"],"cleanup parameter name")
        for name in old.sequence(raw["cleanup"]["captures"],"cleanup captures"): old.ident(name,"cleanup capture")
        require(raw["result_name"]!=raw["control_name"],"PRODUCE result/control binders must differ")
    if (family,tag)==("command","fork"):
        old.obj(raw["job"],{"captures","result","body"},"fork job")
        for name in old.sequence(raw["job"]["captures"],"fork captures"): old.ident(name,"fork capture")
        require(raw["result_name"]!=raw["control_name"],"FORK result/control binders must differ")
    if (family,tag)==("expression","op"):
        old.sequence(raw["arguments"],"operation arguments")
    if family == "value" and tag in ("bool","nat","word"):
        v = raw["literal"]
        require(type(v) is bool if tag == "bool" else type(v) is int, "literal kind")
        if tag == "nat": require(v >= 0, "Nat literal")
        if tag == "word": require(-(2**63) <= v < 2**64, "Word literal range")
    for suffix,child_family,child in source_children(raw,family):
        validate_node(child,child_family,path+suffix)


def validate_node(raw, family, path=()):
    try:
        return _validate_node(raw,family,path)
    except (FoundationError,old.ParseError,Unsupported) as error:
        if not hasattr(error,"source_path"): error.source_path=path
        raise


def validate_document(raw):
    old.obj(raw, {"schema","world","entry"}, "source document")
    require(raw["schema"] == VERSION, "source schema version")
    old.obj(raw["world"], {"names","facts"}, "world")
    old.obj(raw["entry"], {"caller","inputs","result","effect","body"}, "entry")
    old.obj(raw["entry"]["caller"], {"job","task","invocation","region","scope_stack"}, "caller")
    for x in old.sequence(raw["entry"]["inputs"], "inputs"):
        old.obj(x, {"name","type"}, "input")
        old.ident(x["name"], "input name")
    validate_node(raw["entry"]["body"], "command", ("entry","body"))


def validate_closed_document(raw):
    old.obj(raw,{"schema","main"},"closed source document")
    require(raw["schema"]==CLOSED_VERSION,"closed source schema version")
    old.obj(raw["main"],{"result","effect","body"},"closed main")
    validate_node(raw["main"]["body"],"command",("main","body"))

