"""Capture-avoiding static substitution and ordered-binder alpha comparison."""
from p8static.syntax import SORTS
from .model import (Free, Bound, LiteralSize, RNode, Binder, Decision,
                    require, FoundationError, children)


def well_scoped(value, frames=()):
    if isinstance(value, (Free, Bound)):
        require(value.sort in SORTS, "unknown reference sort")
        if isinstance(value, Bound):
            require(type(value.depth) is int and type(value.slot) is int, "bound coordinates must be integers")
            require(0 <= value.depth < len(frames), "dangling bound depth")
            require(0 <= value.slot < len(frames[value.depth]), "dangling bound slot")
            require(frames[value.depth][value.slot] == value.sort, "bound reference sort mismatch")
        return
    if isinstance(value, Binder):
        require(all(sort in SORTS for sort in value.sorts), "unknown binder sort")
        frames = (value.sorts,) + frames
    for child in children(value):
        well_scoped(child, frames)


def free_names(value):
    """A Free identity remains free even if a binder once had its spelling."""
    if isinstance(value, Free):
        return frozenset({value})
    return frozenset().union(*(free_names(child) for child in children(value)))


def sorted_free_names(value):
    references = free_names(value)
    return tuple((sort, tuple(sorted((r for r in references if r.sort == sort), key=lambda r: r.name)))
                 for sort in sorted({r.sort for r in references}))


def rebuild(value, transform):
    if isinstance(value, RNode):
        return RNode(value.family, value.tag, tuple((key, transform(child)) for key, child in value.fields))
    if isinstance(value, tuple):
        return tuple(transform(child) for child in value)
    return value


def shift(value, amount, cutoff=0):
    if isinstance(value, Bound):
        depth = value.depth + amount if value.depth >= cutoff else value.depth
        require(depth >= 0, "negative bound depth after shifting")
        return Bound(value.sort, depth, value.slot)
    if isinstance(value, Binder):
        return Binder(value.kind, value.sorts,
            tuple(shift(f, amount, cutoff + 1) for f in value.facts),
            shift(value.body, amount, cutoff + 1))
    return rebuild(value, lambda child: shift(child, amount, cutoff))


def replacement_sort(value):
    if isinstance(value, LiteralSize):
        return "Size"
    require(isinstance(value, (Free, Bound)), "witness must be a reference or Size literal")
    return value.sort


def checked_free_world(world):
    require(world is not None, "explicit declared free world required")
    world = tuple(world)
    require(all(isinstance(ref, Free) and type(ref.name) is str and ref.name
                and ref.sort in SORTS for ref in world), "declared free references required")
    require(len({ref.name for ref in world}) == len(world), "duplicate declared free name")
    return frozenset(world)


def check_free_references(value, world):
    require(free_names(value) <= world, "undeclared free reference")


def substitute_free(value, substitutions, ambient=(), *, free_world=None):
    """Simultaneous substitution, lifting ambient bound ranges under binders.

    Ranges are never recursively substituted by the same mapping. Repeated
    targets are allowed. Ambient lists nearest lexical frames first. Free
    identities in the input, domain and range must belong to free_world.
    """
    items = tuple(substitutions.items()) if hasattr(substitutions, "items") else tuple(substitutions)
    require(len({source for source, _ in items}) == len(items), "duplicate substitution domain")
    substitutions = dict(items)
    free_world = checked_free_world(free_world)
    well_scoped(value, ambient)
    check_free_references(value, free_world)
    for source, target in substitutions.items():
        require(isinstance(source, Free), "only free nominal identities are substitution keys")
        check_free_references((source, target), free_world)
        require(source.sort == replacement_sort(target), "substitution sort mismatch")
        well_scoped(target, ambient)

    def walk(current, depth):
        if isinstance(current, Free) and current in substitutions:
            return shift(substitutions[current], depth)
        if isinstance(current, Binder):
            return Binder(current.kind, current.sorts,
                tuple(walk(f, depth + 1) for f in current.facts), walk(current.body, depth + 1))
        return rebuild(current, lambda child: walk(child, depth))

    result = walk(value, 0)
    well_scoped(result, ambient)
    return result


def instantiate(binder, witnesses, ambient=(), *, free_world=None):
    """Open one binder with existing witnesses; does not allocate resources."""
    require(isinstance(binder, Binder), "binder required")
    witnesses = tuple(witnesses)
    require(len(witnesses) == len(binder.sorts), "witness arity mismatch")
    free_world = checked_free_world(free_world)
    well_scoped(binder, ambient)
    check_free_references(binder, free_world)
    for sort, witness in zip(binder.sorts, witnesses):
        require(sort == replacement_sort(witness), "witness sort mismatch")
        well_scoped(witness, ambient)
        check_free_references(witness, free_world)

    def open_at(current, inner_depth):
        if isinstance(current, Bound):
            if current.depth == inner_depth:
                return shift(witnesses[current.slot], inner_depth)
            if current.depth > inner_depth:
                return Bound(current.sort, current.depth - 1, current.slot)
            return current
        if isinstance(current, Binder):
            return Binder(current.kind, current.sorts,
                tuple(open_at(f, inner_depth + 1) for f in current.facts), open_at(current.body, inner_depth + 1))
        return rebuild(current, lambda child: open_at(child, inner_depth))

    facts = tuple(open_at(f, 0) for f in binder.facts)
    body = open_at(binder.body, 0)
    well_scoped(facts, ambient)
    well_scoped(body, ambient)
    return facts, body


