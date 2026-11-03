"""Finite independent symbolic caller reasoning and complete lifetime scan."""
from dataclasses import dataclass
from p8foundation.model import Free, Bound, Binder, RNode, Decision, node, require
from p8foundation.algebra import instantiate, rebuild, shift
from .schema import canonical, interface_key, validate_links
from .support import open_interface, signature_support, all_support, foot, external_references, support_of, Caller
from .qualified import qualified_support, chi_sources
from .caller import SymbolicCaller, InScope, key as caller_key, field as caller_field


class Unsupported(ValueError):
    pass


@dataclass(frozen=True)
class ProofContext:
    world: object
    caller: object
    assumptions: object
    evidence: tuple = ()

    def in_world(self, world):
        return ProofContext(world, self.caller, self.assumptions, self.evidence)

    def at(self,requirement):
        return node("proof","KAt",subject=caller_key(self.caller),requirement=requirement,
                    evidence=node("proof","caller-assumption-context",assumptions=self.assumptions,
                                  premises=self.evidence))


def close_refs(value, refs, depth=0):
    if isinstance(value, Free) and value in refs:
        return Bound(value.sort, depth, refs.index(value))
    if isinstance(value, Binder):
        return Binder(value.kind, value.sorts, tuple(close_refs(f, refs, depth + 1) for f in value.facts),
                      close_refs(value.body, refs, depth + 1))
    if isinstance(value, Bound):
        return shift(value, 1, depth)
    return rebuild(value, lambda x: close_refs(x, refs, depth))


def ktrue():
    return node("kappa", "true")


def kall(items):
    return node("kappa", "all", items=tuple(items))


def use(s, world, stack=()):
    if isinstance(s, Binder):
        from .formation import form_interface
        inner, body, refs = form_interface(s,world,"use",stack)
        result = close_refs(use(body, inner,stack), refs)
        return Binder("requires-hidden", s.sorts, s.facts, result)
    if s.tag == "zero":
        return ktrue()
    if s.tag == "all":
        return kall(use(x, world,stack) for x in s.get("items"))
    f = s.get("foot"); names = f.get("names")
    if f.tag == "write":
        raise Unsupported("Write requires kernel context")
    region = names[-1]
    if f.tag == "scope-lifetime":
        region=world.scope_mapping(names[0]).get("region")
    coverage = node("kappa", "covered", region=region)
    if f.tag != "read":
        return coverage
    a, o, e, r = names
    jobs = {x.get("job") for x in world.facts if x.tag == "access" and
            (x.get("access"), x.get("owner"), x.get("epoch"), x.get("region")) == (a, o, e, r)}
    require(len(jobs) == 1, "Read requires unique exact access relation")
    return kall((node("kappa", "same_job", job=next(iter(jobs))), coverage))


def lifetimes(fields):
    refs = external_references(tuple(fields))
    return all_support(foot("region" if r.sort == "Region" else "scope-lifetime", r)
                       for r in sorted(refs, key=repr) if r.sort in ("Region", "Scope"))


def derive(fn, world, initial=False, stack=()):
    chi, a = fn.get("chi"), fn.get("parameter")
    l = lifetimes(fn.get(k) for k in ("chi", "parameter", "result", "effect")) if initial else signature_support(fn)
    qa = qualified_support(a)
    groups = ((chi, chi_sources(fn)), (qa.tree, qa.sources), (l, ()))
    requirements, links = [], []
    for i, (tree, sources) in enumerate(groups):
        requirements.append(use(tree, world,stack))
        for s in sources:
            support_path = s.support_path[1:] if i == 0 else s.support_path
            target = ("kappa", "items", i) + support_path
            # chi source paths are already rooted; parameter paths are typed.
            obj = tree
            for step in support_path:
                obj = obj.body if isinstance(obj, Binder) else obj[step] if isinstance(obj, tuple) else obj.get(step)
            require(isinstance(obj, Binder), "derived support source is not hidden")
            links.append(node("link", "value" if s.qualification == "Value" else "interface_only",
                              target=target, source=s.source_path, alignment=tuple(range(len(obj.sorts))),
                              guards=s.guards if s.qualification == "Value" else ()))
    return kall(requirements), tuple(links), l


