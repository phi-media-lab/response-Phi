"""Finite support, relation projections and caller proof recipes.

Formation checks persistent worlds; generated origins and caller proofs remain
static provenance. Neither a projection nor a held-value recipe is runtime
admission, completion, or resource-reuse authority.
"""
from dataclasses import dataclass
from p8static.syntax import Name
from p8foundation.model import Free, Bound, RNode, Binder, Decision, WitnessPlan, SourceAddress, node, require
from p8foundation.algebra import instantiate, free_names, check_free_references
from p8foundation.entail import FactContext
from .schema import canonical, effect_parts, compare_types


class ClosureContext(FactContext):
    """Same finite facts/consistency rules, new recursive type comparator.

    The override is also used by inherited consistency checks and entails;
    calling the old FactContext.matches would silently use flat-Fn comparison.
    """
    def __init__(self, names, facts, *, init_origin=None, scope_origins=(), epoch_origins=(), region_origins=()):
        self.init_origin = init_origin
        self.scope_origins=tuple(scope_origins)
        self.epoch_origins=tuple(epoch_origins)
        self.region_origins=tuple(region_origins)
        super().__init__(names, facts)

    def extend(self, names=(), facts=(), *, scope_origins=(), epoch_origins=(), region_origins=()):
        return ClosureContext(self.names + tuple(names), self.facts + tuple(facts), init_origin=self.init_origin,
                              scope_origins=self.scope_origins+tuple(scope_origins),
                              epoch_origins=self.epoch_origins+tuple(epoch_origins),
                              region_origins=self.region_origins+tuple(region_origins))

    def check_consistency(self):
        super().check_consistency()
        # Identity in the foundation is per tag. These identities cross tags.
        for epoch in {f.get("epoch") for f in self.facts if f.tag in ("epoch","epoch_owner")}:
            self.epoch_association(epoch)
        scopes={f.get("scope") for f in self.facts if f.tag in ("scope","scope_shape")}
        scopes.update(g.get("scope") for g in self.scope_origins)
        for scope in scopes: self.scope_mapping(scope)

    def epoch_association(self,epoch):
        require(isinstance(epoch,(Free,Bound)) and epoch.sort=="Epoch","epoch association sort")
        facts=tuple(f for f in self.facts if f.tag in ("epoch","epoch_owner") and f.get("epoch")==epoch)
        require(facts,"missing epoch owner association")
        owners={f.get("owner") for f in facts}
        require(len(owners)==1,"conflicting epoch owner association across representations")
        ordinals={f.get("ordinal") for f in facts if f.tag=="epoch"}
        require(all(type(k) is int and k>0 for k in ordinals),"epoch ordinal must be positive")
        require(len(ordinals)<=1,"conflicting concrete epoch ordinal")
        owner=next(iter(owners))
        origins=tuple(g for g in self.epoch_origins if g.get("epoch")==epoch)
        require(all(g.get("owner")==owner for g in origins),"epoch origin owner disagreement")
        return node("mapping","EpochOwner",epoch=epoch,owner=owner,facts=facts,origins=origins)

    def scope_mapping(self,scope):
        require(isinstance(scope,(Free,Bound)) and scope.sort=="Scope","scope mapping sort")
        facts=tuple(f for f in self.facts if f.tag in ("scope","scope_shape") and f.get("scope")==scope)
        origins=tuple(g for g in self.scope_origins if g.get("scope")==scope)
        require(facts or origins,"scope lifetime needs original unique mapping")
        mappings={(f.get("region"),f.get("kind")) for f in facts+origins}
        require(len(mappings)==1,"conflicting scope mapping across representations")
        region,kind=next(iter(mappings))
        # The complete legacy relation's None is an exact field, not the new
        # ShapeOnly interface. It cannot erase a generated symbolic parent.
        parents={f.get("parent_invocation") for f in facts if f.tag=="scope"}
        parents.update(g.get("parent_invocation") for g in origins)
        require(len(parents)<=1,"conflicting scope parent invocation claims")
        if origins:
            require(all(g==origins[0] for g in origins),"conflicting generated scope origins")
        tier="GeneratedScopeOrigin" if origins else "FullScopeRelation" if any(f.tag=="scope" for f in facts) else "ShapeOnly"
        return node("mapping",tier,scope=scope,region=region,kind=kind,facts=facts,origins=origins)

    def entails(self,goal):
        # Explicit relation projection, never type equality or an entry proof.
        if isinstance(goal,RNode) and goal.family=="relation" and goal.tag in ("epoch_owner","scope_shape"):
            check_free_references(goal,self.allowed)
            mapping=(self.epoch_association(goal.get("epoch")) if goal.tag=="epoch_owner" else
                     self.scope_mapping(goal.get("scope")))
            keys=("epoch","owner") if goal.tag=="epoch_owner" else ("scope","region","kind")
            yes=all(mapping.get(k)==goal.get(k) for k in keys)
            return Decision("yes" if yes else "no","explicit-association-projection",(mapping,goal))
        result=super().entails(goal)
        if goal.tag=="outlives" and result.yes:
            path_facts=tuple(self.facts[i] for i in result.details)
            origins=tuple(p for p in self.region_origins if p.get("relation") in path_facts)
            return Decision(result.outcome,result.rule,result.details,result.premises+origins)
        return result

    def matches(self, left, right):
        return compare_types(left, right, self)

    def unpack_plan(self, package, fresh_names, site):
        require(isinstance(package, Binder) and package.kind == "exists", "Exists required")
        require(isinstance(site, SourceAddress), "source-addressed unpack plan required")
        fresh_names = tuple(fresh_names)
        require(tuple(n.sort for n in fresh_names) == package.sorts, "unpack skolem sort/arity")
        require(len({n.text for n in fresh_names}) == len(fresh_names), "duplicate unpack skolem")
        require(not {n.text for n in fresh_names} & {n.text for n in self.names}, "unpack skolems must be fresh")
        replacements = tuple(Free(n.sort, n.text) for n in fresh_names)
        facts, payload = instantiate(package, replacements, free_world=self.allowed | frozenset(replacements))
        expanded = self.extend(fresh_names, facts)
        substitutions = tuple((Bound(sort, 0, i), replacement)
                              for i, (sort, replacement) in enumerate(zip(package.sorts, replacements)))
        return expanded, WitnessPlan(site, "unpack-existing-package-witness-slots", substitutions, (), payload)


