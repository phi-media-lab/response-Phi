"""Ordinary FORK: complete static capture transfer, not runtime admission."""
from p8foundation.model import Binder, Decision, RNode, node, require
from p8foundation.resolve import Resolver
from p8closure.schema import Schema
from p8closure.formation import form_world, form_type, form_effect, form_interface
from p8closure.support import Caller, copyable, stable, support_of
from p8closure.requirements import ProofContext, caller_key, ktrue, holding, require_proof, close_refs
from .binding import free_terms
from .generation import fresh_names, close_interface, close_generated_body

SORTS = ("Job", "Task", "Invocation", "Region")


def proof_tree(value):
    # Decisions are dataclasses, not bindable RNodes. Convert the entire finite
    # evidence tree before closing hidden skolems; do not leak private witnesses.
    if isinstance(value, Decision):
        return node("proof", "decision", outcome=value.outcome, rule=value.rule,
                    details=proof_tree(value.details), premises=proof_tree(value.premises))
    if isinstance(value, tuple): return tuple(proof_tree(v) for v in value)
    if isinstance(value, RNode): return RNode(value.family, value.tag, tuple((k, proof_tree(v)) for k,v in value.fields))
    return value


def admit_support(world, target_region, support):
    """Universal interface proof over target lifetime, never parent ResHold."""
    if isinstance(support, Binder):
        require(support.kind == "support-hidden", "FORK hidden support binder")
        inner, body, refs = form_interface(support, world, "fork_capture")
        evidence = admit_support(inner, target_region, body)
        return Binder("fork-capture-hidden", support.sorts, support.facts, close_refs(evidence, refs))
    if support.tag == "zero": return node("proof", "fork-capture-zero")
    if support.tag == "all":
        return node("proof", "fork-capture-all", items=tuple(admit_support(world, target_region, x) for x in support.get("items")))
    require(support.tag == "atom", "FORK capture support form")
    foot = support.get("foot"); names = foot.get("names")
    require(foot.tag not in ("lease", "read", "write"), "FORK capture contains forbidden storage support")
    require(foot.tag in ("scope", "region", "scope-lifetime"), "FORK capture support kind")
    anchor = world.scope_mapping(names[0]).get("region") if foot.tag == "scope-lifetime" else names[-1]
    goal = node("relation", "outlives", inner=target_region, outer=anchor)
    proof = require_proof(world.entails(goal), "FORK captured lifetime")
    return node("proof", "fork-capture-lifetime", support=support, relation=goal, evidence=proof_tree(proof))


def capture_admissible(world, target_region, typ):
    form_type(typ, world)
    require(copyable(typ), "FORK cannot capture affine value")
    return admit_support(world, target_region, support_of(typ))


def fork_world(checker, raw, state, path, scope_type):
    require(isinstance(scope_type, RNode) and scope_type.tag == "Scope", "FORK needs Scope")
    s, rs = scope_type.get("scope"), scope_type.get("region")
    names, refs = fresh_names(raw, state, SORTS)
    p, task, invocation, region = refs
    source_names = state.pc.world.names if state.source_names is None else state.source_names
    result = Schema(Resolver(source_names + names)).type(raw["job"]["result"])
    facts = (node("relation", "job", job=p, task=task, boundary=node("boundary", "ByScope", scope=s), result=result),
             node("relation", "result", job=p, result=result), node("relation", "jobRegion", job=p, region=region),
             node("relation", "bodyRegion", job=p, role="ordinary", region=region),
             node("relation", "outlives", inner=region, outer=rs))
    world = state.pc.world.extend(names, facts); form_world(world); form_type(result, world)
    require(stable(result), "FORK job result must be Stable")
    child = Caller(p, task, invocation, region, (s,)); child.validate(world)
    interface = close_interface("generation-fork", refs, facts, node("type", "Unit"))
    origin = node("generation", "fork", site=checker.site(path), interface=interface,
                  parent=caller_key(state.pc.caller), child=caller_key(child), scope=s, region=region,
                  captures=tuple(raw["job"]["captures"]), execution=state.execution, runtime_realized=False)
    parent = state.clone(); parent.pc = state.pc.in_world(world)
    if parent.source_names is not None: parent.source_names += names
    parent.generations += tuple((ref, origin, i) for i, ref in enumerate(refs))
    inner = parent.clone(); inner.names = {}; inner.slots = {}
    inner.pc = ProofContext(world, child, ktrue())
    return parent, inner, refs, facts, result, origin


