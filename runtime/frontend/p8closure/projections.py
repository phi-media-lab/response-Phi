"""Public total chi qualification, finite selector incidence and transport."""
from p8foundation.model import Binder, RNode, node, require


def hidden_entries(s, path=("chi",), ancestors=()):
    if isinstance(s, Binder):
        require(s.kind == "support-hidden", "chi binder kind")
        return ((path, ancestors),) + hidden_entries(s.body, path + ("body",), ancestors + (path,))
    if s.tag == "all":
        return sum((hidden_entries(x, path + ("items", i), ancestors)
                    for i, x in enumerate(s.get("items"))), ())
    return ()


def parse_projections(raw):
    from .schema import exact, array, path, old
    def guard(g):
        exact(g, ("selector", "arm"), "projection guard")
        require(g["arm"] in ("left", "right"), "projection guard arm")
        return old.natural(g["selector"], "selector"), g["arm"]
    exact(raw, ("selectors", "entries"), "projections")
    selectors, entries = [], []
    for s in array(raw["selectors"], "selectors"):
        exact(s, ("index", "parents"), "selector")
        selectors.append(node("projection", "selector", index=old.natural(s["index"], "index"),
                              parents=tuple(guard(g) for g in array(s["parents"], "parents"))))
    for e in array(raw["entries"], "entries"):
        exact(e, ("path", "class", "ancestors", "guards"), "projection entry")
        require(e["class"] in ("Value", "Interface"), "projection class")
        entries.append(node("projection", "entry", path=path(e["path"]), **{"class": e["class"]},
                            ancestors=tuple(path(p) for p in array(e["ancestors"], "ancestors")),
                            guards=tuple(guard(g) for g in array(e["guards"], "guards"))))
    return node("projection", "contract", selectors=tuple(selectors), entries=tuple(entries))


def entry_at(fn, path):
    found = [e for e in fn.get("projections").get("entries") if e.get("path") == path]
    require(len(found) == 1, "missing/duplicate public chi projection")
    return found[0]


def validate_projections(fn):
    contract = fn.get("projections")
    entries, selectors = contract.get("entries"), contract.get("selectors")
    expected = dict(hidden_entries(fn.get("chi")))
    require(len(entries) == len(expected) and {e.get("path") for e in entries} == set(expected),
            "projection entries must exactly cover chi hidden occurrences")
    declared, used = {}, set()
    def guards(gs):
        seen = []
        for index, arm in gs:
            require(index in declared and arm in ("left", "right"), "undeclared selector/arm")
            require(index not in {i for i, _ in seen}, "repeated/contradictory selector guard")
            parents = declared[index]
            require(tuple(seen[:len(parents)]) == parents, "selector parent guard prefix")
            seen.append((index, arm)); used.add(index)
    for s in selectors:
        index = s.get("index")
        require(type(index) is int and index >= 0 and index not in declared, "duplicate selector index")
        guards(s.get("parents")); declared[index] = s.get("parents")
    for e in entries:
        require(e.get("class") in ("Value", "Interface"), "projection class")
        require(e.get("ancestors") == expected[e.get("path")], "projection ancestor mismatch")
        guards(e.get("guards"))
        for ancestor in e.get("ancestors"):
            a = entry_at(fn, ancestor)
            require(e.get("guards")[:len(a.get("guards"))] == a.get("guards"), "ancestor guard prefix")
            require(a.get("class") == "Value" or e.get("class") == "Interface", "Interface ancestor cannot expose Value")
    require(used == set(declared), "unused selector declaration")


def selector_map(fn):
    selectors = {s.get("index"): s.get("parents") for s in fn.get("projections").get("selectors")}
    mapping = {}
    def visit(index):
        if index in mapping:
            return
        for parent, _ in selectors[index]:
            visit(parent)
        mapping[index] = len(mapping)
    for p, _ in hidden_entries(fn.get("chi")):
        for index, _ in entry_at(fn, p).get("guards"):
            visit(index)
    return mapping


def normalized_projections(fn, prefix, transport):
    from .schema import remap_path
    mapping = selector_map(fn)
    def gs(guards):
        return tuple((mapping[i], arm) for i, arm in guards)
    def path(p):
        return remap_path(prefix + p, transport)[len(prefix):]
    selectors = tuple(sorted((node("projection", "selector", index=mapping[s.get("index")], parents=gs(s.get("parents")))
                              for s in fn.get("projections").get("selectors")), key=lambda s: s.get("index")))
    entries = tuple(node("projection", "entry", path=path(p), **{"class": entry_at(fn, p).get("class")},
                         ancestors=tuple(path(a) for a in ancestors), guards=gs(entry_at(fn, p).get("guards")))
                    for p, ancestors in hidden_entries(fn.get("chi")))
    return node("projection", "contract", selectors=selectors, entries=entries)
