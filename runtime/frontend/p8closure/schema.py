"""Versioned expanded closure types, with explicit nominal field resolution.

This is syntax/formation of the new fields, not a whole-world or body checker.
No source digest, runtime package identity or supplied proof enters type equality.
"""
from dataclasses import dataclass
from p8static import parser as old
from p8foundation.model import Free, Bound, LiteralSize, RNode, Binder, Decision, FoundationError, node, require
from p8foundation.resolve import Resolver, TYPE_REFS, REL_REFS, ATOM_REFS, FOOT_SORTS
from p8foundation.algebra import well_scoped, free_names, check_free_references, substitute_free


VERSION = "p8-generative-type-v0.1"

# This version extends the source representation, not the frozen foundation's
# flat-Foot schema. A signature lifetime is a Scope identity/coverage condition;
# it does not stand for a held Scope value, an open scope, or access authority.
CLOSURE_FOOT_SORTS = {**FOOT_SORTS, "scope-lifetime": ("Scope",)}


@dataclass(frozen=True)
class ResolvedWorld:
    names: tuple
    facts: tuple


def exact(value, keys, where):
    return old.obj(value, set(keys), where)


def array(value, where):
    return old.sequence(value, where)


def path(value):
    parts = tuple(array(value, "interface path"))
    require(parts and parts[0] in ("chi", "parameter", "result", "kappa"), "invalid path role")
    require(all(type(p) is str or type(p) is int and p >= 0 for p in parts), "invalid path component")
    return parts