def check_fork(checker, raw, state, path, expected):
    from .body import effect, union, Exceptional
    scope, state, ds = checker.value(raw["scope"], state, path + ("scope",))
    checker.check_hold(scope, state)
    checker.path = path
    parent, inner, refs, facts, result, origin = fork_world(checker, raw, state, path, scope.type)
    captures = tuple(raw["job"]["captures"])
    require(len(captures) == len(set(captures)), "duplicate fork capture")
    require(set(captures) == set(free_terms(raw["job"]["body"])), "exact fork free captures mismatch")
    records = []
    for index, name in enumerate(captures):
        require(name in state.names and state.names[name] in state.slots, "fork capture unavailable: " + name)
        source = state.slots[state.names[name]]; checker.check_hold(source, state)
        admissible = capture_admissible(state.pc.world, scope.type.get("region"), source.type)
        held = require_proof(holding(source.type, inner.pc), "FORK child capture holding")
        view = node("view", "ForkCaptureEntry", site=checker.site(path), index=index, actual=source.view, origin=origin)
        value = checker.made(source.type, view, inner, "fork-capture-entry", (source.hold, admissible, held), source.implementation)
        binder, _ = checker.bind(inner, name, value, path + ("job", "entry", "capture", index), "capture")
        records.append(node("capture", "checked", name=name, binder=binder, outer_binder=state.names[name], value=value.record()))
    body = checker.command(raw["job"]["body"], inner, path + ("job", "body"), result)
    for normal in body.normal:
        checker.check_hold(normal.value, normal.state)
        require(stable(normal.value.type), "FORK job result must be Stable")
        require(not normal.state.affine(), "FORK normal affine remainder requires explicit drop")
    bindings = []
    for name, tag in ((raw["result_name"], "Shared"), (raw["control_name"], "Control")):
        typ = node("type", tag, job=refs[0], **({"payload": result} if tag == "Shared" else {})); form_type(typ, parent.pc.world)
        value = checker.made(typ, node("view", "Forked" + tag, site=checker.site(path), origin=origin), parent,
                             "fork-registration-success", (origin,))
        bid, prior = checker.bind(parent, name, value, path, tag.lower()); bindings.append((name, bid, prior))
    nxt = checker.command(raw["next"], parent, path + ("next",), expected)
    for name, bid, prior in reversed(bindings): nxt = checker.close_binding(nxt, name, bid, prior)
    closed = close_generated_body(checker, nxt, state, refs, origin, path, expected)
    frame = node("frame", "GenBind", site=checker.site(path), caller=caller_key(state.pc.caller),
                 interface=origin.get("interface"), remaining=state.snapshot(), success="move-once-to-next",
                 failure="unwind-without-installing-binders")
    suspended = state.clone()
    for bid in suspended.affine(): del suspended.slots[bid]
    failure = Exceptional(suspended, node("failure", "registration", site=checker.site(path), caller=caller_key(state.pc.caller)), (), (frame,))
    eps = close_interface("nu", refs, facts, union(body.effect, nxt.effect, effect(node("atom", "Register", scope=origin.get("scope")), node("atom", "Raise"))))
    form_effect(eps, state.pc.world)
    finish = node("frame", "JobFinish", site=checker.site(path), job=refs[0], task=refs[1], role="ordinary",
                  source_region=refs[3], job_region=refs[3], runtime_exit=caller_key(Caller(refs[0], refs[1], refs[2], refs[3], ())),
                  remaining=(), completed=False)
    detail = node("generation", "checked-fork", origin=origin, interface=origin.get("interface"), captures=tuple(records),
                  job_finish=finish, child_outcomes=tuple(e.record() for e in body.exceptional), success_realized=False)
    return checker.finish("fork", path, nxt.type, eps, closed.normal, (failure,) + closed.exceptional,
                          (ds, body.derivation, nxt.derivation), (frame,), detail)
