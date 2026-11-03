"""Check a closed program or an explicitly open fragment; never execute it."""
import argparse
import json
import sys
from p8foundation.model import serialize
from .entry import check_closed_main, check_open_fragment
from .sigma import fixed_sigma
from .source_schema import CLOSED_VERSION, VERSION


class SourceJSONError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceJSONError("duplicate JSON field: " + key)
        result[key] = value
    return result


def reject_constant(value):
    raise SourceJSONError("non-JSON numeric constant: " + value)


def load_source(stream):
    return json.load(stream, object_pairs_hook=unique_object,
                     parse_constant=reject_constant)


def failure(verdict, code, message):
    return {"verdict": verdict,
            "diagnostic": {"code": code, "path": [], "message": message},
            "derivation": None, "premises": []}


def check_source(source, entry):
    if entry == "auto":
        schema = source.get("schema") if type(source) is dict else None
        if schema == CLOSED_VERSION:
            entry = "closed"
        elif schema == VERSION:
            entry = "open"
        else:
            return failure("semantic-no", "source-schema",
                           "expected a closed or open generative source schema")
    # An explicit mode does not migrate the source or invent missing premises.
    result = (check_closed_main(fixed_sigma(), source)
              if entry == "closed" else check_open_fragment(fixed_sigma(), source))
    known = {"checked-closed-main-source",
             "checked-source-fragment-under-explicit-premises",
             "semantic-no", "unsupported", "infrastructure-error"}
    if result.verdict not in known:
        return failure("infrastructure-error", "unknown-verdict",
                       "checker returned an undeclared verdict")
    return {"verdict": result.verdict, "diagnostic": result.diagnostic,
            "derivation": serialize(result.derivation),
            "premises": serialize(result.premises)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="UTF-8 JSON source file, or - for stdin")
    parser.add_argument("--entry", choices=("auto", "closed", "open"), default="auto",
                        help="source entry; auto selects the exact schema (default)")
    args = parser.parse_args(argv)
    try:
        if args.source == "-":
            source = load_source(sys.stdin)
        else:
            with open(args.source, encoding="utf-8") as stream:
                source = load_source(stream)
        output = check_source(source, args.entry)
    except (json.JSONDecodeError, SourceJSONError, UnicodeError) as error:
        output = failure("semantic-no", "source-json", str(error))
    except OSError as error:
        output = failure("infrastructure-error", "source-io", str(error))
    except Exception as error:
        output = failure("infrastructure-error", type(error).__name__, str(error))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if output["verdict"] in ("checked-closed-main-source",
                             "checked-source-fragment-under-explicit-premises"):
        return 0
    return 2 if output["verdict"] == "infrastructure-error" else 1


if __name__=="__main__":
    raise SystemExit(main())