class Schema:
    def __init__(self, resolver):
        self.resolver = resolver

    def interface(self, raw, body, kind, parse_body):
        exact(raw, ("names", "facts"), "interface")
        names = tuple(old.name(n, "interface.name") for n in array(raw["names"], "names"))
        inner = Schema(self.resolver.enter(names))
        facts = tuple(inner.relation(f) for f in array(raw["facts"], "facts"))
        # Phi is a structural set at comparison time. Repeated source facts do
        # not narrow the interface; retain their syntax and normalize in key.
        return Binder(kind, tuple(n.sort for n in names), facts, parse_body(inner, body))

    def leaf_field(self, raw, conversion, key, refs):
        if key in refs:
            if raw is None and conversion == "optional_id":
                return None
            return self.resolver.ref(old.ident(raw, key), refs[key])
        if conversion == "type":
            return self.type(raw)
        if conversion == "size":
            return self.resolver.resolve(old.size(raw, key))
        if conversion == "kind":
            parsed = old.convert(raw, "kind", key)
            return "plain" if parsed == "plain" else (
                self.resolver.ref(parsed[0], "Owner"), self.resolver.ref(parsed[1], "Epoch"))
        if conversion in ("mode", "nat", "role", "wait_mode", "service_literal", "capability_literal"):
            return old.convert(raw, conversion, key)
        raise FoundationError("unclassified field: " + key + "/" + conversion)

    def structured(self, raw, discriminator, schemas, family, references):
        require(type(raw) is dict and type(raw.get(discriminator)) is str, "tagged object required")
        tag = raw[discriminator]
        require(tag in schemas, "unknown constructor")
        fields = schemas[tag]
        exact(raw, (discriminator, *fields), family + "/" + tag)
        return RNode(family, tag, tuple((k, self.leaf_field(raw[k], c, k, references.get(tag, {})))
                                     for k, c in fields.items()))

    def relation(self, raw):
        fields={**old.REL_FIELDS,"epoch_owner":{"epoch":"id","owner":"id"},
                "scope_shape":{"scope":"id","region":"id","kind":"kind"}}
        fact = self.structured(raw, "relation", fields, "relation", REL_REFS)
        if fact.tag == "job":
            # The public ordinary relation can never construct HostRoot.
            return RNode("relation", "job", tuple(("boundary", node("boundary", "ByScope", scope=v))
                         if k == "scope" else (k, v) for k, v in fact.fields))
        return fact

    def type(self, raw):
        require(type(raw) is dict and type(raw.get("type")) is str, "type object required")
        tag = raw["type"]
        if tag == "Exists":
            exact(raw, ("type", "interface", "body"), "Exists")
            return self.interface(raw["interface"], raw["body"], "exists", lambda s, b: s.type(b))
        if tag == "Fn":
            exact(raw, ("type", "mode", "foot", "projections", "requires", "effect", "parameter", "result", "links"), "Fn")
            from .projections import parse_projections, validate_projections
            fn = node("ctype", "Fn", mode=old.convert(raw["mode"], "mode", "Fn.mode"),
                chi=self.support(raw["foot"]), projections=parse_projections(raw["projections"]), kappa=self.kappa(raw["requires"]),
                effect=self.effect(raw["effect"]), parameter=self.type(raw["parameter"]),
                result=self.type(raw["result"]), links=tuple(self.link(x) for x in array(raw["links"], "links")))
            validate_projections(fn); validate_links(fn)
            return fn
        return self.structured(raw, "type", old.TYPE_FIELDS, "type", TYPE_REFS)

    def support(self, raw):
        require(type(raw) is dict, "support object required")
        tag = raw.get("support")
        if tag == "zero":
            exact(raw, ("support",), "zero")
            return node("support", "zero")
        if tag == "atom":
            exact(raw, ("support", "foot"), "support atom")
            f = exact(raw["foot"], ("kind", "names"), "foot")
            require(type(f["kind"]) is str and f["kind"] in CLOSURE_FOOT_SORTS, "unknown Foot")
            names = array(f["names"], "foot.names")
            sorts = CLOSURE_FOOT_SORTS[f["kind"]]
            require(len(names) == len(sorts), "Foot arity")
            foot = node("foot", f["kind"], names=tuple(self.resolver.ref(old.ident(n, "foot.name"), s)
                                                       for n, s in zip(names, sorts)))
            return node("support", "atom", foot=foot)
        if tag == "all":
            exact(raw, ("support", "items"), "support all")
            return node("support", "all", items=tuple(self.support(x) for x in array(raw["items"], "items")))
        if tag == "hidden":
            exact(raw, ("support", "interface", "body"), "support hidden")
            return self.interface(raw["interface"], raw["body"], "support-hidden", lambda s, b: s.support(b))
        raise FoundationError("unknown support constructor")

    def kappa(self, raw):
        require(type(raw) is dict, "kappa object required")
        tag = raw.get("requires")
        if tag == "true":
            exact(raw, ("requires",), "true")
            return node("kappa", "true")
        if tag in ("covered", "same_job"):
            key, sort = ("region", "Region") if tag == "covered" else ("job", "Job")
            exact(raw, ("requires", key), tag)
            return node("kappa", tag, **{key: self.resolver.ref(old.ident(raw[key], key), sort)})
        if tag == "all":
            exact(raw, ("requires", "items"), "kappa all")
            return node("kappa", "all", items=tuple(self.kappa(x) for x in array(raw["items"], "items")))
        if tag == "hidden":
            exact(raw, ("requires", "interface", "body"), "kappa hidden")
            return self.interface(raw["interface"], raw["body"], "requires-hidden", lambda s, b: s.kappa(b))
        raise FoundationError("unknown caller requirement")

    def effect(self, raw):
        require(type(raw) is dict, "effect object required")
        tag = raw.get("effect")
        if tag == "hide":
            exact(raw, ("effect", "interface", "body"), "hide")
            return self.interface(raw["interface"], raw["body"], "hide", lambda s, b: s.effect(b))
        if tag == "nu":
            exact(raw, ("effect", "interface", "body"), "nu")
            return self.interface(raw["interface"], raw["body"], "nu", lambda s, b: s.effect(b))
        if tag == "set":
            exact(raw, ("effect", "atoms"), "effect set")
            atoms = tuple(self.structured(a, "atom", old.ATOM_FIELDS, "atom", ATOM_REFS)
                          for a in array(raw["atoms"], "atoms"))
            return node("effect", "set", atoms=atoms)
        if tag == "union":
            exact(raw, ("effect", "left", "right"), "effect union")
            return node("effect", "union", left=self.effect(raw["left"]), right=self.effect(raw["right"]))
        raise FoundationError("unknown effect constructor")

    def link(self, raw):
        require(type(raw) is dict, "link object required")
        kind = raw.get("link")
        require(kind in ("value", "interface_only"), "unknown interface link")
        keys = ("link", "target", "source", "alignment") + (("guards",) if kind == "value" else ())
        exact(raw, keys, "link")
        alignment = tuple(old.natural(i, "alignment") for i in array(raw["alignment"], "alignment"))
        guards = ()
        if kind == "value":
            gs = []
            for guard in array(raw["guards"], "guards"):
                require(type(guard) is dict, "ValueLink guard object required")
                if "selector" in guard:
                    exact(guard, ("fn", "selector", "arm"), "selector guard")
                    fp = tuple(array(guard["fn"], "guard.fn"))
                    if fp:
                        path(list(fp))
                    require(guard["arm"] in ("left", "right"), "unknown Sum arm")
                    gs.append((fp, old.natural(guard["selector"], "selector"), guard["arm"]))
                    continue
                exact(guard, ("path", "arm"), "guard")
                require(guard["arm"] in ("left", "right"), "unknown Sum arm")
                gs.append((path(guard["path"]), guard["arm"]))
            guards = tuple(gs)
        return node("link", kind, target=path(raw["target"]), source=path(raw["source"]),
                    alignment=alignment, guards=guards)