def is_effect(value):
    return isinstance(value, Binder) and value.kind in ("nu","hide") or isinstance(value, RNode) and value.family == "effect"


def effect_parts(value):
    """Flatten atom union, retain every generative binder occurrence."""
    if isinstance(value, Binder):
        require(value.kind in ("nu","hide"), "invalid effect binder")
        return (), (value,)
    require(isinstance(value, RNode) and value.family == "effect", "effect expression required")
    if value.tag == "set":
        atoms = value.get("atoms")
        require(all(isinstance(a, RNode) and a.family == "atom" for a in atoms), "effect set must contain atoms")
        return atoms, ()
    if value.tag == "union":
        left_atoms, left_blocks = effect_parts(value.get("left"))
        right_atoms, right_blocks = effect_parts(value.get("right"))
        return left_atoms + right_atoms, left_blocks + right_blocks
    raise FoundationError("unknown effect constructor")


def canonical(value):
    """Ordered telescope; Phi is a structural set, not a theorem prover."""
    if isinstance(value, Free):
        return ("free", value.sort, value.name)
    if isinstance(value, Bound):
        return ("bound", value.sort, value.depth, value.slot)
    if isinstance(value, LiteralSize):
        return ("size", value.value)
    if is_effect(value):
        atoms, blocks = effect_parts(value)
        atom_keys = tuple(sorted({canonical(a) for a in atoms}, key=repr))
        # Multiplicity of nu blocks is deliberately preserved. This is not
        # unrestricted idempotence/extrusion of generative effect scopes.
        block_keys = tuple(sorted(((block.kind,block.sorts,
                      tuple(sorted({canonical(f) for f in block.facts},key=repr)),canonical(block.body))
                      for block in blocks),key=repr))
        return ("effect-normal", atom_keys, block_keys)
    if isinstance(value, Binder):
        return ("binder", value.kind, value.sorts,
                tuple(sorted({canonical(f) for f in value.facts}, key=repr)), canonical(value.body))
    if isinstance(value, RNode):
        fields = []
        for name, field in value.fields:
            key = tuple(sorted({canonical(f) for f in field}, key=repr)) if name == "foot" and value.family == "type" and value.tag == "Fn" else canonical(field)
            fields.append((name, key))
        return ("node", value.family, value.tag, tuple(sorted(fields)))
    if isinstance(value, tuple):
        return ("tuple", tuple(canonical(v) for v in value))
    if value is None or type(value) in (str, int, bool):
        return (type(value).__name__, repr(value))
    raise FoundationError("unclassified canonical object")


def alpha_equal(left, right, left_ambient=(), right_ambient=()):
    well_scoped(left, left_ambient)
    well_scoped(right, right_ambient)
    if left_ambient or right_ambient:
        # Sort lists alone do not prove identity of two external binder frames.
        # Compare their enclosing Binder trees instead; free Psi names are
        # already rigid Free references and are fully supported.
        return Decision("unsupported", "external-bound-frame-identity-not-specified")
    a, b = canonical(left), canonical(right)
    return Decision("yes", "ordered-alpha-structural-phi") if a == b else Decision("no", "different-alpha-normal-form", (a, b))


def effect_includes(inferred, declared, ambient=()):
    """Conservative syntax inclusion; no execution-trace or cost theorem."""
    well_scoped(inferred, ambient)
    well_scoped(declared, ambient)
    if ambient:
        return Decision("unsupported", "external-bound-frame-identity-not-specified")

    def include(left, right):
        la, lb = effect_parts(left)
        ra, rb = effect_parts(right)
        needed = {canonical(a) for a in la}
        offered = {canonical(a) for a in ra}
        missing = tuple(sorted(needed - offered, key=repr))
        if missing:
            return Decision("no", "missing-effect-atoms", missing)
        candidates = {}
        decisions = {}
        for i, actual in enumerate(lb):
            candidates[i] = []
            for j, upper in enumerate(rb):
                same=(actual.kind==upper.kind and actual.sorts==upper.sorts and
                      {canonical(f) for f in actual.facts}=={canonical(f) for f in upper.facts})
                decision = (include(actual.body, upper.body) if same
                            else Decision("no", "nu-sort-order-mismatch", (actual.sorts, upper.sorts)))
                decisions[i, j] = decision
                if decision.yes:
                    candidates[i].append(j)
        # Finite augmenting-path matching: no greedy matching or many-to-one
        # collapse of separate inferred nu occurrences.
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

        for i in range(len(lb)):
            if not augment(i, set()):
                return Decision("no", "no-injective-nu-block-matching",
                    (i, tuple(candidates[i]), len(lb), len(rb)), tuple(decisions.values()))
        matching = tuple(sorted((i, j) for j, i in assigned.items()))
        return Decision("yes", "syntax-effect-inclusion", matching,
                        tuple(decisions[i, j] for i, j in matching))

    return include(inferred, declared)
