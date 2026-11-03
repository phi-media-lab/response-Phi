#!/usr/bin/env python3
"""Pure SRC1 decoder-input construction, never a decoder or candidate runner.

Input is an actually saved source/ready compiler exchange and its actual exit.
Every negative starts from an independent deepcopy. Original raw bytes remain
available; expected diagnostics are source-derived predictions until Swift runs.
Only Python stdlib is imported. This module never imports the frontend/runtime.
"""
import argparse
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path

SCHEMA="src1-response-decoder-mutations-v1"
PROFILE="src1-response-offer-owner1-v1"
CONTRACT_KEYS=("id","port","registry_name","command","payload_type","result_type",
               "caller","owner_authority","submission","commit")
CASE_COUNT=49
DECODER_BASIS={"file":"Sources/RuntimeCore/Program.swift","revision":"SRC1-R1",
               "bytes":51129,"sha256":"404f725c0a5b25d541ff07dc138cc1cd43120bd4bd9128eaeb5fb36228ca46b0",
               "basis_note":"R1 concrete Owner/Size identities and complete lexical binding correspondence; frozen source manifest supplies exact compiled identity."}


def require(condition,message):
    if not condition: raise ValueError(message)


def sha(data): return hashlib.sha256(data).hexdigest()
def encode(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")
def pointer(path): return "/"+"/".join(str(x).replace("~","~0").replace("/","~1") for x in path)


def strict_json(data):
    def pairs(items):
        out={}
        for key,value in items:
            require(key not in out,"duplicate input JSON key: "+key);out[key]=value
        return out
    def reject(value): raise ValueError("non-integer JSON token: "+value)
    return json.loads(data.decode("utf-8"),object_pairs_hook=pairs,parse_float=reject,parse_constant=reject)


def at(value,path):
    for key in path: value=value[int(key)] if isinstance(value,list) else value[key]
    return value


def replacing(value,path,replacement):
    parent=at(value,path[:-1]);key=int(path[-1]) if isinstance(parent,list) else path[-1]
    parent[key]=deepcopy(replacement)


def differences(left,right,path=()):
    """Exact changed leaves, plus explicit added/deleted members or elements."""
    if type(left) is type(right) and left==right: return []
    if type(left) is dict and type(right) is dict:
        rows=[]
        for key in sorted(left.keys()|right.keys()):
            if key in left and key in right: rows.extend(differences(left[key],right[key],path+(key,)))
            else: rows.append({"pointer":pointer(path+(key,)),"old_present":key in left,
                               "new_present":key in right,"old":left.get(key),"new":right.get(key)})
        return rows
    if type(left) is list and type(right) is list:
        rows=[]
        for index in range(max(len(left),len(right))):
            if index<len(left) and index<len(right): rows.extend(differences(left[index],right[index],path+(index,)))
            else: rows.append({"pointer":pointer(path+(index,)),"old_present":index<len(left),
                               "new_present":index<len(right),"old":left[index] if index<len(left) else None,
                               "new":right[index] if index<len(right) else None})
        return rows
    return [{"pointer":pointer(path),"old_present":True,"new_present":True,"old":left,"new":right}]


def references(value,path=()):
    if type(value) is dict:
        if set(value)=={"ref"}:
            text=value["ref"];require(type(text) is str and text.isdecimal(),"canonical ref text")
            require(text==str(int(text)),"canonical ref index")
            yield int(text),path
        else:
            for key,child in value.items(): yield from references(child,path+(key,))
    elif type(value) is list:
        for index,child in enumerate(value): yield from references(child,path+(index,))


class Table:
    """Small DAG selector/COW editor; does not reconstruct a type checker."""
    def __init__(self,values):
        self.values=values
        require(type(values) is list and len(values)<=131072,"bounded static table")
        for index,node in enumerate(values):
            require(all(0<=ref<index for ref,_ in references(node)),"original DAG postorder")

    def resolve(self,value):
        budget=len(self.values)+1
        while type(value) is dict and set(value)=={"ref"}:
            require(budget>0,"ref cycle");budget-=1
            index=int(value["ref"]);require(0<=index<len(self.values),"ref bounds")
            value=self.values[index]
        return value

    def member(self,root,key):
        node=self.resolve(root)
        if type(key) is int: return node["array"][key]
        pairs=node["object"];matches=[value for name,value in pairs if name==key]
        require(len(matches)==1,"one static member: "+key);return matches[0]

    def path(self,root,keys):
        for key in keys: root=self.member(root,key)
        return root

    def scalar(self,root,keys=()): return self.resolve(self.path(root,keys))

    def appended(self,node):
        index=len(self.values);require(index<131072,"mutated static table limit")
        require(all(ref<index for ref,_ in references(node)),"appended DAG postorder")
        self.values.append(deepcopy(node));return {"ref":str(index)}

    def cow(self,root,keys,transform):
        """Clone only the selected path; all original nodes stay byte-equivalent."""
        node=deepcopy(self.resolve(root))
        if not keys: return self.appended(transform(node))
        key=keys[0]
        if type(key) is int:
            node["array"][key]=self.cow(node["array"][key],keys[1:],transform)
        else:
            matches=[i for i,pair in enumerate(node["object"]) if pair[0]==key]
            require(len(matches)==1,"COW static member exists: "+key)
            i=matches[0];node["object"][i][1]=self.cow(node["object"][i][1],keys[1:],transform)
        return self.appended(node)

    def impact_if_shared_node_were_changed(self,root,program):
        require(set(root)=={"ref"},"selected annotation root ref")
        index=int(root["ref"]);affected={index};direct=[]
        for other in range(index+1,len(self.values)):
            refs={ref for ref,_ in references(self.values[other])}
            if index in refs: direct.append(other)
            if refs & affected: affected.add(other)
        uses=[]
        for key in ("sites","entry"):
            for ref,path in references(program[key],("program",key)):
                if ref in affected: uses.append({"pointer":pointer(path),"ref_index":ref})
        return {"original_ref":index,"original_direct_referrers":direct,
                "original_transitive_referrers":sorted(affected),"original_dependent_uses":uses,
                "note":"These would share an in-place change. This fixture keeps every original table node intact and replaces only the selected annotation root with a COW path."}


def drop_member(name):
    def edit(node):
        require(sum(k==name for k,_ in node["object"])==1,"drop existing static field")
        node["object"]=[pair for pair in node["object"] if pair[0]!=name];return node
    return edit


def add_member(name,value):
    def edit(node):
        require(all(k!=name for k,_ in node["object"]),"add fresh static field")
        node["object"].append([name,deepcopy(value)]);return node
    return edit


def replace_identity(value,old,new):
    """Source edits rebind every original hash leaf, including shared DAG leaves."""
    if type(value) is str: return new if value==old else value
    if type(value) is dict: return {k:replace_identity(v,old,new) for k,v in value.items()}
    if type(value) is list: return [replace_identity(v,old,new) for v in value]
    return value


def diagnostic(message):
    return {"phase":"protocol","code":"invalid-compiler-exchange","path":[],"message":message}


def mutations(source_bytes,response_bytes,actual_exit_code):
    """Yield 49 independent {source_bytes,response_bytes,record} cases.

    The original compiler result must contain exactly one reachable offer and
    one receive (the actual unit_after_b export). Baseline acceptance by Swift
    remains the harness's first obligation; this function performs no decode.
    """
    require(actual_exit_code==0,"actual ready compiler exit must be zero")
    require(len(source_bytes)<=1048576 and len(response_bytes)<=67108864,"input byte limits")
    source=strict_json(source_bytes);original=strict_json(response_bytes)
    require(original.get("status")=="ready" and original.get("diagnostic") is None,"actual ready exchange required")
    require(original.get("schema")=="p8-runtime-frontend-response-v1","frontend response schema")
    program=original["program"];record=program["source"]
    require(program["profile"]==PROFILE,"actual SRC1 profile")
    require(base64.b64decode(record["utf8_base64"],validate=True)==source_bytes,"actual source bytes embedded")
    require(record["bytes_sha256"]==sha(source_bytes) and record["canonical_sha256"]==sha(encode(source)),"source identities")
    rows=program["sites"];offers=[i for i,r in enumerate(rows) if r["node_kind"]=="command:offer_response"]
    require(len(offers)==1 and rows[offers[0]]["execution"]=="reachable","one actual reachable offer required")
    require(sum(r["node_kind"]=="command:receive_request" and r["execution"]=="reachable" for r in rows)==1,"one original receive required")
    oi=offers[0];site=rows[oi]["site"]["pointer"]
    oi_source=tuple(x.replace("~1","/").replace("~0","~") for x in site[1:].split("/"))
    require(at(source,oi_source).get("command")=="offer_response","actual offer source occurrence")
    indices={r["site"]["pointer"]:i for i,r in enumerate(rows)}
    require(len(indices)==len(rows),"unique original site paths")
    owner_i,token_i=indices[site+"/owner"],indices[site+"/token"]
    require(rows[owner_i]["node_kind"]=="value:var" and len(rows[owner_i]["usage"]["copy"])==1 and not rows[owner_i]["usage"]["move"],"actual Owner copied variable")
    table=Table(program["static_values"])
    unit_ref,word_ref,owner_ref=rows[oi]["type"],rows[token_i]["type"],rows[owner_i]["type"]
    for ref,tag in ((unit_ref,"Unit"),(word_ref,"Word"),(owner_ref,"Owner")):
        require(table.scalar(ref,("family",))=="type" and table.scalar(ref,("tag",))==tag,"actual operand/result type: "+tag)
    require(table.resolve(table.path(unit_ref,("fields",)))=={"object":[]} and table.resolve(table.path(word_ref,("fields",)))=={"object":[]},"actual fieldless Unit/Word")
    require({k for k,_ in table.resolve(table.path(owner_ref,("fields",)))["object"]}=={"owner","size"},"actual complete Owner fields")
    require(table.scalar(owner_ref,("fields","owner","nominal","index"))=="1","actual new-owner Owner index")
    size_ref=table.path(owner_ref,("fields","size"))
    require(table.scalar(size_ref,("nominal","sort"))=="Size","actual symbolic Size")
    # Select an actual literal witness already emitted for NEW's size equality;
    # never invent an original type/value or replace the ready exchange input.
    literal_refs=[]
    for i,node in enumerate(table.values):
        if type(node) is dict and set(node)=={"object"} and len(node["object"])==1 and node["object"][0][0]=="size_literal":
            if table.resolve(node["object"][0][1])=={"integer":"1"}: literal_refs.append({"ref":str(i)})
    require(len(literal_refs)==1,"one actual Size literal 1 static node")
    literal_one=literal_refs[0]
    eps=rows[oi]["effect"]
    require(table.scalar(eps,("family",))=="effect" and table.scalar(eps,("tag",))=="set","actual offer effect set")
    atoms=table.resolve(table.path(eps,("fields","atoms")))["array"]
    require(len(atoms)==2,"actual two effect atoms")
    tags=[table.scalar(a,("tag",)) for a in atoms]
    require(sorted(tags)==["HostResponse","Raise"],"actual response/Raise atoms")
    ri,xi=tags.index("HostResponse"),tags.index("Raise")
    operations=program["sigma"]["operations"]
    response_ops=[i for i,op in enumerate(operations) if op["name"]=="B.response"]
    require(len(response_ops)==1,"actual response registry entry")
    opi=response_ops[0]
    require(set(program["sigma"]["host_response_contract"])==set(CONTRACT_KEYS),"actual ten-field response contract")

    def emit(case,category,message,change,first_boundary,source_change=None,selection=None):
        changed=deepcopy(original);new_source=deepcopy(source);alias=None
        if selection is not None:
            alias=table.impact_if_shared_node_were_changed(at(original,selection),program)
        change(changed)
        data=source_bytes
        if source_change is not None:
            source_change(new_source);data=encode(new_source)
            changed=replace_identity(changed,record["canonical_sha256"],sha(data))
            changed["program"]["source"]={"utf8_base64":base64.b64encode(data).decode("ascii"),
                "bytes_sha256":sha(data),"canonical_sha256":sha(data)}
        wire_diffs=differences(original,changed);source_diffs=differences(source,new_source)
        require(wire_diffs,"mutation must actually change exchange")
        if source_change is None:
            require(changed["program"]["static_values"][:len(program["static_values"])]==program["static_values"],"original shared table nodes unchanged")
        metadata={"case":case,"category":category,"expected_exception":"RuntimeCore.ProtocolError",
            "expected_diagnostic":diagnostic(message),"expected_first_boundary":first_boundary,
            "actual_decoder_executed":False,"actual_compiler_exit_supplied":actual_exit_code,
            "selected_offer_site":site,"source_ast_changes":source_diffs,"source_ast_change_count":len(source_diffs),
            "response_wire_changes":wire_diffs,"response_wire_change_count":len(wire_diffs),
            "original_table_entries":len(program["static_values"]),
            "appended_table_entries":len(changed["program"]["static_values"])-len(program["static_values"]),
            "original_shared_alias_scope":alias,
            "source_identity_policy":"one explicit AST edit plus all derived source identity rewrites" if source_change else "original exact source bytes unchanged",
            "scope_note":"Prediction from pinned decoder source; requires real positive decode and exact diagnostic observation. No request, receipt, once, lifecycle or performance assertion."}
        return {"source_bytes":data,"response_bytes":encode(changed),"record":metadata}

    def scalar_case(case,path,value,message,category="registry",boundary="decodeExchange fixed profile/Sigma equality"):
        return emit(case,category,message,lambda r:replacing(r,path,value),boundary)

    def cow_case(case,annotation,keys,transform,message):
        def change(r):
            editor=Table(r["program"]["static_values"])
            replacing(r,annotation,editor.cow(at(r,annotation),keys,transform))
        return emit(case,"annotation-cow",message,change,"checkBindings reachable offer contract",selection=annotation)

    yield scalar_case("P01-profile",("program","profile"),"ebd1-external-request-owner1-v1","program version")
    yield scalar_case("P02-sigma-id",("program","sigma","id"),"p8-ebd1-fixed-sigma-v1","fixed Sigma implementations")
    yield scalar_case("P03-sigma-abi",("program","sigma","abi"),"p8-ebd1-runtime-sigma-abi-v1","fixed Sigma implementations")
    for case,key,value in (("P04-operation-name","name","B"),("P05-operation-kind","kind","host-input"),
                           ("P06-operation-implementation","implementation","ebd1-receive-request-tokens8-v1")):
        yield scalar_case(case,("program","sigma","operations",opi,key),value,"fixed Sigma implementations")
    for index,key in enumerate(CONTRACT_KEYS,1):
        old=program["sigma"]["host_response_contract"][key]
        require(type(old) is str,"contract scalar string")
        yield scalar_case("C%02d-contract-%s"%(index,key),("program","sigma","host_response_contract",key),old+"-mutated","fixed Sigma implementations")

    def source_edit(case,edit,message):
        return emit(case,"source-and-derived-identities",message,lambda r:None,
                    "walkSource offer port/strict source fields before child traversal",lambda s:edit(at(s,oi_source)))
    yield source_edit("S01-port",lambda n:n.__setitem__("name","C"),"fixed host-response port")
    yield source_edit("S02-extra-field",lambda n:n.__setitem__("receipt","forged"),"object fields do not match schema")
    yield source_edit("S03-missing-owner",lambda n:n.pop("owner"),"object fields do not match schema")
    yield source_edit("S04-missing-token",lambda n:n.pop("token"),"object fields do not match schema")

    result_path=("program","sites",oi,"type");owner_path=("program","sites",owner_i,"type")
    token_path=("program","sites",token_i,"type");effect_path=("program","sites",oi,"effect")
    for case,path,value,message in (("A01-result-Word",result_path,word_ref,"host response result Unit contract"),
          ("A02-token-Unit",token_path,unit_ref,"host response token Word contract"),
          ("A03-owner-Word",owner_path,word_ref,"host response Owner contract")):
        yield scalar_case(case,path,value,message,"annotation-reference","checkBindings reachable offer contract")
    yield cow_case("A04-owner-missing-owner",owner_path,("fields",),drop_member("owner"),"host response Owner identity")
    yield cow_case("A05-owner-missing-size",owner_path,("fields",),drop_member("size"),"host response Owner identity")
    yield cow_case("A06-owner-nominal-index",owner_path,("fields","owner","nominal","index"),lambda n:"0","host response Owner nominal sort/source")
    yield cow_case("A07-owner-null",owner_path,("fields","owner"),lambda n:None,"host response Owner nominal form")
    yield cow_case("A08-owner-wrong-sort",owner_path,("fields","owner"),lambda n:deepcopy(table.resolve(size_ref)),"host response Owner nominal sort/source")
    yield cow_case("A09-size-null",owner_path,("fields","size"),lambda n:None,"host response Size nominal form")
    yield cow_case("A10-size-wrong-sort",owner_path,("fields","size"),lambda n:deepcopy(table.resolve(table.path(owner_ref,("fields","owner")))),"host response Size nominal sort/source")
    def negative_literal(node):
        result=deepcopy(table.resolve(literal_one))
        result["object"][0][1]={"integer":"-1"};return result
    yield cow_case("A11-size-negative-literal",owner_path,("fields","size"),negative_literal,"host response Size literal nonnegative")
    yield cow_case("A12-owner-binding-mismatch",owner_path,("fields","size"),lambda n:deepcopy(table.resolve(literal_one)),"host response Owner complete binding correspondence")

    yield cow_case("E01-effect-tag",effect_path,("tag",),lambda n:"union","host response effect set")
    yield cow_case("E02-effect-family",effect_path,("family",),lambda n:"type","host response effect set")
    yield cow_case("E03-effect-extra-field",effect_path,("fields",),add_member("extra",unit_ref),"host response effect atoms")
    yield cow_case("E04-effect-arity",effect_path,("fields","atoms"),lambda n:{"array":[n["array"][ri]]},"host response exact effect arity")
    yield cow_case("E05-response-atom-missing",effect_path,("fields","atoms",ri,"tag"),lambda n:"HostInput","host response and Raise effects")
    yield cow_case("E06-raise-atom-missing",effect_path,("fields","atoms",xi,"tag"),lambda n:"Alloc","host response and Raise effects")
    yield cow_case("E07-response-extra-field",effect_path,("fields","atoms",ri,"fields"),add_member("port",unit_ref),"host response effect fields")
    yield cow_case("E08-raise-extra-field",effect_path,("fields","atoms",xi,"fields"),add_member("owner",table.path(owner_ref,("fields","owner"))),"host response effect fields")
    yield cow_case("E09-atom-family",effect_path,("fields","atoms",ri,"family"),lambda n:"type","host response effect fields")
    yield cow_case("E10-effect-owner-nominal-index",effect_path,("fields","atoms",ri,"fields","owner","nominal","index"),lambda n:"0","host response effect Owner correspondence")

    for case,index in (("T01-missing-owner-annotation",owner_i),("T02-missing-token-annotation",token_i)):
        yield emit(case,"site-annotation","missing AST annotation",lambda r,i=index:r["program"]["sites"].pop(i),"walkSource child lookup before checkBindings")
    yield scalar_case("T03-node-kind",("program","sites",token_i,"node_kind"),"expression:value","AST/annotation kind","site-annotation","walkSource child family/tag correspondence")
    def swap(r):
        sites=r["program"]["sites"];sites[owner_i],sites[token_i]=sites[token_i],sites[owner_i]
    yield emit("T04-child-order","site-annotation","exact source occurrence order",swap,"decodeExchange source walk order after traversal")
    yield scalar_case("T05-site-hash",("program","sites",owner_i,"site","source_sha256"),"0"*64,"site source identity","site-annotation","ProgramDecoder.annotation/site before source walk")
    yield emit("T06-duplicate-site","site-annotation","duplicate source site",lambda r:r["program"]["sites"].append(deepcopy(r["program"]["sites"][owner_i])),"decodeExchange unique sites before source walk")
    yield scalar_case("B01-owner-direct-usage",("program","sites",owner_i,"usage","copy"),[],"host response Owner lexical binding","binding","checkBindings offer reads Owner operand's direct binding before that variable row")


def file_pin(path,data): return {"path":str(path.resolve()),"bytes":len(data),"sha256":sha(data)}


def save_bytes(path,data):
    with path.open("xb") as stream: stream.write(data)
    return file_pin(path,data)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--response",type=Path,required=True)
    parser.add_argument("--actual-exit-code",type=int,required=True)
    parser.add_argument("--offer-only-source",type=Path,required=True)
    parser.add_argument("--offer-only-response",type=Path,required=True)
    parser.add_argument("--offer-only-actual-exit-code",type=int,required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args()
    require(all(p.is_absolute() for p in (args.source,args.response,args.offer_only_source,args.offer_only_response,args.output_dir)),"absolute input/output paths required")
    source=args.source.read_bytes();response=args.response.read_bytes()
    only_source=args.offer_only_source.read_bytes();only_response=args.offer_only_response.read_bytes()
    args.output_dir.mkdir(parents=True,exist_ok=False)
    original_dir=args.output_dir/"original";original_dir.mkdir()
    originals={"source":save_bytes(original_dir/"source.json",source),
               "response":save_bytes(original_dir/"frontend.stdout",response)}
    only_dir=args.output_dir/"offer-only-original";only_dir.mkdir()
    only_copies={"source":save_bytes(only_dir/"source.json",only_source),
                 "response":save_bytes(only_dir/"frontend.stdout",only_response)}
    provenance={"schema":SCHEMA,"constructor":file_pin(Path(__file__),Path(__file__).read_bytes()),
        "inputs":{"source":file_pin(args.source,source),"response":file_pin(args.response,response),
                  "offer_only_source":file_pin(args.offer_only_source,only_source),"offer_only_response":file_pin(args.offer_only_response,only_response)},
        "original_copies":originals,"actual_compiler_exit_supplied":args.actual_exit_code,
        "offer_only_copies":only_copies,"offer_only_actual_compiler_exit_supplied":args.offer_only_actual_exit_code,
        "decoder_source_basis":DECODER_BASIS,"decoder_executed":False,"candidate_executed":False}
    save_bytes(args.output_dir/"preparation.json",encode(provenance)+b"\n")
    cases=[]
    try:
        require(args.offer_only_actual_exit_code==0 and len(only_source)<=1048576 and len(only_response)<=67108864,"actual bounded offer-only ready exchange")
        only=strict_json(only_response);only_ast=strict_json(only_source)
        require(only.get("schema")=="p8-runtime-frontend-response-v1" and only.get("status")=="ready" and only.get("diagnostic") is None,"actual offer-only ready response")
        op=only["program"];sr=op["source"]
        require(op["profile"]==PROFILE and base64.b64decode(sr["utf8_base64"],validate=True)==only_source,"offer-only actual source/profile")
        require(sr["bytes_sha256"]==sha(only_source) and sr["canonical_sha256"]==sha(encode(only_ast)),"offer-only source identities")
        require(sum(r["node_kind"]=="command:offer_response" and r["execution"]=="reachable" for r in op["sites"])==1,"one actual offer-only occurrence")
        require(not any(r["node_kind"]=="command:receive_request" for r in op["sites"]),"offer-only has no receive occurrence")
        for case in mutations(source,response,args.actual_exit_code):
            row=case["record"];directory=args.output_dir/row["case"];directory.mkdir()
            row["source"]=save_bytes(directory/"source.json",case["source_bytes"])
            row["response"]=save_bytes(directory/"frontend.stdout",case["response_bytes"])
            saved=save_bytes(directory/"mutation.json",encode(row)+b"\n")
            cases.append({"case":row["case"],"source":row["source"],"response":row["response"],
                          "mutation":saved,"expected_diagnostic":row["expected_diagnostic"]})
        require(len(cases)==CASE_COUNT,"fixed 49-case population")
        require(args.source.read_bytes()==source and args.response.read_bytes()==response,"original inputs unchanged")
        require(args.offer_only_source.read_bytes()==only_source and args.offer_only_response.read_bytes()==only_response,"offer-only inputs unchanged")
        manifest={**provenance,"construction_complete":True,"case_count":len(cases),"cases":cases,
            "fixed_observation_count":53,
            "baseline_first_required":"Swift decodes both exact originals at actual compiler exits 0, accepts complete base configured=true, rejects complete base configured=false at receive count, and rejects offer-only configured=false at the offer guard; then 49 fixed decoder mutations.",
            "limits":["No production decoder, compiler, Task, Ledger, transport, worker, model or GPU is invoked.",
                      "Expected failures are predictions, never actual diagnostics or accepted decoder results.",
                      "Owner/Size cases target finite R1 shape/sort/source/literal/binding guards, not a complete independent type checker.",
                      "Runtime request/receipt/token/once/caller/lifecycle and Phase B composition require separate controls."]}
        save_bytes(args.output_dir/"manifest.json",encode(manifest)+b"\n")
        print(json.dumps({"output_dir":str(args.output_dir),"constructed_cases":len(cases),"decoder_executed":False}))
    except BaseException as error:
        failure={**provenance,"construction_complete":False,"completed_cases":cases,
                 "error_type":type(error).__name__,"error":str(error)}
        save_bytes(args.output_dir/"construction-failure.json",encode(failure)+b"\n")
        raise


if __name__=="__main__": main()