def at(fn, steps):
    """Structural interface lookup, including signatures with no current value.

    Only InterfaceOnlyLink sources and requirement targets use this operation.
    ValueLink uses value_source_at, whose every step has a value/support rule.
    """
    current = fn.get(steps[0])
    parents, guards = [], []
    prefix = (steps[0],)
    for step in steps[1:]:
        if isinstance(current, Binder):
            require(step == "body", "binder path may only enter body")
            parents.append((prefix, current))
            current = current.body
        elif isinstance(current, RNode):
            if current.family == "type" and current.tag == "Sum":
                require(step in ("left", "right"), "Sum path requires arm")
                guards.append((prefix, step))
            require(type(step) is str and step in dict(current.fields), "invalid structural field path")
            current = current.get(step)
        elif isinstance(current, tuple):
            require(type(step) is int and 0 <= step < len(current), "invalid structural index")
            current = current[step]
        else:
            raise FoundationError("path traverses a leaf")
        prefix += (step,)
    return current, tuple(parents), tuple(guards)


def value_source_at(fn, steps):
    """Follow a typed projection of the held closure/actual argument.

    A nested Fn exposes only chi via its required configuration/certificate
    support map. Its A/B/kappa/effect/links are interface descriptions, not
    existing values or witness slots. Shared's payload field is likewise a
    result type, not a delivered result value. This formation check does not itself
    establish the configuration premise or manufacture a runtime package.
    """
    require(steps and steps[0] in ("chi", "parameter"), "value link source role invalid")
    current, prefix = fn.get(steps[0]), (steps[0],)
    projection_owner = (fn, (), ("chi",)) if steps[0] == "chi" else None
    parents, guards = [], []
    index = 1
    while index < len(steps):
        step = steps[index]
        if isinstance(current, Binder):
            require(current.kind in ("exists", "support-hidden") and step == "body",
                    "value link may only project an existing interface payload")
            parents.append((prefix, current))
            current = current.body
        elif isinstance(current, RNode) and current.family == "ctype" and current.tag == "Fn":
            require(step == "chi", "value link cannot project a function signature")
            projection_owner = (current, prefix, prefix + ("chi",))
            current = current.get("chi")
        elif isinstance(current, RNode) and current.family == "type" and current.tag in ("Pair", "Sum"):
            require(step in ("left", "right"), "value link requires a typed product/sum arm")
            if current.tag == "Sum":
                guards.append((prefix, step))
            current = current.get(step)
        elif isinstance(current, RNode) and current.family == "type" and current.tag == "Shared":
            require(False, "Shared handle has no current payload value")
        elif isinstance(current, RNode) and current.family == "support" and current.tag == "all":
            require(step == "items" and index + 1 < len(steps), "support projection requires item index")
            item_index = steps[index + 1]
            items = current.get("items")
            require(type(item_index) is int and 0 <= item_index < len(items), "invalid support projection index")
            current = items[item_index]
            prefix += (step, item_index)
            index += 2
            continue
        else:
            require(False, "value link path has no value/support projection rule")
        prefix += (step,)
        index += 1
    if projection_owner is not None:
        from .projections import entry_at
        owner, anchor, start = projection_owner
        relative = ("chi",) + tuple(steps[len(start):])
        entry = entry_at(owner, relative)
        require(entry.get("class") == "Value", "interface-only chi has no witness projection")
        guards.extend((anchor, index, arm) for index, arm in entry.get("guards"))
    return current, tuple(parents), tuple(guards)


