"""Known body templates refine opaque CALL exception origin without executing."""
from p8foundation.model import RNode, Binder, Decision, node
from p8closure.caller import substitute_caller_record, key as caller_key


def local_lambdas(implementation,projection=(),guards=()):
    if not isinstance(implementation,RNode): return ((None,guards),)
    tag=implementation.tag;fields=dict(implementation.fields)
    if tag=="local-lambda" and not projection:
        return ((implementation.get("certificate"),guards),)
    if tag=="project":
        return local_lambdas(implementation.get("parent"),implementation.get("path")+projection,guards)
    if tag=="pair" and projection:
        arm=projection[0]
        return local_lambdas(fields.get(arm),projection[1:],guards)
    if tag=="sum" and projection:
        # CASE proves one active arm. An incompatible injection contributes
        # no possible callee; do not replace that empty set with an opaque one.
        if projection[0]!=implementation.get("arm"): return ()
        return local_lambdas(implementation.get("payload"),projection[1:],guards)
    if tag=="choice":
        return tuple(item for i,branch in enumerate(implementation.get("branches"))
                     for item in local_lambdas(branch,projection,guards+((implementation.get("selector"),i),)))
    if tag in ("closed-opening","closed-generation"):
        return local_lambdas(implementation.get("body"),projection,guards)
    if tag=="open":
        parent=implementation.get("parent")
        if isinstance(parent,RNode) and parent.tag=="pack":
            return local_lambdas(parent.get("payload"),projection,guards)
    return ((None,guards),)


def parameter_view_substitution(value,callee_site,argument_view,call_site):
    """Preserve all record fields while transforming each shared input once.

    The identity cache is local to these exact three substitution parameters.
    The replacement argument is installed unchanged, never recursively rewritten.
    """
    memo={}
    def visit(item):
        if not isinstance(item,(RNode,Binder,Decision,tuple)): return item
        previous=memo.get(id(item))
        if previous is not None:
            if previous[0] is not item: raise RuntimeError("parameter substitution memo lost retained input")
            return previous[1]
        if isinstance(item,RNode):
            fields=dict(item.fields)
            if item.family=="view" and item.tag=="ParameterEntry" and fields.get("site")==callee_site:
                result=node("view","CallParameter",site=call_site,parameter_site=callee_site,parent=argument_view)
            else: result=RNode(item.family,item.tag,tuple((k,visit(v)) for k,v in item.fields))
        elif isinstance(item,Binder):
            result=Binder(item.kind,item.sorts,tuple(visit(f) for f in item.facts),visit(item.body))
        elif isinstance(item,Decision):
            result=Decision(item.outcome,item.rule,visit(item.details),visit(item.premises))
        else: result=tuple(visit(v) for v in item)
        memo[id(item)]=(item,result)
        return result
    try: return visit(value)
    finally: memo.clear()


def exception_postcondition(function,argument,caller,site):
    alternatives=[];opaque=[]
    known=local_lambdas(function.implementation)
    for certificate,guards in known:
        if certificate is None:
            opaque.append(node("call","opaque-exception-alternative",guards=guards,
                               identity_policy="preserve-callee-faults",unwind_to_call_boundary_required=True))
            continue
        binder=certificate.get("caller").get("binder")
        body=substitute_caller_record(certificate.get("body"),binder,caller)
        body=parameter_view_substitution(body,certificate.get("site"),argument.view,site)
        for exceptional in body.get("exceptional"):
            alternatives.append(node("call","exception-template",activation_site=site,
                                     callee_site=certificate.get("site"),guards=guards,
                                     origin=exceptional.get("origin"),
                                     unwind_prerequisites=exceptional.get("frames"),
                                     abandoned=exceptional.get("abandoned"),
                                     fresh_generation_each_activation=True,runtime_executed=False))
    return node("failure","callee-exception-postcondition",site=site,caller=caller_key(caller),
                callee=function.implementation,argument=argument.view,alternatives=tuple(alternatives),
                identity_policy="preserve-callee-faults",origin_policy="callee-origin-not-necessarily-call-invocation",
                unwind_to_call_boundary_required=True,opaque_alternatives=tuple(opaque),
                opaque_alternatives_possible=bool(opaque))


def normal_binding(function,argument,caller,site):
    """Instantiate known caller/parameter templates; runtime brands stay pending."""
    alternatives=[];opaque=[]
    for certificate,guards in local_lambdas(function.implementation):
        if certificate is None:
            opaque.append(node("call","opaque-normal-alternative",guards=guards,
                               obligation="callee-normal-postcondition-at-this-caller"))
            continue
        binder=certificate.get("caller").get("binder")
        body=substitute_caller_record(certificate.get("body"),binder,caller)
        body=parameter_view_substitution(body,certificate.get("site"),argument.view,site)
        for normal in body.get("normal"):
            alternatives.append(node("call","normal-template",activation_site=site,callee_site=certificate.get("site"),
                                     guards=guards,value=normal.get("value"),
                                     caller_substitution=node("call","caller-substitution",binder=binder,replacement=caller_key(caller)),
                                     runtime_name_env="fresh-realization-required",runtime_executed=False))
    return node("call","normal-binding",caller=caller_key(caller),alternatives=tuple(alternatives),
                opaque_alternatives=tuple(opaque),normal_is_conditional=True,
                runtime_generation_realized=False)
