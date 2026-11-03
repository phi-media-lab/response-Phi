"""Resolved, locally nameless static objects; never runtime resource tags."""
from dataclasses import dataclass


class FoundationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise FoundationError(message)


@dataclass(frozen=True)
class Free:
    sort: str
    name: str


@dataclass(frozen=True)
class Bound:
    sort: str
    depth: int
    slot: int


@dataclass(frozen=True)
class LiteralSize:
    value: int

    def __post_init__(self):
        require(type(self.value) is int and self.value >= 0, "Size literal must be Nat, not bool")


@dataclass(frozen=True)
class RNode:
    family: str
    tag: str
    fields: tuple = ()

    def get(self, key):
        return dict(self.fields)[key]


@dataclass(frozen=True)
class Binder:
    kind: str
    sorts: tuple[str, ...]
    facts: tuple
    body: object


@dataclass(frozen=True)
class Decision:
    outcome: str
    rule: str
    details: tuple = ()
    premises: tuple = ()

    @property
    def yes(self):
        return self.outcome == "yes"


@dataclass(frozen=True)
class SourceAddress:
    input_sha256: str
    pointer: str

    def __post_init__(self):
        require(len(self.input_sha256) == 64 and all(c in "0123456789abcdef" for c in self.input_sha256), "canonical input SHA256 required")
        require(type(self.pointer) is str and (self.pointer == "" or self.pointer.startswith("/")), "JSON pointer required")


@dataclass(frozen=True)
class WitnessPlan:
    node_address: SourceAddress
    kind: str
    substitutions: tuple
    relation_decisions: tuple
    payload: object
    executed: bool = False


def node(family, tag, **fields):
    return RNode(family, tag, tuple(fields.items()))


def children(value):
    if isinstance(value, RNode):
        return tuple(v for _, v in value.fields)
    if isinstance(value, Binder):
        return value.facts + (value.body,)
    if isinstance(value, tuple):
        return value
    return ()


def serialize(value):
    """Inspectable resolved representation, not an executable source serializer."""
    if isinstance(value, Free):
        return {"free": value.name, "sort": value.sort}
    if isinstance(value, Bound):
        return {"bound_depth": value.depth, "slot": value.slot, "sort": value.sort}
    if isinstance(value, LiteralSize):
        return {"size_literal": value.value}
    if isinstance(value, RNode):
        return {"family": value.family, "tag": value.tag,
                "fields": {k: serialize(v) for k, v in value.fields}}
    if isinstance(value, Binder):
        return {"binder": value.kind, "ordered_sorts": list(value.sorts),
                "facts": [serialize(v) for v in value.facts], "body": serialize(value.body)}
    if isinstance(value, Decision):
        return {"outcome": value.outcome, "rule": value.rule,
                "details": serialize(value.details), "premises": serialize(value.premises)}
    if isinstance(value, WitnessPlan):
        return {"node_address": serialize(value.node_address), "kind": value.kind,
                "substitutions": serialize(value.substitutions),
                "relation_decisions": serialize(value.relation_decisions),
                "payload": serialize(value.payload), "executed": value.executed}
    if isinstance(value, SourceAddress):
        return {"canonical_input_sha256": value.input_sha256, "json_pointer": value.pointer}
    if isinstance(value, tuple):
        return [serialize(v) for v in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise FoundationError("unclassified resolved object: " + type(value).__name__)