def hidden_paths(value, prefix):
    if isinstance(value, Binder):
        here = (prefix,) if value.kind == "requires-hidden" else ()
        return here + hidden_paths(value.body, prefix + ("body",))
    if isinstance(value, RNode) and value.family == "kappa" and value.tag == "all":
        return sum((hidden_paths(x, prefix + ("items", i)) for i, x in enumerate(value.get("items"))), ())
    return ()


def interface_key(binder):
    require(isinstance(binder, Binder), "interface binder required")
    return binder.sorts, tuple(sorted({canonical(f) for f in binder.facts}, key=repr))


def validate_links(fn):
    links = fn.get("links")
    targets = [l.get("target") for l in links]
    require(len(set(targets)) == len(targets), "duplicate link target")
    require(set(targets) == set(hidden_paths(fn.get("kappa"), ("kappa",))), "every hidden requirement needs one link")
    mapping = {l.get("target"): l.get("source") for l in links}
    for link in links:
        target_path, source_path = link.get("target"), link.get("source")
        target, target_ancestors, _ = at(fn, target_path)
        source, source_ancestors, guards = (value_source_at(fn, source_path)
                                           if link.tag == "value" else at(fn, source_path))
        require(isinstance(target, Binder) and target.kind == "requires-hidden", "link target must be kappa hidden")
        require(isinstance(source, Binder) and source.kind in ("exists", "support-hidden"), "link source must be interface")
        roles = ("chi", "parameter") if link.tag == "value" else ("chi", "parameter", "result")
        require(source_path[0] in roles, "link source role invalid")
        require(interface_key(target) == interface_key(source), "linked interfaces differ")
        require(link.get("alignment") == tuple(range(len(source.sorts))), "ordered binder alignment required")
        require(len(target_ancestors) == len(source_ancestors), "linked ancestor depth mismatch")
        for (tp, tb), (sp, sb) in zip(target_ancestors, source_ancestors):
            require(mapping.get(tp) == sp and interface_key(tb) == interface_key(sb), "linked ancestor chain mismatch")
        if link.tag == "value":
            require(link.get("guards") == guards, "Sum guards must match typed source path")
        else:
            require(not link.get("guards"), "universal link has no runtime guard")


def effect_parts(value):
    if isinstance(value, Binder) and value.kind in ("nu", "hide"):
        return (), (value,)
    require(isinstance(value, RNode) and value.family == "effect", "effect required")
    if value.tag == "set":
        return value.get("atoms"), ()
    require(value.tag == "union", "unknown effect")
    a, b = effect_parts(value.get("left"))
    c, d = effect_parts(value.get("right"))
    return a + c, b + d


