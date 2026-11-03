"""Finite persistent-fact reasoning and unexecuted witness plans.

This is a symbolic relation layer, not whole-world/configuration typing. Its
caller still owes the full type-formation judgment for supplied Psi. No query
here proves a gate open, a runtime witness present, or an access completed.
"""
from p8static import syntax as ast
from .model import (Free, Bound, LiteralSize, RNode, Binder, Decision, WitnessPlan, SourceAddress,
                    require, children)
from .resolve import Resolver, REL_REFS
from .algebra import (canonical, alpha_equal, free_names, well_scoped, instantiate,
                      substitute_free, check_free_references)


IDENTITY = {"owner": ("owner",), "epoch": ("epoch",), "epoch_owner":("epoch",),
    "scope": ("scope",), "scope_shape":("scope",),
    "job": ("job",), "result": ("job",), "jobRegion": ("job",),
    "bodyRegion": ("job", "role"), "producerOf": ("job",), "access": ("access",)}


def size_term(value):
    return isinstance(value, LiteralSize) or isinstance(value, (Free, Bound)) and value.sort == "Size"


def reachable_path(graph, start, finish):
    pending = [(start, ())]
    seen = set()
    while pending:
        current, path = pending.pop(0)
        if current == finish:
            return path
        if current in seen:
            continue
        seen.add(current)
        for other, evidence in graph.get(current, ()):
            if other not in seen:
                pending.append((other, path + (evidence,)))
    return None


class FactContext:
    def __init__(self, names, facts):
        self.names = tuple(names)
        require(len({n.text for n in self.names}) == len(self.names), "duplicate context name")
        self.resolver = Resolver(self.names)
        self.allowed = frozenset(Free(n.sort, n.text) for n in self.names)
        self.facts = tuple(facts)
        self.size_graph = {}
        self.region_graph = {}
        self.positive_facts = []
        self.size_nodes = {ref for ref in self.allowed if ref.sort == "Size"}
        for index, fact in enumerate(self.facts):
            require(isinstance(fact, RNode) and fact.family == "relation" and fact.tag in REL_REFS,
                    "only persistent relation facts are allowed")
            well_scoped(fact)
            require(free_names(fact) <= self.allowed, "fact refers to undeclared free name")
            if fact.tag == "sizeEqual":
                a, b = fact.get("left"), fact.get("right")
                require(size_term(a) and size_term(b), "size equality needs Size indices")
                self.size_nodes.update((a, b))
                self.size_graph.setdefault(a, []).append((b, index))
                self.size_graph.setdefault(b, []).append((a, index))
            elif fact.tag == "positiveSize":
                n = fact.get("size")
                require(size_term(n), "positiveSize needs Size index")
                self.size_nodes.add(n)
                self.positive_facts.append((n, index))
            elif fact.tag == "outlives":
                a, b = fact.get("inner"), fact.get("outer")
                self.region_graph.setdefault(a, []).append((b, index))
        self.check_consistency()

    @classmethod
    def from_telescope(cls, telescope):
        resolver = Resolver(telescope.names)
        return cls(telescope.names, tuple(resolver.resolve(f) for f in telescope.facts))

    def check_consistency(self):
        for n in self.size_nodes:
            literals = {other.value for other in self.size_nodes if isinstance(other, LiteralSize)
                        and reachable_path(self.size_graph, n, other) is not None}
            require(len(literals) <= 1, "contradictory literal aliases")
            if 0 in literals:
                require(not any(reachable_path(self.size_graph, n, p) is not None for p, _ in self.positive_facts),
                        "positive Size aliases zero")
        definitions = {}
        for index, fact in enumerate(self.facts):
            if fact.tag not in IDENTITY:
                continue
            identity = fact.tag, tuple(fact.get(k) for k in IDENTITY[fact.tag])
            if identity in definitions:
                previous = definitions[identity]
                require(self.matches(self.facts[previous], fact).yes, "conflicting nominal identity facts")
            else:
                definitions[identity] = index

    def size_equal(self, left, right):
        require(size_term(left) and size_term(right), "size comparison needs Size indices")
        well_scoped(left);well_scoped(right)
        require(free_names((left, right)) <= self.allowed, "size index refers to undeclared name")
        path = reachable_path(self.size_graph, left, right)
        return Decision("yes", "size-alias-path", path) if path is not None else Decision("no", "no-size-alias-path", (left, right))

    def positive(self, n):
        require(size_term(n), "positivity needs Size index")
        well_scoped(n)
        require(free_names(n) <= self.allowed, "size index refers to undeclared name")
        if isinstance(n, LiteralSize) and n.value > 0:
            return Decision("yes", "positive-literal", (n,))
        for other in sorted(self.size_nodes, key=repr):
            if isinstance(other, LiteralSize) and other.value > 0:
                equality = self.size_equal(n, other)
                if equality.yes:
                    return Decision("yes", "positive-literal-alias", (other,), (equality,))
        for other, index in self.positive_facts:
            equality = self.size_equal(n, other)
            if equality.yes:
                return Decision("yes", "explicit-positive-fact", (index,), (equality,))
        return Decision("no", "no-positive-evidence", (n,))

    def matches(self, left, right):
        """Recursive structural alpha modulo current-world free Size aliases.

        Bound Size slots stay rigid. Phi retains its structural fact set;
        normalization neither derives nor removes interface facts.
        """
        well_scoped((left, right))
        check_free_references((left, right), self.allowed)
        if canonical(left) == canonical(right):
            return Decision("yes", "structural-alpha-match")
        substitutions, evidence = {}, []
        for ref in sorted(free_names((left, right)), key=lambda r: (r.sort, r.name)):
            if ref.sort != "Size":
                continue
            reachable = [(candidate, self.size_equal(ref, candidate))
                         for candidate in sorted(self.size_nodes, key=repr)]
            equivalent = [(candidate, reason) for candidate, reason in reachable if reason.yes]
            # Prefer a literal. Otherwise choose the same rigid free name in
            # each alias class, independent of which object is on the left.
            candidate, reason = min(equivalent, key=lambda item:
                (0, item[0].value) if isinstance(item[0], LiteralSize) else (1, item[0].name))
            if candidate != ref:
                substitutions[ref] = candidate
                evidence.append(Decision("yes", "normalize-free-size", (ref, candidate), (reason,)))
        normalized_left = substitute_free(left, substitutions, free_world=self.allowed)
        normalized_right = substitute_free(right, substitutions, free_world=self.allowed)
        equal = canonical(normalized_left) == canonical(normalized_right)
        return Decision("yes" if equal else "no", "recursive-alpha-free-size-aliases",
                        () if equal else (normalized_left, normalized_right), tuple(evidence))

    def entails(self, goal):
        require(isinstance(goal, RNode) and goal.family == "relation" and goal.tag in REL_REFS, "persistent goal required")
        well_scoped(goal)
        require(free_names(goal) <= self.allowed, "goal refers to undeclared free name")
        if goal.tag == "sizeEqual":
            return self.size_equal(goal.get("left"), goal.get("right"))
        if goal.tag == "positiveSize":
            return self.positive(goal.get("size"))
        if goal.tag == "outlives":
            path = reachable_path(self.region_graph, goal.get("inner"), goal.get("outer"))
            return (Decision("yes", "outlives-reflexive-transitive-path", path) if path is not None
                    else Decision("no", "no-outlives-path", (goal,)))
        candidates = []
        for index, fact in enumerate(self.facts):
            if fact.tag != goal.tag:
                continue
            comparison = self.matches(fact, goal)
            if comparison.yes:
                return Decision("yes", "existing-persistent-fact", (index,), (comparison,))
            candidates.append(comparison)
        return Decision("no", "no-matching-persistent-fact", (goal,), tuple(candidates))

    def all(self, goals):
        decisions = tuple(self.entails(goal) for goal in goals)
        return Decision("yes" if all(d.yes for d in decisions) else "no", "all-interface-relations", (), decisions)


