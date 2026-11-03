"""Typed support provenance. No fallback from failed Value to Interface."""
from dataclasses import dataclass
from p8foundation.model import Binder, RNode, node, require
from .support import support_of, signature_support, all_support, stable
from .projections import hidden_entries, entry_at, validate_projections


@dataclass(frozen=True)
class Source:
    support_path: tuple
    source_path: tuple
    qualification: str
    guards: tuple


@dataclass(frozen=True)
class Qualified:
    tree: object
    sources: tuple


def qualified_support(t, origin=("parameter",), mode="Value"):
    records = []
    def walk(value, source, out, current_mode, guards):
        if isinstance(value, Binder):
            require(value.kind == "exists", "support requires Exists")
            records.append(Source(out, source, current_mode, guards))
            return Binder("support-hidden", value.sorts, value.facts,
                          walk(value.body, source + ("body",), out + ("body",), current_mode, guards))
        if value.tag in ("Pair", "Sum"):
            children = []
            for i, arm in enumerate(("left", "right")):
                gs = guards + ((source, arm),) if value.tag == "Sum" and current_mode == "Value" else guards
                children.append(walk(value.get(arm), source + (arm,), out + ("items", i), current_mode, gs))
            return all_support(children)
        if value.tag == "Shared":
            require(stable(value.get("payload")), "Shared payload must be Stable")
            return walk(value.get("payload"), source + ("payload",), out, "Interface", guards)
        if value.tag == "Fn":
            validate_projections(value)
            chi = value.get("chi")
            for p, _ in hidden_entries(chi):
                entry = entry_at(value, p)
                gs = guards
                if current_mode == "Value":
                    gs += tuple((source, index, arm) for index, arm in entry.get("guards"))
                cls = "Value" if current_mode == entry.get("class") == "Value" else "Interface"
                records.append(Source(out + ("items", 0) + p[1:], source + p, cls, gs))
            return all_support((chi, signature_support(value)))
        return support_of(value)
    tree = walk(t, origin, (), mode, ())
    return Qualified(tree, tuple(records))


def capture_support(types):
    """Return exact ordered chi, public contract, and private typed routes."""
    trees, sources = [], []
    for i, t in enumerate(types):
        q = qualified_support(t, ("capture", i))
        trees.append(q.tree)
        sources.extend(Source(("chi", "items", i) + s.support_path, s.source_path, s.qualification, s.guards)
                       for s in q.sources)
    chi = all_support(trees)
    selectors, index_for, entries = [], {}, []
    records = {s.support_path: s for s in sources}
    for path, ancestors in hidden_entries(chi):
        record = records[path]
        public_guards = []
        for guard in record.guards:
            identity, arm = guard[:-1], guard[-1]
            if identity not in index_for:
                index = len(index_for); index_for[identity] = index
                selectors.append(node("projection", "selector", index=index, parents=tuple(public_guards)))
            public_guards.append((index_for[identity], arm))
        entries.append(node("projection", "entry", path=path, **{"class": record.qualification},
                            ancestors=ancestors, guards=tuple(public_guards)))
    contract = node("projection", "contract", selectors=tuple(selectors), entries=tuple(entries))
    return chi, contract, tuple(sources)


def chi_sources(fn):
    return tuple(Source(p, p, entry_at(fn, p).get("class"),
                        tuple(((), i, arm) for i, arm in entry_at(fn, p).get("guards")))
                 for p, _ in hidden_entries(fn.get("chi")))
