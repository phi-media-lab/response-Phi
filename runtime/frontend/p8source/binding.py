"""Lexical FV independent of capture annotations; source paths identify binders."""
from .source_schema import source_children


def free_terms(raw, family="command"):
    tag = raw[family]
    if family == "value" and tag == "var":
        return frozenset((raw["name"],))
    if family == "value" and tag == "lambda":
        return free_terms(raw["body"]) - {raw["parameter"]["name"]}
    if family == "command":
        if tag=="fork":
            return (free_terms(raw["scope"],"value")|free_terms(raw["job"]["body"])|
                    (free_terms(raw["next"])-{raw["result_name"],raw["control_name"]}))
        if tag=="produce":
            cleanup=raw["cleanup"]
            return (free_terms(raw["scope"],"value")|free_terms(raw["argument"],"value")|
                    (free_terms(cleanup["body"])-{cleanup["parameter"]["name"]})|
                    (free_terms(raw["next"])-{raw["result_name"],raw["control_name"]}))
        if tag == "let_e":
            return free_terms(raw["expression"], "expression") | (free_terms(raw["next"]) - {raw["name"]})
        if tag == "let_c":
            return free_terms(raw["first"]) | (free_terms(raw["next"]) - {raw["name"]})
        if tag == "case":
            return (free_terms(raw["value"], "value") | (free_terms(raw["left"]) - {raw["left_name"]}) |
                    (free_terms(raw["right"]) - {raw["right_name"]}))
        if tag == "unpack":
            return free_terms(raw["package"], "value") | (free_terms(raw["body"]) - {raw["name"]})
        if tag in ("new_owner","scope","run"):
            operand=(free_terms(raw["value"],"value") if tag=="new_owner" else
                     free_terms(raw["owner"],"value") if tag=="run" else frozenset())
            return operand | (free_terms(raw["body"])-{raw["name"]})
        if tag == "try":
            return free_terms(raw["body"]) | (free_terms(raw["handler"]) - {raw["failure_name"]})
    return frozenset().union(*(free_terms(child,f) for _,f,child in source_children(raw,family)))
