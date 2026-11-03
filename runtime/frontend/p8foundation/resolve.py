"""Field-sorted resolution against explicit ambient names and binder frames.

Input classes come from the separately frozen frontend syntax, not its checker.
Source binders are resolved without claiming their typing/admission rules pass.
"""
from p8static import syntax as ast
from .model import Free, Bound, LiteralSize, RNode, Binder, node, require, FoundationError


TYPE_REFS = {
    "Owner": {"owner": "Owner"}, "Scope": {"scope": "Scope", "region": "Region"},
    "Shared": {"job": "Job"}, "Control": {"job": "Job"},
    "Lease": {"scope": "Scope", "owner": "Owner", "epoch": "Epoch", "lease": "Lease", "region": "Region"},
    "Read": {"access": "Access", "owner": "Owner", "epoch": "Epoch", "region": "Region"},
    "Write": {"owner": "Owner", "epoch": "Epoch"},
}
REL_REFS = {
    "owner": {"owner": "Owner"}, "epoch": {"epoch": "Epoch", "owner": "Owner"},
    "epoch_owner": {"epoch":"Epoch","owner":"Owner"},
    "scope_shape": {"scope":"Scope","region":"Region"},
    "scope": {"scope": "Scope", "region": "Region", "parent_invocation": "Invocation"},
    "job": {"job": "Job", "task": "Task", "scope": "Scope"},
    "jobRegion": {"job": "Job", "region": "Region"},
    "bodyRegion": {"job": "Job", "region": "Region"}, "result": {"job": "Job"},
    "producerOf": {"job": "Job", "scope": "Scope", "owner": "Owner", "epoch": "Epoch"},
    "access": {"access": "Access", "job": "Job", "owner": "Owner", "epoch": "Epoch", "region": "Region"},
    "outlives": {"inner": "Region", "outer": "Region"}, "positiveSize": {}, "sizeEqual": {},
}
ATOM_REFS = {"Alloc": {}, "Raise": {}, "Enter": {"scope": "Scope"},
    "Register": {"scope": "Scope"}, "Issue": {"owner": "Owner"}, "Read": {"owner": "Owner"},
    "HostInput": {"owner": "Owner"}, "HostResponse": {"owner": "Owner"},
    "HostService": {"scope": "Scope"}, "LocalRead": {},
    "Need": {"job": "Job"}, "WaitDetach": {"job": "Job"},
    "RequestCancel": {"job": "Job"}, "Drain": {"scope": "Scope"}}
FOOT_SORTS = {"scope": ("Scope", "Region"), "lease": ("Scope", "Owner", "Epoch", "Lease", "Region"),
    "read": ("Access", "Owner", "Epoch", "Region"), "write": ("Owner", "Epoch"), "region": ("Region",)}
SOURCE_BINDERS = {
    "new_owner": (("size_name", "Size"), ("owner_name", "Owner")),
    "scope": (("scope_name", "Scope"), ("region_name", "Region"), ("invocation_name", "Invocation")),
    "run": (("epoch_name", "Epoch"), ("scope_name", "Scope"), ("region_name", "Region"), ("invocation_name", "Invocation")),
    "lease": (("lease_name", "Lease"),),
}
JOB_NAMES = (("job", "Job"), ("task", "Task"), ("invocation", "Invocation"), ("region", "Region"))
SOURCE_GROUPS = {
    "produce": (JOB_NAMES + (("write_access", "Access"), ("cleanup_access", "Access"), ("cleanup_region", "Region")), ("cleanup", "next")),
    "fork": (JOB_NAMES, ("body", "next")),
    "fork_read": (JOB_NAMES + (("access", "Access"), ("read_region", "Region")), ("body", "next")),
}


