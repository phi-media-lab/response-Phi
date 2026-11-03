"""Fixed host registry; serialized registrations never grant authority."""
from dataclasses import dataclass
import json
from p8foundation.model import node, require

SIGMA_ID="p8-src1-fixed-sigma-v1"
SIGMA_ABI="p8-src1-runtime-sigma-abi-v1"
OPERATIONS=(("Prefill32","kernel","qwen3-week3-mlx-prefill32-token1-v1"),
            ("Prefill8","kernel","qwen3-week3-mlx-prefill8-token1-v1"),
            ("body_error","pure","body-error-unit-v1"),
            ("B","host-input","ebd1-receive-request-tokens8-v1"),
            ("B.response","host-response","src1-offer-response-word-v1"))
# Immutable host constants, never source-supplied capabilities. A fresh mapping
# is returned at each wire boundary; the internal registry uses an immutable node.
BACKEND_CONTRACT=(("id","p8-cm1-mlx-backend-return-v1"),
                  ("allocation_kind","MLXPrefillOwner1"),("word_slots",1),
                  ("private_kv_layers",28),("return_obligation","BackendReturn-v1"),
                  ("unknown_policy","host-incomplete-sticky-v1"))
HOST_INPUT_CONTRACT=(("id","p8-ebd1-external-request-v1"),
                     ("port","B"),("command","receive_request"),
                     ("result_type","Tokens8"),("caller","init-main-job-only"),
                     ("owner_authority","identity-only-no-storage-access"),
                     ("submission","single-irrevocable-complete-request-v1"))
HOST_RESPONSE_CONTRACT=(("id","src1-explicit-response-intent-v1"),
                        ("port","B"),("registry_name","B.response"),
                        ("command","offer_response"),("payload_type","Word"),
                        ("result_type","Unit"),("caller","init-main-job-only"),
                        ("owner_authority","identity-only-no-storage-access"),
                        ("submission","single-valid-intent-after-receipt-v1"),
                        ("commit","original-main-joined-all-source-safe-v1"))

def backend_contract(): return dict(BACKEND_CONTRACT)
def checked_backend_contract():
    return node("signature","backend-return-contract",**backend_contract())
def host_input_contract(): return dict(HOST_INPUT_CONTRACT)
def checked_host_input_contract():
    return node("signature","host-input-contract",**host_input_contract())
def host_response_contract(): return dict(HOST_RESPONSE_CONTRACT)
def checked_host_response_contract():
    return node("signature","host-response-contract",**host_response_contract())

def tokens_type(count):
    result=node("type","Word")
    for _ in range(count-1): result=node("type","Pair",left=node("type","Word"),right=result)
    return result

@dataclass(frozen=True)
class FixedSigma:
    profile="src1-response-offer-owner1-v1"
    parent=False
    def checked(self):
        return node("signature","checked-registry",id=SIGMA_ID,abi=SIGMA_ABI,
                    entries=tuple(node("signature","fixed",name=n,kind=k,implementation_id=i) for n,k,i in OPERATIONS),
                    backend_contract=checked_backend_contract(),
                    host_input_contract=checked_host_input_contract(),
                    host_response_contract=checked_host_response_contract())
    def pure(self,name):
        require(name=="body_error","unregistered pure operation")
        return (node("type","Unit"),),node("type","ErrorPayload")
    def kernel(self,name):
        require(name in ("Prefill32","Prefill8"),"unregistered kernel")
        return tokens_type(32 if name=="Prefill32" else 8),1
    def backend_return(self): return checked_backend_contract()
    def host_input(self,name):
        require(name=="B","unregistered host-input port")
        return tokens_type(8)
    def host_input_boundary(self): return checked_host_input_contract()
    def host_response(self,name):
        require(name=="B","unregistered host-response port")
        return node("type","Word"),node("type","Unit")
    def host_response_boundary(self): return checked_host_response_contract()

def fixed_sigma(): return FixedSigma()
def check_sigma(value):
    require(type(value) in (FixedSigma,FixedParentSigma),"fixed host Sigma required")
    return value
def wire_sigma(sigma=None):
    if sigma is not None:
        check_sigma(sigma)
        if sigma.parent:return json.loads(PARENT_WIRE_JSON)
    return {"id":SIGMA_ID,"abi":SIGMA_ABI,
            "operations":[{"name":n,"kind":k,"implementation":i} for n,k,i in OPERATIONS],
            "backend_contract":backend_contract(),"host_input_contract":host_input_contract(),
            "host_response_contract":host_response_contract()}