def normalize_support(value, prefix=("chi",)):
    """Return normalized tree plus every surviving old->new structural path.

    Links are validated in input coordinates, then transported with this map
    during joint comparison. Removing zero/duplicate atoms must move hidden
    interface coordinates at the same time, never erase the links themselves.
    """
    def walk(current, old_path, new_path):
        mapping = {old_path: new_path}
        if isinstance(current, Binder):
            body, nested = walk(current.body, old_path + ("body",), new_path + ("body",))
            mapping.update(nested)
            return Binder(current.kind, current.sorts, current.facts, body), mapping
        if current.tag != "all":
            return current, mapping
        output, run = [], []
        def flush():
            keys = sorted({canonical(x) for _, x in run}, key=repr)
            for key in keys:
                items = [(i, x) for i, x in run if canonical(x) == key]
                index = len(output); output.append(items[0][1])
                for i, _ in items:
                    mapping[old_path + ("items", i)] = new_path + ("items", index)
            run.clear()
        for i, item in enumerate(current.get("items")):
            if isinstance(item, RNode) and item.tag == "zero":
                continue
            if isinstance(item, RNode) and item.tag == "atom":
                run.append((i, item))
                continue
            flush()
            normalized, nested = walk(item, old_path + ("items", i), new_path + ("items", len(output)))
            output.append(normalized); mapping.update(nested)
        flush()
        return node("support", "all", items=tuple(output)), mapping
    return walk(value, prefix, prefix)


def remap_path(steps, mapping):
    for length in range(len(steps), 0, -1):
        if steps[:length] in mapping:
            return mapping[steps[:length]] + steps[length:]
    return steps


def normalize_object(value, prefix=()):
    """Normalize descendants and transport each Fn's relative interface paths."""
    if isinstance(value, RNode) and value.family == "support" or isinstance(value, Binder) and value.kind == "support-hidden":
        return normalize_support(value, prefix)
    mapping = {prefix: prefix}
    if isinstance(value, Binder):
        body, inner = normalize_object(value.body, prefix + ("body",))
        mapping.update(inner)
        facts = tuple(normalize_object(f)[0] for f in value.facts)
        return Binder(value.kind, value.sorts, facts, body), mapping
    if isinstance(value, tuple):
        output = []
        for i, item in enumerate(value):
            normalized, inner = normalize_object(item, prefix + (i,))
            output.append(normalized); mapping.update(inner)
        return tuple(output), mapping
    if isinstance(value, RNode):
        is_fn = value.family == "ctype" and value.tag == "Fn"
        fields = {}
        for key, item in value.fields:
            if is_fn and key in ("links", "projections"):
                continue
            normalized, inner = normalize_object(item, prefix + (key,))
            fields[key] = normalized; mapping.update(inner)
        if is_fn:
            from .projections import normalized_projections, selector_map
            fields["projections"] = normalized_projections(value, prefix, mapping)
            links = []
            for link in value.get("links"):
                entries = dict(link.fields)
                for key in ("source", "target"):
                    entries[key] = remap_path(prefix + entries[key], mapping)[len(prefix):]
                guards = []
                for guard in entries["guards"]:
                    if len(guard) == 2:
                        p, arm = guard
                        guards.append((remap_path(prefix + p, mapping)[len(prefix):], arm))
                    else:
                        p, index, arm = guard
                        owner = value if not p else at(value, p)[0]
                        guards.append((remap_path(prefix + p, mapping)[len(prefix):], selector_map(owner)[index], arm))
                entries["guards"] = tuple(guards)
                links.append(RNode("link", link.tag, tuple(entries.items())))
            fields["links"] = tuple(links)
        return RNode(value.family, value.tag, tuple(fields.items())), mapping
    return value, mapping