def plan_pack(context, interface, witnesses, payload_type, actual_value_type, node_address):
    """Static witnesses and finite reasons; no value allocation/evaluation."""
    require(isinstance(node_address, SourceAddress), "digest-bound source node address required")
    require(len(witnesses) == len(interface.names), "pack witness arity mismatch")
    package = context.resolver.binder("exists", interface.names, interface.facts, payload_type)
    replacements = tuple(context.resolver.witness(w, n.sort) for w, n in zip(witnesses, interface.names))
    goals, expected_type = instantiate(package, replacements, free_world=context.allowed)
    relations = context.all(goals)
    actual_type = context.resolver.resolve(actual_value_type)
    payload = context.matches(actual_type, expected_type)
    decision = Decision("yes" if relations.yes and payload.yes else "no", "pack-interface-and-payload", (), (relations, payload))
    if not decision.yes:
        return decision, None
    substitution = tuple((Bound(sort, 0, i), replacement) for i, (sort, replacement) in enumerate(zip(package.sorts, replacements)))
    return decision, WitnessPlan(node_address, "pack-existing-witnesses", substitution, relations.premises, expected_type)


def plan_unpack(context, package, fresh_names, node_address):
    """Fresh STATIC skolems denote existing package witnesses, not fresh resources."""
    require(isinstance(package, Binder) and package.kind == "exists", "existential package type required")
    require(isinstance(node_address, SourceAddress), "digest-bound source node address required")
    well_scoped(package)
    check_free_references(package, context.allowed)
    fresh_names = tuple(fresh_names)
    require(tuple(n.sort for n in fresh_names) == package.sorts, "unpack skolem sort/arity mismatch")
    require(len({n.text for n in fresh_names}) == len(fresh_names), "duplicate unpack skolem")
    require(not {n.text for n in fresh_names} & {n.text for n in context.names}, "unpack skolems must be fresh")
    replacements = tuple(Free(n.sort, n.text) for n in fresh_names)
    facts, payload = instantiate(package, replacements, free_world=context.allowed | frozenset(replacements))
    expanded = FactContext(context.names + fresh_names, context.facts + facts)
    substitution = tuple((Bound(sort, 0, i), replacement) for i, (sort, replacement) in enumerate(zip(package.sorts, replacements)))
    plan = WitnessPlan(node_address, "unpack-existing-package-witness-slots", substitution, (), payload)
    return expanded, plan