# Immutable fixed trusted registry text; every wire caller gets a fresh object.
PARENT_WIRE_JSON='{"id":"p8-src1b-parent-fixed-sigma-v1","abi":"p8-src1b-parent-runtime-sigma-abi-v1","operations":[{"name":"body_error","kind":"pure","implementation":"body-error-unit-v1"},{"name":"B","kind":"host-service","implementation":"src1b-leaf-response-host-close-v1"},{"name":"tokenizer.vocab_symbol","kind":"readonly-tool","implementation":"src1b-vocab-symbol-utf8-sha256-v1"}],"host_parent_contract":{"id":"src1b-host-parent-v1","execution_domain":"hostParent","direct_backend":"none","caller":"init-main-job-only","scope_kind":"plain","service_instances":1,"scope_exit":"actual-host-close-before-parent-publication-v1","registry":"fixed-leaf-no-parent-callback-v1"},"host_service_contract":{"id":"src1b-response-and-host-close-v1","name":"B","command":"call_service","argument_type":"Tokens8","result_type":"Word","child_profile":"src1-response-offer-owner1-v1","delivery":"after-source-join","response":"original-commit-requester-delivered-once-v1","responsibility":"parent-scope-HostClose-v1","submission":"reserve-gated-commit-start-v1"},"tool_contract":{"id":"src1b-tokenizer-vocab-symbol-v1","name":"tokenizer.vocab_symbol","command":"call_tool","capability":"tokenizer_vocab_v1","argument_type":"Word","result_type":"ToolInfo-v1","representation":"bpe-vocab-symbol-utf8-v1","authority_sha256":"aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4","max_file_bytes":12582912,"work_budget_ns":2000000000,"added_token_policy":"error-unsupported","missing_token_policy":"error-missing","effect":"readonly-file-io-plus-raise-v1"}}'
PARENT_FORBIDDEN=frozenset(("new_owner","run","produce","fork","receive_request","offer_response","await","await_cancel","request_cancel","load"))


def tool_info_type():
    return node("type","Pair",left=node("type","Nat"),right=tokens_type(4))


@dataclass(frozen=True)
class FixedParentSigma:
    profile="src1b-parent-host-close-v1"
    parent=True
    def checked(self):
        wire=wire_sigma(self)
        return node("signature","checked-parent-registry",id=wire["id"],abi=wire["abi"],
                    entries=tuple(node("signature","fixed",name=o["name"],kind=o["kind"],implementation_id=o["implementation"]) for o in wire["operations"]),
                    host_parent_contract=node("signature","host-parent-contract",**wire["host_parent_contract"]),
                    host_service_contract=self.host_service_boundary(),tool_contract=self.tool_boundary())
    def pure(self,name):
        require(name=="body_error","unregistered pure operation")
        return (node("type","Unit"),),node("type","ErrorPayload")
    def host_service(self,name):
        require(name=="B","unregistered host-service name")
        return tokens_type(8),node("type","Word")
    def host_service_boundary(self):
        return node("signature","host-service-contract",**wire_sigma(self)["host_service_contract"])
    def tool(self,name):
        require(name=="tokenizer.vocab_symbol","unregistered readonly-tool name")
        return node("type","Word"),tool_info_type()
    def tool_boundary(self):
        return node("signature","readonly-tool-contract",**wire_sigma(self)["tool_contract"])


def fixed_parent_sigma(): return FixedParentSigma()


def validate_source_profile(sigma,source,*,closed):
    """Declarations do not acquire capabilities merely because parser knows atoms."""
    check_sigma(sigma)
    require(not sigma.parent or closed,"parent profile requires private closed INIT main")
    stack=[source]
    while stack:
        value=stack.pop()
        if type(value) is dict:
            command=value.get("command")
            if sigma.parent:
                require(type(command) is not str or command not in PARENT_FORBIDDEN,"command prohibited by parent profile: "+str(command))
            else:
                require(command not in ("call_service","call_tool"),"parent command prohibited by child profile")
                require(value.get("atom") not in ("HostService","LocalRead"),"parent effect prohibited by child profile")
            stack.extend(value.values())
        elif type(value) is list:stack.extend(value)