def canonical(value):
    if isinstance(value, Free):
        return ("free", value.sort, value.name)
    if isinstance(value, Bound):
        return ("bound", value.sort, value.depth, value.slot)
    if isinstance(value, LiteralSize):
        return ("size", value.value)
    if isinstance(value, Binder):
        if value.kind in ("nu", "hide"):
            return ("effect", (), (effect_block_key(value),))
        return ("binder", value.kind, *interface_key(value), canonical(value.body))
    if isinstance(value, RNode):
        if value.family == "support" and value.tag == "zero":
            return ("support-all", ())
        if value.family == "ctype" and value.tag == "Fn":
            normalized, _ = normalize_object(value)
            fields = dict(normalized.fields)
            fields["links"] = tuple(sorted((canonical(l) for l in fields["links"]), key=repr))
            return ("node", "ctype", "Fn", tuple(sorted((key, val if key == "links" else canonical(val))
                                                          for key, val in fields.items())))
        if value.family == "support" and value.tag == "all":
            out, atoms = [], []
            def flush():
                out.extend(sorted(set(atoms), key=repr)); atoms.clear()
            for item in value.get("items"):
                if isinstance(item, RNode) and item.family == "support" and item.tag == "zero":
                    continue
                if isinstance(item, RNode) and item.family == "support" and item.tag == "atom":
                    atoms.append(canonical(item))
                else:
                    flush(); out.append(canonical(item))
            flush()
            return ("support-all", tuple(out))
        if value.family == "effect":
            atoms, blocks = effect_parts(value)
            return ("effect", tuple(sorted({canonical(a) for a in atoms}, key=repr)),
                    tuple(sorted((effect_block_key(b) for b in blocks), key=repr)))
        fields = []
        for key, field in value.fields:
            result = tuple(sorted((canonical(x) for x in field), key=repr)) if key == "links" and value.family == "ctype" else canonical(field)
            fields.append((key, result))
        return ("node", value.family, value.tag, tuple(sorted(fields)))
    if isinstance(value, tuple):
        return ("tuple", tuple(canonical(x) for x in value))
    if value is None or type(value) in (str, int, bool):
        return (type(value).__name__, repr(value))
    raise ValueError("unclassified canonical value")


def effect_block_key(binder):
    return ("block", binder.kind, *interface_key(binder), canonical(binder.body))


def structural_alpha_equal(left, right):
    well_scoped((left, right))
    def validate(current):
        if isinstance(current, Binder):
            for x in current.facts + (current.body,): validate(x)
        elif isinstance(current, RNode):
            if current.family == "ctype" and current.tag == "Fn":
                from .projections import validate_projections
                validate_projections(current); validate_links(current)
            for _, x in current.fields: validate(x)
        elif isinstance(current, tuple):
            for x in current: validate(x)
    validate(left); validate(right)
    return Decision("yes" if canonical(left) == canonical(right) else "no", "joint-closure-alpha")


def compare_types(left, right, context):
    """Joint alpha modulo current-world free Size aliases; Bound stays rigid."""
    well_scoped((left, right))
    check_free_references((left, right), context.allowed)
    substitutions, evidence = {}, []
    for ref in sorted(free_names((left, right)), key=lambda r: (r.sort, r.name)):
        if ref.sort != "Size":
            continue
        choices = [(v, context.size_equal(ref, v)) for v in sorted(context.size_nodes, key=repr)]
        equivalent = [(v, proof) for v, proof in choices if proof.yes]
        selected, proof = min(equivalent, key=lambda item:
            (0, item[0].value) if isinstance(item[0], LiteralSize) else (1, item[0].name))
        if selected != ref:
            substitutions[ref] = selected
            evidence.append(proof)
    a = substitute_free(left, substitutions, free_world=context.allowed)
    b = substitute_free(right, substitutions, free_world=context.allowed)
    verdict = structural_alpha_equal(a, b)
    return Decision(verdict.outcome, "joint-closure-alpha-free-size-aliases", (), tuple(evidence) + (verdict,))


def parse_type_document(raw):
    exact(raw, ("schema", "world", "type"), "closure type document")
    require(raw["schema"] == VERSION, "explicit closure schema version required")
    exact(raw["world"], ("names", "facts"), "world")
    names = tuple(old.name(n, "world.name") for n in array(raw["world"]["names"], "world.names"))
    schema = Schema(Resolver(names))
    facts = tuple(schema.relation(f) for f in array(raw["world"]["facts"], "world.facts"))
    world = ResolvedWorld(names, facts)
    result = schema.type(raw["type"])
    well_scoped(result)
    check_free_references(result, frozenset(Free(n.sort, n.text) for n in world.names))
    well_scoped(world.facts)
    return world, result