@dataclass(frozen=True)
class Caller:
    job: Free
    task: Free
    invocation: Free
    region: Free
    scope_stack: tuple = ()

    def validate(self, context):
        refs = (self.job, self.task, self.invocation, self.region) + self.scope_stack
        sorts = ("Job", "Task", "Invocation", "Region") + ("Scope",) * len(self.scope_stack)
        require(all(isinstance(r, Free) and r.sort == s for r, s in zip(refs, sorts)), "actual caller fields/sorts")
        check_free_references(refs, context.allowed)
        jobs = [f for f in context.facts if f.tag == "job" and f.get("job") == self.job]
        require(jobs and {j.get("task") for j in jobs} == {self.task}, "caller job/Task relation")
        regions = [f.get("region") for f in context.facts
                   if f.tag in ("jobRegion", "bodyRegion") and f.get("job") == self.job]
        require(any(context.entails(node("relation", "outlives", inner=self.region, outer=r)).yes for r in regions),
                "caller region lacks job region coverage")


def combine(rule, decisions):
    decisions = tuple(decisions)
    outcome = "no" if any(d.outcome == "no" for d in decisions) else (
        "unsupported" if any(d.outcome == "unsupported" for d in decisions) else "yes")
    return Decision(outcome, rule, (), decisions)