class Resolver:
    def __init__(self, names, frames=()):
        names = tuple(names)
        require(len({n.text for n in names}) == len(names), "duplicate ambient name")
        require(all(n.sort in ast.SORTS for n in names), "unknown ambient sort")
        self.names = names
        self.ambient = {n.text: n.sort for n in names}
        self.frames = frames  # nearest frame first; each frame is ordered (display, sort)

    def enter(self, names):
        names = tuple(names)
        require(len({n.text for n in names}) == len(names), "duplicate binder in one frame")
        require(all(n.sort in ast.SORTS for n in names), "unknown binder sort")
        frame = tuple((n.text, n.sort) for n in names)
        return Resolver(self.names, (frame,) + self.frames)

    def ref(self, name, sort):
        for depth, frame in enumerate(self.frames):
            for slot, (display, actual_sort) in enumerate(frame):
                if name == display:
                    require(actual_sort == sort, "bound name sort mismatch: " + name)
                    return Bound(sort, depth, slot)
        require(self.ambient.get(name) == sort, "missing or wrongly sorted nominal name: " + name)
        return Free(sort, name)

    def witness(self, witness, sort):
        require(isinstance(witness, ast.Witness), "structured Witness required")
        if witness.size is not None:
            require(witness.name is None and sort == "Size", "only Size can use a size witness")
            return self.resolve(witness.size)
        require(witness.name is not None, "missing witness")
        return self.ref(witness.name, sort)

    def binder(self, kind, names, facts, body):
        inner = self.enter(names)
        return Binder(kind, tuple(n.sort for n in names), tuple(inner.resolve(f) for f in facts), inner.resolve(body))

    def resolve(self, value):
        if isinstance(value, ast.Program):
            require(value.world.names == self.names and not self.frames, "Program resolution requires its exact free world")
            return node("program", "open-judgment", signature=self.resolve(value.signature),
                world_facts=self.resolve(value.world.facts), theta=self.resolve(value.theta),
                copy=self.resolve(value.copy), affine=self.resolve(value.affine),
                body=self.resolve(value.body), result=self.resolve(value.result))
        if isinstance(value, ast.Binding):
            return node("context", "binding", name=value.name, type=self.resolve(value.type))
        if isinstance(value, ast.Signature):
            return node("signature", "registry", pure=self.resolve(value.pure), kernels=self.resolve(value.kernels))
        if isinstance(value, ast.PureSignature):
            return node("signature", "pure", name=value.name, parameters=self.resolve(value.parameters), result=self.resolve(value.result))
        if isinstance(value, ast.KernelSignature):
            inner = self.enter((value.size_binder,)) if value.size_binder is not None else self
            body = node("signature", "kernel", name=value.name, parameter=inner.resolve(value.parameter),
                        size=inner.resolve(value.size), postcondition=value.postcondition)
            return Binder("kernel-size", ("Size",), (), body) if value.size_binder is not None else body
        if isinstance(value, ast.Size):
            if value.literal is not None:
                require(value.witness is None, "ambiguous Size")
                return LiteralSize(value.literal)
            require(value.witness is not None, "missing Size")
            return self.ref(value.witness, "Size")
        if isinstance(value, ast.Type):
            if value.tag == "Exists":
                interface = value.get("interface")
                return self.binder("exists", interface.names, interface.facts, value.get("body"))
            return self.structured(value, "type", TYPE_REFS.get(value.tag, {}))
        if isinstance(value, ast.Relation):
            require(value.tag in REL_REFS, "unclassified relation")
            return self.structured(value, "relation", REL_REFS[value.tag])
        if isinstance(value, ast.Foot):
            require(value.tag in FOOT_SORTS and len(value.names) == len(FOOT_SORTS[value.tag]), "invalid Foot shape")
            return node("foot", value.tag, names=tuple(self.ref(n, s) for n, s in zip(value.names, FOOT_SORTS[value.tag])))
        if isinstance(value, ast.Effect):
            if value.tag == "nu":
                return self.binder("nu", (value.get("name"),), (), value.get("body"))
            if value.tag in ("set", "union"):
                return RNode("effect", value.tag, tuple((k, self.resolve(v)) for k, v in value.fields))
            require(value.tag in ATOM_REFS, "unclassified effect atom")
            return self.structured(value, "atom", ATOM_REFS[value.tag])
        if isinstance(value, ast.Node):
            return self.source(value)
        if isinstance(value, ast.Theta):
            return node("context", "theta", job=self.ref(value.job, "Job"), task=self.ref(value.task, "Task"),
                invocation=self.ref(value.invocation, "Invocation"), region=self.ref(value.region, "Region"),
                scope_stack=tuple(self.ref(s, "Scope") for s in value.scope_stack))
        if isinstance(value, (tuple, frozenset)):
            # Set normalization belongs to comparison, never to name resolution.
            return tuple(self.resolve(v) for v in value)
        if value is None or type(value) in (str, int, bool):
            return value
        raise FoundationError("unclassified AST object: " + type(value).__name__)

    def structured(self, value, family, references):
        # Explicitly classified fields; no arbitrary string-field fallback.
        from p8static.parser import TYPE_FIELDS, REL_FIELDS, ATOM_FIELDS
        schemas = {"type": TYPE_FIELDS, "relation": REL_FIELDS, "atom": ATOM_FIELDS}[family]
        require(value.tag in schemas, "unclassified structured constructor")
        schema = schemas[value.tag]
        require(set(dict(value.fields)) == set(schema), "wrong structured fields")
        result = []
        for key, field in value.fields:
            if key in references:
                resolved = None if field is None and key == "parent_invocation" else self.ref(field, references[key])
            elif key == "kind":
                resolved = "plain" if field == "plain" else (self.ref(field[0], "Owner"), self.ref(field[1], "Epoch"))
            elif schema[key] in ("type", "size", "foot", "effect"):
                resolved = self.resolve(field)
            elif schema[key] in ("service_literal", "capability_literal"):
                from p8static.parser import convert
                resolved = convert(field,schema[key],key)
            elif (family, key) in (("type", "mode"), ("relation", "ordinal"), ("relation", "role"), ("atom", "mode")):
                resolved = field
            else:
                raise FoundationError("unclassified field: " + family + "/" + value.tag + "/" + key)
            result.append((key, resolved))
        return RNode(family, value.tag, tuple(result))

    def source(self, value):
        schemas = {"v": ast.VALUE_FIELDS, "e": ast.EXPR_FIELDS, "c": ast.COMMAND_FIELDS}
        require(value.family in schemas and value.tag in schemas[value.family], "unknown source constructor")
        schema = schemas[value.family][value.tag]
        require(set(dict(value.fields)) == set(schema), "wrong source fields")
        if value.tag == "pack" and value.family == "v":
            interface = value.get("interface")
            require(len(interface.names) == len(value.get("witnesses")), "pack witness arity")
            return node("v", "pack", interface=self.binder("exists", interface.names, interface.facts, value.get("payload_type")),
                witnesses=tuple(self.witness(w, n.sort) for w, n in zip(value.get("witnesses"), interface.names)),
                value=self.resolve(value.get("value")))
        if value.tag == "unpack" and value.family == "c":
            interface = value.get("interface")
            return node("c", "unpack", value=self.resolve(value.get("value")), name=value.get("name"),
                body=self.binder("source:unpack", interface.names, interface.facts, value.get("body")))
        names, scoped_keys, omitted = (), (), set()
        if value.family == "c" and value.tag in SOURCE_BINDERS:
            definitions = SOURCE_BINDERS[value.tag]
            names = tuple(ast.Name(value.get(k), sort) for k, sort in definitions)
            scoped_keys = ("next",) if value.tag == "lease" else ("body",)
            omitted = {k for k, _ in definitions}
        if value.family == "c" and value.tag in SOURCE_GROUPS:
            definitions, scoped_keys = SOURCE_GROUPS[value.tag]
            supplied = dict(value.get("names"))
            require(set(supplied) == {k for k, _ in definitions}, "wrong source name group")
            names = tuple(ast.Name(supplied[k], sort) for k, sort in definitions)
            omitted = {"names"}
        fields = []
        if names:
            inner = self.enter(names)
            group = RNode("scope-body", value.tag, tuple((key, inner.resolve(value.get(key))) for key in scoped_keys))
            fields.append(("nominal_scope", Binder("source:" + value.tag, tuple(n.sort for n in names), (), group)))
        for key, field in value.fields:
            if key in omitted or key in scoped_keys:
                continue
            conversion = schema[key]
            if conversion in ("id", "ids", "mode", "nat", "int", "bool", "ints"):
                fields.append((key, field))  # term names, lexical labels and literal data
            elif conversion in ("v", "e", "c", "values", "type", "foot", "effect"):
                fields.append((key, self.resolve(field)))
            else:
                raise FoundationError("unclassified source field: " + value.tag + "/" + key)
        return RNode(value.family, value.tag, tuple(fields))