def flatten(k):
    if isinstance(k, RNode) and k.tag == "all":
        return sum((flatten(x) for x in k.get("items")), ())
    return (k,)


def consistent(k):
    def external_jobs(value):
        if isinstance(value,Binder): return external_jobs(value.body)
        if value.tag=="all": return frozenset().union(*(external_jobs(x) for x in value.get("items")))
        return frozenset((value.get("job"),)) if value.tag=="same_job" and isinstance(value.get("job"),Free) else frozenset()
    jobs = external_jobs(k)
    require(len(jobs) <= 1, "contradictory rigid same_job assumptions")


def prove(k, pc):
    """Universals remain universals; concrete value ResHold never enters here."""
    assumptions = flatten(pc.assumptions)
    opened=None
    if isinstance(k,Binder):
        from .formation import form_interface
        opened=form_interface(k,pc.world,"prove")
    if any(canonical(k) == canonical(a) for a in assumptions):
        return Decision("yes", "declared-caller-assumption", (pc.at(k),))
    if isinstance(k, Binder):
        inner, body, refs = opened
        aligned = []
        for a in assumptions:
            if isinstance(a, Binder) and interface_key(a) == interface_key(k):
                _, opened = instantiate(a, refs, free_world=inner.allowed)
                aligned.append(opened)
            else:
                aligned.append(a)
        proof = prove(body, ProofContext(inner, pc.caller, kall(aligned), pc.evidence))
        return Decision(proof.outcome, "strict-universal-interface", (k.sorts, refs), (proof,))
    if k.tag == "true":
        return Decision("yes", "true")
    if k.tag == "all":
        children = tuple(prove(x, pc) for x in k.get("items"))
        return Decision("yes" if all(x.yes for x in children) else "no", "all-caller", (), children)
    if isinstance(pc.caller, Caller):
        if k.tag == "same_job":
            return Decision("yes" if pc.caller.job == k.get("job") else "no", "caller-job-match" if pc.caller.job == k.get("job") else "caller-job-mismatch", (k,))
        return pc.world.entails(node("relation", "outlives", inner=pc.caller.region, outer=k.get("region")))
    require(isinstance(pc.caller, (SymbolicCaller,InScope)), "independent symbolic or scoped caller required")
    if k.tag == "same_job":
        actual=caller_field(pc.caller,"job")
        if isinstance(actual,Free):
            return Decision("yes" if actual==k.get("job") else "no","scoped-caller-job-match" if actual==k.get("job") else "scoped-caller-job-mismatch",
                            (caller_key(pc.caller),k,actual))
        return Decision("no", "symbolic-same_job-missing", (caller_key(pc.caller), k))
    if isinstance(pc.caller,InScope):
        local=pc.world.entails(node("relation","outlives",inner=caller_field(pc.caller,"region"),outer=k.get("region")))
        if local.yes: return Decision("yes","scoped-region-coverage",(caller_key(pc.caller),k,pc.caller.origin),(local,))
    for a in assumptions:
        if isinstance(a, RNode) and a.tag == "covered":
            p = pc.world.entails(node("relation", "outlives", inner=a.get("region"), outer=k.get("region")))
            if p.yes:
                return Decision("yes", "symbolic-coverage-transitive", (pc.at(a),k), (p,))
    return Decision("no", "symbolic-coverage-missing", (caller_key(pc.caller), k))


def require_proof(decision, message):
    if decision.outcome == "unsupported":
        raise Unsupported(message + ": " + decision.rule)
    def failures(d):
        nested=tuple(p for p in d.premises if isinstance(p,Decision) and not p.yes)
        return "/".join(failures(p) for p in nested) if nested else d.rule
    require(decision.yes, message + ": " + failures(decision))
    return decision


def minimum(fn, world, stack=()):
    kmin, _, l = derive(fn, world,stack=stack)
    consistent(fn.get("kappa"))
    proof = prove(kmin, ProofContext(world, SymbolicCaller(("formation",)), fn.get("kappa")))
    require_proof(proof, "kappa does not entail complete minimum")
    return kmin, l, proof


def holding(t, pc):
    return require_proof(prove(use(support_of(t), pc.world), pc), "value holding")