def open_interface(binder, context, label):
    used = {n.text for n in context.names}
    fresh = []
    for i, sort in enumerate(binder.sorts):
        name = label + "_" + str(i)
        while name in used:
            name += "_"
        used.add(name); fresh.append(Name(name, sort))
    witnesses = tuple(Free(n.sort, n.text) for n in fresh)
    facts, body = instantiate(binder, witnesses, free_world=context.allowed | frozenset(witnesses))
    return context.extend(tuple(fresh), facts), body, witnesses


def external_references(value, depth=0):
    """Include references to enclosing binders, excluding locally bound names."""
    if isinstance(value, Free):
        return frozenset((value,))
    if isinstance(value, Bound):
        return frozenset((Bound(value.sort, value.depth - depth, value.slot),)) if value.depth >= depth else frozenset()
    if isinstance(value, Binder):
        return external_references(value.facts + (value.body,), depth + 1)
    if isinstance(value, RNode):
        return external_references(tuple(v for _, v in value.fields), depth)
    if isinstance(value, tuple):
        return frozenset().union(*(external_references(v, depth) for v in value))
    return frozenset()


def all_support(items):
    return node("support", "all", items=tuple(items))


def foot(kind, *names):
    return node("support", "atom", foot=node("foot", kind, names=tuple(names)))


def signature_support(fn):
    refs = external_references(tuple(fn.get(k) for k in ("chi", "parameter", "result", "kappa", "effect")))
    return all_support(foot("region" if r.sort == "Region" else "scope-lifetime", r)
                       for r in sorted(refs, key=repr) if r.sort in ("Region", "Scope"))


def support_of(t):
    if isinstance(t, Binder):
        require(t.kind == "exists", "type binder expected")
        return Binder("support-hidden", t.sorts, t.facts, support_of(t.body))
    require(isinstance(t, RNode) and t.family in ("type", "ctype"), "resolved type required")
    if t.tag in ("Pair", "Sum"):
        return all_support((support_of(t.get("left")), support_of(t.get("right"))))
    if t.tag == "Fn":
        require(t.family == "ctype", "legacy Fn needs explicit schema migration")
        return all_support((t.get("chi"), signature_support(t)))
    if t.tag == "Shared":
        require(stable(t.get("payload")), "Shared payload must be Stable")
        return support_of(t.get("payload"))
    refs = {"Scope": ("scope", "region"), "Lease": ("scope", "owner", "epoch", "lease", "region"),
            "Read": ("access", "owner", "epoch", "region"), "Write": ("owner", "epoch")}
    if t.tag in refs:
        return foot(t.tag.lower(), *(t.get(k) for k in refs[t.tag]))
    require(t.tag in ("Unit", "Bool", "Nat", "Word", "Vector", "Failure", "ErrorPayload", "FaultSet", "Owner", "Control"),
            "unclassified type support")
    return node("support", "zero")


def has_live(s):
    if isinstance(s, Binder):
        return has_live(s.body)
    if s.tag == "zero":
        return False
    if s.tag == "atom":
        return True
    return any(has_live(x) for x in s.get("items"))


def has_write(t):
    if isinstance(t, Binder):
        return has_write(t.body)
    if isinstance(t, RNode):
        if t.family == "type" and t.tag == "Write":
            return True
        if t.family == "foot" and t.tag == "write":
            return True
        return any(has_write(v) for _, v in t.fields)
    return isinstance(t, tuple) and any(has_write(v) for v in t)


def copyable(t):
    if isinstance(t, Binder):
        return copyable(t.body)
    if t.tag == "Write":
        return False
    if t.tag == "Fn":
        return t.get("mode") == "U"
    if t.tag in ("Pair", "Sum"):
        return copyable(t.get("left")) and copyable(t.get("right"))
    return True


def stable(t):
    return copyable(t) and not has_live(support_of(t)) and not has_write(t)


