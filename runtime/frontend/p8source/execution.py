"""Closed-source execution profile and compact, source-bound annotation export.

This file grants no runtime capabilities. Swift accepts an exchange only from
its own fixed compiler child, then realizes all names through runtime rules.
"""
import base64
import hashlib
import json
from p8foundation.model import Free,Bound,LiteralSize,RNode,Binder
from .source_schema import source_children
from .sigma import check_sigma,wire_sigma
from .entry import check_closed_main

RESPONSE="p8-runtime-frontend-response-v1"
PROGRAM="p8-runtime-checked-program-v1"

class ProfileError(Exception):
    def __init__(self,code,message,path=()):
        super().__init__(message);self.code=code;self.path=tuple(path)

def needed(condition,message,path=()):
    if not condition: raise ProfileError("execution-profile",message,path)

def pointer(path): return "/"+"/".join(str(x).replace("~","~0").replace("/","~1") for x in path)
def site(digest,path): return {"source_sha256":digest,"pointer":pointer(path)}

def strict_source(data):
    needed(type(data) is bytes,"source must be bytes")
    needed(len(data)<=1048576,"source exceeds 1 MiB")
    def pairs(items):
        value={}
        for k,v in items:
            if k in value: raise ValueError("duplicate JSON field: "+k)
            value[k]=v
        return value
    def integer(token):
        if len(token)>4096: raise ProfileError("source-resource","integer token exceeds finite parser limit")
        return int(token)
    def reject(token): raise ValueError("non-integer/nonstandard JSON number: "+token)
    raw=json.loads(data.decode("utf-8"),object_pairs_hook=pairs,parse_int=integer,
                   parse_float=reject,parse_constant=reject)
    stack=[(raw,0)];count=0
    while stack:
        value,depth=stack.pop();count+=1
        needed(depth<=128 and count<=131072,"source structure resource limit")
        if type(value) is str and any(0xD800<=ord(c)<=0xDFFF for c in value):
            raise ProfileError("source-text-profile","surrogate source text cannot enter G1 canonical UTF-8")
        if type(value) is dict:
            stack.extend((x,depth+1) for pair in value.items() for x in pair)
        elif type(value) is list: stack.extend((x,depth+1) for x in value)
    return raw

def source_index(raw):
    nodes={};stack=[(("main","body"),"command",raw["main"]["body"])]
    while stack:
        path,family,value=stack.pop();needed(path not in nodes,"duplicate source path",path)
        nodes[path]=(family,value)
        needed(len(nodes)<=8192,"source AST exceeds 8192 nodes",path)
        if family=="value" and value[family]=="nat": needed(value["literal"]<2**64,"Nat exceeds UInt64 profile",path)
        if family=="value" and value[family]=="pack":
            for index,witness in enumerate(value["witnesses"]):
                if "size" in witness and type(witness["size"]) is int:
                    needed(witness["size"]<2**64,"Size witness exceeds UInt64 profile",path+("witnesses",index))
        # All supported NEW evaluations are guarded at runtime. A literal 0
        # remains executable; it is never a statically rejected allocation.
        if family=="command" and value[family]=="new_owner":
            operand=value["value"]
            needed(operand["value"]=="nat" and operand["literal"] in (0,1),"NEW execution profile requires a Nat literal 0 or 1",path)
        for suffix,kind,child in reversed(source_children(value,family)): stack.append((path+suffix,kind,child))
    return nodes

def key(value):
    if type(value) is dict: return ("object",tuple((k,key(v)) for k,v in value.items()))
    if type(value) in (tuple,list): return ("array",tuple(key(v) for v in value))
    return (type(value).__name__,value)

class StaticTable:
    def __init__(self): self.values=[];self.index={}
    def add(self,value):
        token=key(value)
        if token in self.index: return {"ref":str(self.index[token])}
        if value is None or type(value) in (str,bool): encoded=value
        elif type(value) is int: encoded={"integer":str(value)}
        elif type(value) in (tuple,list): encoded={"array":[self.add(x) for x in value]}
        elif type(value) is dict: encoded={"object":[[k,self.add(v)] for k,v in value.items()]}
        else: raise TypeError("not a static wire value: "+type(value).__name__)
        needed(len(self.values)<131072,"static value table exceeds limit")
        index=len(self.values);self.values.append(encoded);self.index[token]=index
        return {"ref":str(index)}

