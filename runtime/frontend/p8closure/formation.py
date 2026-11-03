"""Whole-world and recursive new-type formation, separate from entailment."""
from p8foundation.model import RNode, Binder, LiteralSize, node, require
from p8foundation.algebra import well_scoped, check_free_references
from .schema import canonical, compare_types, validate_links, effect_parts
from .projections import validate_projections
from .support import open_interface, stable, has_write
from .requirements import Unsupported


def one(world, tag, **fields):
    found = [f for f in world.facts if f.tag == tag and all(f.get(k) == v for k, v in fields.items())]
    require(found, "missing world relation " + tag)
    require(all(compare_types(found[0], f, world).yes for f in found), "conflicting world relation " + tag)
    return found[0]


def form_world(world, check_types=True, stack=()):
    # ClosureContext already checks sorted free references, structural duplicate
    # identity consistency, Size alias conflicts, and contradictory positivity.
    for f in world.facts:
        if f.tag == "owner":
            require(world.positive(f.get("size")).yes, "owner requires positive Size")
        elif f.tag in ("epoch","epoch_owner"):
            world.epoch_association(f.get("epoch"))
            one(world, "owner", owner=f.get("owner"))
        elif f.tag in ("scope","scope_shape"):
            world.scope_mapping(f.get("scope"))
            if f.get("kind") != "plain":
                owner, epoch = f.get("kind")
                require(world.epoch_association(epoch).get("owner")==owner,"owned Scope epoch owner mismatch")
                one(world,"owner",owner=owner)
        elif f.tag == "job":
            boundary = f.get("boundary")
            require(isinstance(boundary,RNode) and boundary.family=="boundary", "job registration boundary")
            if boundary.tag == "ByScope":
                require(set(dict(boundary.fields))=={"scope"}, "ByScope fields")
                world.scope_mapping(boundary.get("scope"))
            else:
                require(boundary.tag=="HostRoot" and not boundary.fields, "invalid registration boundary")
                origin=world.init_origin
                require(origin is not None, "HostRoot requires internal closed INIT provenance")
                require(f.get("job")==origin.get("main") and f.get("task")==origin.get("task"),
                        "HostRoot main/Task mismatch")
                require(stable(f.get("result")), "closed main result must be Stable")
                one(world,"jobRegion",job=f.get("job"),region=origin.get("main_region"))
                one(world,"bodyRegion",job=f.get("job"),role="ordinary",region=origin.get("main_region"))
            result = one(world, "result", job=f.get("job")).get("result")
            require(compare_types(result, f.get("result"), world).yes, "job/result disagreement")
        elif f.tag in ("jobRegion", "bodyRegion"):
            one(world, "job", job=f.get("job"))
        elif f.tag == "producerOf":
            job=one(world, "job", job=f.get("job"))
            require(job.get("boundary")==node("boundary","ByScope",scope=f.get("scope")),
                    "producer requires matching ordinary scope registration")
            require(world.scope_mapping(f.get("scope")).get("kind")== (f.get("owner"),f.get("epoch")),
                    "producer owned Scope mapping mismatch")
        elif f.tag == "access":
            one(world, "job", job=f.get("job"))
            require(world.epoch_association(f.get("epoch")).get("owner")==f.get("owner"),"access epoch owner mismatch")
        if check_types:
            for _, value in f.fields:
                if isinstance(value, Binder) or isinstance(value, RNode) and value.family in ("type", "ctype"):
                    form_type(value, world, stack)
    return world


def form_interface(binder, world, label, stack=()):
    inner,body,refs=open_interface(binder,world,label)
    form_world(inner,False,stack)
    for fact in inner.facts[len(world.facts):]:
        for _,value in fact.fields:
            if isinstance(value,Binder) or isinstance(value,RNode) and value.family in ("type","ctype"):
                form_type(value,inner,stack)
    return inner,body,refs


def form_effect(effect, world, stack=()):
    well_scoped(effect); check_free_references(effect, world.allowed)
    atoms, blocks = effect_parts(effect)
    for block in blocks:
        inner, body, _ = form_interface(block, world, "effect_form",stack)
        form_effect(body, inner,stack)


def form_type(t, world, stack=()):
    well_scoped(t); check_free_references(t, world.allowed)
    key = canonical(t)
    if key in stack:
        raise Unsupported("cyclic embedded type formation dependency")
    stack = stack + (key,)
    if isinstance(t, Binder):
        require(t.kind == "exists", "type binder is not Exists")
        inner, body, _ = form_interface(t, world, "type_form",stack)
        form_type(body, inner, stack)
        return
    if t.tag in ("Unit", "Bool", "Nat", "Word", "Vector", "Failure", "ErrorPayload", "FaultSet"):
        return
    if t.tag in ("Pair", "Sum"):
        form_type(t.get("left"), world, stack); form_type(t.get("right"), world, stack); return
    if t.tag == "Fn":
        from .requirements import minimum, use
        validate_projections(t); validate_links(t)
        form_type(t.get("parameter"), world, stack); form_type(t.get("result"), world, stack)
        form_effect(t.get("effect"), world,stack)
        require(t.get("mode") != "U" or not has_write(t.get("chi")), "U cannot capture Write")
        use(t.get("chi"), world,stack)
        # Validate even vacuous explicitly written hidden interfaces.
        def kform(k, w):
            if isinstance(k, Binder):
                inner, b, _ = form_interface(k, w, "kappa_form",stack)
                kform(b, inner)
            elif k.tag == "all":
                for b in k.get("items"): kform(b, w)
        kform(t.get("kappa"), world)
        minimum(t, world,stack)
        return
    if t.tag == "Owner":
        require(world.positive(t.get("size")).yes, "Owner positive Size")
        require(world.size_equal(one(world, "owner", owner=t.get("owner")).get("size"), t.get("size")).yes,
                "Owner Size mismatch")
    elif t.tag == "Scope":
        mapping=world.scope_mapping(t.get("scope"))
        require(mapping.get("region")==t.get("region") and mapping.get("kind")==t.get("kind"),"Scope type mapping mismatch")
    elif t.tag == "Shared":
        form_type(t.get("payload"), world, stack)
        require(stable(t.get("payload")), "Shared payload must be Stable")
        result = one(world, "result", job=t.get("job")).get("result")
        require(compare_types(result, t.get("payload"), world).yes, "Shared/result mismatch")
    elif t.tag == "Control":
        one(world, "result", job=t.get("job"))
    elif t.tag in ("Read", "Write", "Lease"):
        require(world.epoch_association(t.get("epoch")).get("owner")==t.get("owner"),"type epoch owner mismatch")
        if t.tag == "Read":
            one(world, "access", access=t.get("access"), owner=t.get("owner"), epoch=t.get("epoch"), region=t.get("region"))
        if t.tag == "Lease":
            mapping=world.scope_mapping(t.get("scope"))
            require(mapping.get("region")==t.get("region") and mapping.get("kind")== (t.get("owner"),t.get("epoch")),
                    "Lease scope mapping mismatch")
        if t.tag == "Write":
            raise Unsupported("Write kernel-context formation")
    else:
        raise Unsupported("type formation: " + t.tag)