def prove_kappa(k, context, caller):
    if isinstance(k, Binder):
        require(k.kind == "requires-hidden", "requires binder expected")
        inner, body, skolems = open_interface(k, context, "kappa_open")
        premise = prove_kappa(body, inner, caller)
        return Decision(premise.outcome, "universal-interface-caller-proof", (skolems,), (premise,))
    require(k.family == "kappa", "kappa required")
    if k.tag == "true":
        return Decision("yes", "true-caller-condition")
    if k.tag == "all":
        return combine("all-caller-conditions", (prove_kappa(x, context, caller) for x in k.get("items")))
    if k.tag == "same_job":
        return Decision("yes" if caller.job == k.get("job") else "no", "rigid-actual-caller-job",
                        (caller.job, k.get("job")))
    require(k.tag == "covered", "unknown caller condition")
    return context.entails(node("relation", "outlives", inner=caller.region, outer=k.get("region")))


def prove_support(s, context, caller):
    if isinstance(s, Binder):
        require(s.kind == "support-hidden", "support binder expected")
        inner, body, skolems = open_interface(s, context, "support_open")
        premise = prove_support(body, inner, caller)
        return Decision(premise.outcome, "hidden-support-interface-proof", (skolems,), (premise,))
    if s.tag == "zero":
        return Decision("yes", "no-live-support")
    if s.tag == "all":
        return combine("all-support", (prove_support(x, context, caller) for x in s.get("items")))
    require(s.tag == "atom", "unknown support")
    f = s.get("foot"); names = f.get("names")
    if f.tag == "write":
        # Kernel-only authority and affine formation are not inferred here from
        # owner/epoch tags. Keep this distinct from semantic rejection.
        return Decision("unsupported", "write-kernel-context-integration-pending")
    if f.tag == "scope-lifetime":
        region=context.scope_mapping(names[0]).get("region")
    else:
        region = names[-1]
    coverage = context.entails(node("relation", "outlives", inner=caller.region, outer=region))
    if f.tag != "read":
        return coverage
    access, owner, epoch, region = names
    candidates = [x for x in context.facts if x.tag == "access" and x.get("access") == access
                  and x.get("owner") == owner and x.get("epoch") == epoch and x.get("region") == region]
    require(candidates and len({x.get("job") for x in candidates}) == 1,
            "Read needs matching persistent access relation")
    actual_job = candidates[0].get("job")
    same_job = Decision("yes" if caller.job == actual_job else "no", "read-authorized-job", (actual_job, caller.job))
    return combine("read-hold", (same_job, coverage))


def hold(t, context, caller):
    """Fn Hold deliberately excludes the returned function's own kappa."""
    return prove_support(support_of(t), context, caller)


def effect_includes(inferred, declared):
    """Finite injective occurrence matching, distinct hide and nu constructors."""
    ia, ib = effect_parts(inferred); da, db = effect_parts(declared)
    missing = {canonical(a) for a in ia} - {canonical(a) for a in da}
    if missing:
        return Decision("no", "missing-effect-atoms", tuple(sorted(missing, key=repr)))
    candidates, proofs = {}, {}
    for i, actual in enumerate(ib):
        candidates[i] = []
        for j, upper in enumerate(db):
            same_interface = actual.kind == upper.kind and actual.sorts == upper.sorts and (
                {canonical(f) for f in actual.facts} == {canonical(f) for f in upper.facts})
            proof = effect_includes(actual.body, upper.body) if same_interface else Decision("no", "effect-binder-mismatch")
            proofs[i, j] = proof
            if proof.yes:
                candidates[i].append(j)
    assigned = {}
    def augment(i, seen):
        for j in candidates[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in assigned or augment(assigned[j], seen):
                assigned[j] = i
                return True
        return False
    for i in range(len(ib)):
        if not augment(i, set()):
            return Decision("no", "no-injective-hide-nu-match", (i,))
    matching = tuple(sorted((i, j) for j, i in assigned.items()))
    return Decision("yes", "injective-hide-nu-inclusion", matching, tuple(proofs[p] for p in matching))
