"""Immutable, structurally parsed P8 core AST. No evaluation or runtime tags."""
from dataclasses import dataclass
from typing import Any

SORTS=frozenset({'Owner','Epoch','Scope','Job','Task','Invocation','Access','Lease',
    'Pin','Command','Wait','Fault','Region','Registration','Size'})

@dataclass(frozen=True)
class Name:
    text:str
    sort:str

@dataclass(frozen=True)
class Size:
    literal:int|None=None
    witness:str|None=None

@dataclass(frozen=True)
class Witness:
    name:str|None=None
    size:Size|None=None

@dataclass(frozen=True)
class Relation:
    tag:str
    fields:tuple[tuple[str,Any],...]
    def get(self,key):return dict(self.fields)[key]

@dataclass(frozen=True)
class Telescope:
    names:tuple[Name,...]
    facts:tuple[Relation,...]

@dataclass(frozen=True)
class Foot:
    tag:str
    names:tuple[str,...]

@dataclass(frozen=True)
class Effect:
    tag:str
    fields:tuple[tuple[str,Any],...]=()
    def get(self,key):return dict(self.fields)[key]

@dataclass(frozen=True)
class Type:
    tag:str
    fields:tuple[tuple[str,Any],...]=()
    def get(self,key):return dict(self.fields)[key]

@dataclass(frozen=True)
class Node:
    family:str
    tag:str
    fields:tuple[tuple[str,Any],...]=()
    def get(self,key):return dict(self.fields)[key]

@dataclass(frozen=True)
class Theta:
    job:str
    task:str
    invocation:str
    region:str
    scope_stack:tuple[str,...]

@dataclass(frozen=True)
class Binding:
    name:str
    type:Type

@dataclass(frozen=True)
class PureSignature:
    name:str
    parameters:tuple[Type,...]
    result:Type

@dataclass(frozen=True)
class KernelSignature:
    name:str
    size_binder:Name|None
    parameter:Type
    size:Size
    postcondition:str

@dataclass(frozen=True)
class Signature:
    pure:tuple[PureSignature,...]
    kernels:tuple[KernelSignature,...]

@dataclass(frozen=True)
class Program:
    signature:Signature
    world:Telescope
    theta:Theta
    copy:tuple[Binding,...]
    affine:tuple[Binding,...]
    body:Node
    result:Type

class FrontendError(ValueError):pass
class ParseError(FrontendError):pass
class TypeError(FrontendError):pass
class Unsupported(TypeError):pass

def fields(**kwargs):return tuple(kwargs.items())

# Every source constructor is represented, including the explicit names that
# a surface elaborator would synthesize. No unknown constructor falls through.
VALUE_FIELDS={
    'var':{'name':'id'},'unit':{},'bool':{'value':'bool'},'nat':{'value':'nat'},
    'word':{'value':'int'},'vector':{'items':'ints'},'pair':{'left':'v','right':'v'},
    'inl':{'left_type':'type','right_type':'type','value':'v'},
    'inr':{'left_type':'type','right_type':'type','value':'v'},
    'lambda':{'mode':'mode','captures':'ids','foot':'foot','effect':'effect',
        'parameter':'id','parameter_type':'type','result_type':'type','body':'c'},
    'pack':{'interface':'telescope','witnesses':'witnesses','payload_type':'type','value':'v'},
}
EXPR_FIELDS={'value':{'value':'v'},'fst':{'value':'v'},'snd':{'value':'v'},
    'op':{'operator':'id','arguments':'values'}}
COMMAND_FIELDS={
    'return':{'value':'v'},'let':{'name':'id','expression':'e','next':'c'},
    'bind':{'name':'id','command':'c','next':'c'},
    'call':{'name':'id','function':'v','argument':'v','next':'c'},
    'if':{'condition':'v','then':'c','else':'c'},
    'case':{'value':'v','left_name':'id','left':'c','right_name':'id','right':'c'},
    'unpack':{'interface':'telescope','name':'id','value':'v','body':'c'},
    'for':{'index':'id','bound':'v','body':'c','next':'c'},
    'throw':{'value':'v','result_type':'type'},'rethrow':{'value':'v','result_type':'type'},
    'try':{'body':'c','failure_name':'id','handler':'c'},
    'new_owner':{'size':'v','size_name':'id','owner_name':'id','variable':'id','body':'c'},
    'scope':{'scope_name':'id','region_name':'id','invocation_name':'id','variable':'id','body':'c'},
    'run':{'owner':'v','epoch_name':'id','scope_name':'id','region_name':'id',
        'invocation_name':'id','variable':'id','body':'c'},
    'produce':{'scope':'v','kernel':'id','argument':'v','names':'producer_names',
        'read_name':'id','cleanup':'c','shared_name':'id','control_name':'id','next':'c'},
    'fork':{'scope':'v','names':'job_names','body':'c','shared_name':'id','control_name':'id','next':'c'},
    'lease':{'scope':'v','producer':'v','lease_name':'id','variable':'id','next':'c'},
    'fork_read':{'scope':'v','lease':'v','names':'reader_names','read_name':'id',
        'body':'c','shared_name':'id','control_name':'id','next':'c'},
    'load':{'name':'id','read':'v','index':'v','next':'c'},
    'await':{'name':'id','shared':'v','next':'c'},
    'await_cancel':{'name':'id','shared':'v','next':'c'},
    'request_cancel':{'control':'v','next':'c'},'drop':{'value':'v','next':'c'},
}