class Exporter:
    def __init__(self,source,result,data,sigma):
        self.source=source;self.result=result;self.data=data;self.sigma=check_sigma(sigma)
        self.digest=hashlib.sha256(json.dumps(source,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")).hexdigest()
        self.nodes=source_index(source);self.table=StaticTable();self.records={}
    def id_site(self,path): return site(self.digest,path)
    def binder(self,bid):
        needed(type(bid) is tuple and bid[0]==self.digest,"foreign term source identity")
        path=bid[1:-1];role=bid[-1];index=0
        if role=="capture":
            index=path[-1]
            path=path[:-3]
            if path and path[-1] in ("cleanup","job"): path=path[:-1]
        elif role in ("parameter","cleanup-parameter"):
            path=path[:-2]
            if role=="cleanup-parameter": path=path[:-1]
        elif role=="case": index=0 if path[-1]=="left" else 1;path=path[:-1]
        elif role=="term": role="let"
        elif role=="failure": role="handler"
        elif role in ("owner","scope","unpack","shared","control"):
            index=1 if role=="control" else 0;role="generated"
        needed(path in self.nodes,"term binder has no source definition",path)
        return {"site":self.id_site(path),"role":role,"index":str(index)}
    def nominal_map(self,record):
        mapping={}
        for ref,address,role,index in record.get("nominals"):
            needed(address[0]==self.digest,"foreign nominal source identity")
            path=address[1:]
            if role=="source-generation":
                needed(path in self.nodes,"generation lacks source owner",path)
                role={"new_owner":"new-owner","produce":"produce"}.get(self.nodes[path][1]["command"],self.nodes[path][1]["command"])
            item={"site":self.id_site(path),"role":role,"index":str(index),"sort":ref.sort}
            needed(ref not in mapping or mapping[ref]==item,"ambiguous nominal export",path)
            mapping[ref]=item
        return mapping
    def static(self,value,mapping):
        if isinstance(value,Free):
            needed(value in mapping,"unmapped Free in full static interface: "+value.name)
            return {"nominal":mapping[value]}
        if isinstance(value,Bound): return {"bound_depth":value.depth,"slot":value.slot,"sort":value.sort}
        if isinstance(value,LiteralSize): return {"size_literal":value.value}
        if isinstance(value,RNode):
            return {"family":value.family,"tag":value.tag,"fields":{k:self.static(v,mapping) for k,v in value.fields}}
        if isinstance(value,Binder):
            return {"binder":value.kind,"ordered_sorts":list(value.sorts),"facts":[self.static(v,mapping) for v in value.facts],"body":self.static(value.body,mapping)}
        if type(value) is tuple: return [self.static(x,mapping) for x in value]
        needed(value is None or type(value) in (str,int,bool),"proof object in static interface")
        return value
    def ref(self,value,mapping): return self.table.add(self.static(value,mapping))
    def free_nominals(self,value,mapping):
        result=[];stack=[value]
        while stack:
            item=stack.pop()
            if isinstance(item,Free):
                needed(item in mapping,"unmapped generated value nominal")
                if mapping[item] not in result: result.append(mapping[item])
            elif isinstance(item,RNode): stack.extend(v for _,v in reversed(item.fields))
            elif isinstance(item,Binder): stack.extend(reversed(item.facts+(item.body,)))
            elif type(item) is tuple: stack.extend(reversed(item))
        return result
    def annotation(self,d,record):
        path=record.get("site")[1:];family,raw=self.nodes[path];mapping=self.nominal_map(record)
        needed(all(v==record.get("remaining") for v in record.get("remaining_variants")),
               "source site normal exits have different affine annotations",path)
        def typ(value): return self.ref(value,mapping)
        bindings=[];binding_ids={}
        for n,b,t in record.get("bindings"):
            item={"binder":self.binder(b),"name":n,"type":typ(t)};identity=key(item["binder"])
            needed(identity not in binding_ids or binding_ids[identity]==item,
                   "source binder has different complete binding annotations",path)
            if identity not in binding_ids: bindings.append(item);binding_ids[identity]=item
        capture=[]
        names=raw["captures"] if family=="value" and raw[family]=="lambda" else raw["cleanup"]["captures"] if family=="command" and raw[family]=="produce" else []
        if family=="command" and raw[family]=="fork": names=raw["job"]["captures"]
        env={n:(b,t) for n,b,t in record.get("environment")}
        for index,name in enumerate(names):
            b,t=env[name]
            capture.append({"binder":{"site":self.id_site(path),"role":"capture","index":str(index)},
                            "source":self.binder(b),"use":"copy" if b in record.get("copy") else "move","type":typ(t)})
        frames=d.get("frames");frame=None
        if frames:
            needed(len(frames)==1,"multiple owning frames at one source site",path)
            f=frames[0];kind="Try" if f.tag=="TryRestore" else f.tag
            needed(kind in ("Bind","GenBind","CallReturn","Try","ScopeExit"),"frame not in execution profile",path)
            frame={"kind":kind,"retained":[self.binder(s.get("binder")) for s in f.get("remaining")],
                   "normal_restore":"move-once","exception_policy":"try-restore" if kind=="Try" else "scope-drain-before-unwind" if kind=="ScopeExit" else "unwind-drop"}
            if kind=="ScopeExit":
                contract=f.get("backend_return_contract")
                # This is checked frame metadata, not inferred from a source
                # label or accepted as an externally supplied release proof.
                frame["backend_return_contract"]=None if contract is None else dict(contract.fields)
        generation=None
        if family=="command" and raw[family] in ("new_owner","scope","run","produce","fork","unpack"):
            tag=raw[family];detail=d.get("detail")
            interface=detail.get("opening").get("interface") if tag=="unpack" else detail.get("interface")
            refs=[Free(n["sort"],n["name"]) for n in raw["names"]]
            success=[]
            for n,b,t in record.get("bindings"):
                if b[-1] in ("owner","scope","unpack","shared","control","cleanup-parameter"):
                    if isinstance(t,RNode) and t.tag in ("Owner","Scope","Shared","Control","Read"):
                        item={"binder":self.binder(b),"kind":t.tag,"nominals":self.free_nominals(t,mapping)}
                        same=[x for x in success if x["binder"]==item["binder"]]
                        needed(not same or same==[item],"different generation binding annotations",path)
                        if not same: success.append(item)
            generation={"kind":"new-owner" if tag=="new_owner" else tag,"nominals":[mapping[r] for r in refs],
                        "interface":typ(interface),"success_bindings":success}
        execution={"ordinary":"reachable","unreachable-success-template":"ghost-only",
                   "unreachable-continuation":"unreachable-continuation"}[record.get("execution")]
        return {"site":self.id_site(path),"node_kind":record.get("node_kind"),"execution":execution,
                "type":None if d.get("type") is None else typ(d.get("type")),"effect":typ(d.get("effect")),
                "bindings":bindings,"usage":{"copy":[self.binder(b) for b in record.get("copy")],
                "move":[self.binder(b) for b in record.get("move")],
                "remaining":None if record.get("remaining") is None else [self.binder(b) for b in record.get("remaining")]},
                "capture":capture,"frame":frame,"generation":generation}
    def build(self):
        # Traverse only final source children. Object identity memo is safe for
        # identical records, while distinct context derivations are all compared.
        stack=[self.result.derivation];seen={}
        while stack:
            d=stack.pop()
            if id(d) in seen: continue
            seen[id(d)]=d
            fields=dict(d.fields);record=fields.get("source_record")
            if record is not None and record.get("site")[1:] in self.nodes:
                path=record.get("site")[1:];annotation=self.annotation(d,record)
                needed(path not in self.records or self.records[path]==annotation,"source site has different executable annotations",path)
                self.records[path]=annotation
            stack.extend(reversed(fields.get("children",())))
            if d.tag in ("unreachable-continuation","unreachable-success-template"): stack.append(d.get("body"))
            detail=fields.get("detail")
            if isinstance(detail,RNode) and detail.tag=="checked-new" and detail.get("ghost_child") is not None:
                stack.append(detail.get("ghost_child"))
        needed(set(self.records)==set(self.nodes),"final checked source annotations incomplete")
        root=self.records[("main","body")]
        program={"schema":PROGRAM,"profile":self.sigma.profile,"source":{"utf8_base64":base64.b64encode(self.data).decode("ascii"),
                 "bytes_sha256":hashlib.sha256(self.data).hexdigest(),"canonical_sha256":self.digest},
                 "sigma":wire_sigma(self.sigma),"static_values":self.table.values,
                 "sites":[self.records[p] for p in self.nodes],
                 "entry":{"site":root["site"],"result_type":root["type"],"effect":root["effect"]}}
        return program

def prepare_execution(sigma,source_bytes):
    phase="source"
    try:
        check_sigma(sigma);raw=strict_source(source_bytes)
        phase="static";result=check_closed_main(sigma,raw)
        if result.verdict!="checked-closed-main-source":
            diagnostic={"phase":phase,**(result.diagnostic or {"code":result.verdict,"path":[],"message":result.verdict})}
            status="infrastructure-error" if result.verdict=="infrastructure-error" else "static-rejected"
            return {"schema":RESPONSE,"status":status,"program":None,"diagnostic":diagnostic}
        phase="execution-profile";program=Exporter(raw,result,source_bytes,sigma).build()
        response={"schema":RESPONSE,"status":"ready","program":program,"diagnostic":None}
        needed(len(json.dumps(response,ensure_ascii=True,separators=(",",":")).encode("utf-8"))<=67108864,"compiler response exceeds 64 MiB")
        return response
    except ProfileError as error:
        return {"schema":RESPONSE,"status":"execution-profile-not-supported","program":None,
                "diagnostic":{"phase":phase,"code":error.code,"path":list(error.path),"message":str(error)}}
    except (ValueError,UnicodeError) as error:
        return {"schema":RESPONSE,"status":"static-rejected" if phase=="source" else "infrastructure-error","program":None,
                "diagnostic":{"phase":phase,"code":type(error).__name__,"path":[],"message":str(error)}}
    except Exception as error:
        return {"schema":RESPONSE,"status":"infrastructure-error","program":None,
                "diagnostic":{"phase":phase,"code":type(error).__name__,"path":[],"message":str(error)}}
