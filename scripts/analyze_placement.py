#!/usr/bin/env python3
"""Independently check P5 event clocks, full numeric digests and planned coverage."""
import argparse
from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import struct


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest_string(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@lru_cache(maxsize=32)
def oracle(m, k, n, seed):
    # Independently expand full FP32 byte streams, using only the declared
    # periodic integer formula, without NumPy, MLX or harness imports.
    def row_x(i):
        return [(5*i + 11*j + seed) % 16 - 8 for j in range(16)]
    def row_w(i):
        return [(7*i + 3*j + seed + 1) % 16 - 8 for j in range(16)]
    xb = b"".join(struct.pack("<16f", *(a/128 for a in row_x(i))) * (k//16) for i in range(m))
    wb = b"".join(struct.pack("<16f", *(a/128 for a in row_w(i))) * (k//16) for i in range(n))
    out = []
    for i in range(m):
        values = [sum(max(a, 0)*b for a, b in zip(row_x(i), row_w(j))) * (k//16) / (128*128)
            for j in range(16)]
        out.append(struct.pack("<16f", *values) * (n//16))
    return {"x_sha256": sha(xb), "w_sha256": sha(wb), "expected_sha256": sha(b"".join(out))}


def case_key(case):
    return json.dumps(case, sort_keys=True)


def valid_case(c):
    return (isinstance(c, dict) and set(c) == {"M", "K", "N", "placement", "readiness", "dtype", "producer_placement", "weight_layout"}
        and type(c["M"]) is int and c["M"] in (1, 128) and type(c["K"]) is int and c["K"] == 1024
        and type(c["N"]) is int and c["N"] == 3072 and c["placement"] in ("cpu", "gpu")
        and c["readiness"] in ("ready", "lazy", "boundary") and c["dtype"] == "float32"
        and c["producer_placement"] == "gpu" and c["weight_layout"] == "transpose-view")


def inspect_trial(t, seed):
    errors, metrics = [], {}
    def require(condition, reason):
        if not condition:
            errors.append(reason)
    require(t.get("passed") is True, "trial reported failure")
    checks = t.get("checks", [])
    require(bool(checks) and all(c.get("passed") is True for c in checks), "self-check failed or absent")
    require(all(not c.get("error") for c in checks), "error preserved in self-check")
    c = t.get("config", {})
    if not valid_case(c):
        return {"passed": False, "errors": errors + ["invalid case"], "metrics": metrics}
    events = t.get("events", [])
    require(bool(events) and all(isinstance(e, dict) and finite(e.get("t_ms")) and e["t_ms"] >= 0 for e in events), "invalid events")
    if not events or any(not isinstance(e, dict) or not finite(e.get("t_ms")) for e in events):
        return {"passed": False, "errors": errors, "metrics": metrics}
    require(all(a["t_ms"] <= b["t_ms"] for a, b in zip(events, events[1:])), "nonmonotonic trace")
    by = defaultdict(list)
    for e in events:
        by[e.get("name")].append(e)
    def event(name):
        require(len(by[name]) == 1, "expected exactly one " + name)
        return by[name][0] if len(by[name]) == 1 else None
    def chain(names):
        es = [event(n) for n in names]
        if all(e is not None for e in es):
            require(all(a["t_ms"] <= b["t_ms"] for a, b in zip(es, es[1:])), "order: " + " -> ".join(names))
    mode = c["readiness"]
    producer = ["producer.graph_started", "producer.graph_created"]
    if mode != "lazy":
        producer += ["producer.eval_started", "producer.eval_returned", "input.ready_observed"]
    else:
        require(not any(by[n] for n in ["producer.eval_started", "producer.eval_returned", "input.ready_observed"]), "lazy fabricated ready boundary")
    names = ["trial.started"] + (["request.started"] if mode != "ready" else []) + producer
    names += (["request.started"] if mode == "ready" else []) + ["consumer.graph_started", "consumer.graph_created", "consumer.eval_started",
        "consumer.eval_returned", "result.available", "validation.started", "validation.completed", "trial.drained"]
    chain(names)
    for name, placement, shape in [("producer.graph_created", "gpu", [c["M"], c["K"]]),
                                  ("consumer.graph_created", c["placement"], [c["M"], c["N"]])]:
        e = event(name)
        if e:
            details = e.get("details", {})
            require(details.get("placement") == placement and details.get("shape") == shape
                and details.get("dtype") in ("float32", "mlx.core.float32"), "wrong placement/shape/dtype: " + name)
    starts, ends = by["cleanup.synchronize_started"], by["cleanup.synchronize_completed"]
    require(len(starts) == len(ends) == 2 and not by["cleanup.synchronize_failed"], "both explicit streams must synchronize")
    require(Counter(e.get("details", {}).get("stream") for e in starts) == Counter(["cpu", "gpu"])
        and Counter(e.get("details", {}).get("stream") for e in ends) == Counter(["cpu", "gpu"]), "synchronization stream identity")
    for stream in ("cpu", "gpu"):
        a = [e for e in starts if e.get("details", {}).get("stream") == stream]
        b = [e for e in ends if e.get("details", {}).get("stream") == stream]
        if len(a) == len(b) == 1 and len(by["validation.completed"]) == len(by["trial.drained"]) == 1:
            require(by["validation.completed"][0]["t_ms"] <= a[0]["t_ms"] <= b[0]["t_ms"] <= by["trial.drained"][0]["t_ms"], "drain order for " + stream)
    obs = t.get("observations", {})
    val = obs.get("validation") or {}
    require(val.get("passed") is True and val.get("finite") is True and val.get("shape_matches") is True
        and val.get("dtype_matches") is True and type(val.get("elements_checked")) is int
        and val["elements_checked"] == c["M"]*c["N"] and type(val.get("mismatched_elements")) is int
        and val["mismatched_elements"] == 0 and val.get("max_abs_error") == val.get("max_rel_error") == 0,
        "full exact numeric validation failed")
    require(val.get("actual_canonical_sha256") == oracle(c["M"], c["K"], c["N"], seed)["expected_sha256"], "independent full-output digest mismatch")
    require(digest_string(val.get("actual_sha256")), "original output byte digest missing")
    require(obs.get("matmul_inputs") == [[c["M"], c["K"]], [c["K"], c["N"]]], "matmul operands differ")
    require(all(obs.get(k) is False for k in ["gpu_completion_timestamps_available", "hardware_overlap_claimed", "amx_usage_claimed"]), "unsupported device claim")
    bounds = {"request_ms": ("request.started", "result.available"), "pipeline_ms": ("producer.graph_started", "result.available"),
        "producer_graph_ms": ("producer.graph_started", "producer.graph_created"), "consumer_graph_ms": ("consumer.graph_started", "consumer.graph_created"),
        "consumer_eval_wait_ms": ("consumer.eval_started", "consumer.eval_returned"),
        "validation_export_and_check_ms": ("validation.started", "validation.completed")}
    if mode != "lazy":
        bounds["producer_eval_wait_ms"] = ("producer.eval_started", "producer.eval_returned")
    if mode == "ready":
        bounds["ready_setup_ms"] = ("producer.graph_started", "input.ready_observed")
    reported = t.get("metrics", {})
    require(set(reported) == set(bounds), "unexpected/missing metrics")
    for key, (start, end) in bounds.items():
        if len(by[start]) == len(by[end]) == 1:
            v = by[end][0]["t_ms"] - by[start][0]["t_ms"]
            metrics[key] = v
            require(v >= 0 and finite(reported.get(key)) and math.isclose(reported[key], v, rel_tol=1e-6, abs_tol=1e-6), "metric mismatch: " + key)
    return {"trial_id": t.get("trial_id"), "config": c, "metrics": metrics, "errors": errors, "passed": not errors}


def analyze(d, source="<memory>"):
    errors, checked = [], []
    def require(condition, reason):
        if not condition:
            errors.append(reason)
    require(d.get("schema_version") == 1 and d.get("experiment") == "mlx-ready-placement", "unsupported schema")
    require(d.get("passed") is True and not d.get("error"), "top-level failure")
    cfg = d.get("config", {}); cases = cfg.get("planned_cases", [])
    seed = cfg.get("seed")
    require(type(seed) is int, "invalid seed")
    require(bool(cases) and all(valid_case(c) for c in cases), "invalid planned cases")
    if not cases or not all(valid_case(c) for c in cases) or type(seed) is not int:
        return {"source": source, "passed": False, "errors": errors, "trials": []}
    keys = [case_key(c) for c in cases]
    require(len(set(keys)) == len(keys), "duplicate planned case")
    if cfg.get("matrix") is True:
        require(len(keys) == 12, "matrix must contain all twelve cases")
    else:
        require(cfg.get("matrix") is False and len(keys) == 1, "invalid single-case scope")
    p = d.get("provenance", {})
    require(all(digest_string(p.get(k)) for k in ["harness_sha256", "protocol_sha256", "mlx_module_sha256", "mlx_library_sha256"]), "missing source/binary hashes")
    packages = p.get("packages", {})
    require(isinstance(packages, dict) and all(isinstance(packages.get(k), str) and packages[k] for k in ["mlx", "numpy"]), "missing backend versions")
    shape = p.get("shape_source", {})
    require(isinstance(shape, dict) and shape.get("K") == 1024 and shape.get("N") == 3072
        and shape.get("packed_shape") == [3072,128] and shape.get("packed_dtype") == "U32"
        and shape.get("tensor") == "model.layers.0.mlp.up_proj.weight"
        and all(digest_string(shape.get(k)) for k in ["config_sha256", "safetensors_header_sha256"]), "shape source evidence missing or inconsistent")
    inputs = d.get("inputs", {})
    require(set(inputs) == {str(c["M"]) for c in cases}, "input identities missing or extra")
    for c in cases:
        identity = inputs.get(str(c["M"]), {})
        require(all(identity.get(k) == v for k, v in oracle(c["M"], c["K"], c["N"], seed).items()), "independent input/oracle identity mismatch")
        require(identity.get("period") == 16 and identity.get("scale") == 128, "numeric contract changed")
    setup = d.get("setup", [])
    require(Counter(s.get("M") for s in setup) == Counter({c["M"] for c in cases}), "setup coverage mismatch")
    for s in setup:
        require(not s.get("error") and not s.get("cleanup_errors"), "setup error preserved")
        es = s.get("events", [])
        require([e.get("name") for e in es] == ["cpu_data_and_oracle.started", "cpu_data_and_oracle.completed", "base_inputs.eval_started", "base_inputs.eval_returned"], "setup completion evidence missing")
        require(all(finite(e.get("t_ms")) for e in es) and all(a.get("t_ms", 0) <= b.get("t_ms", 0) for a,b in zip(es, es[1:])), "invalid setup clock")
    ids = set()
    for name, countkey, warming in [("warmups", "warmup_per_config", True), ("trials", "trials_per_config", False)]:
        count = cfg.get(countkey)
        require(type(count) is int and count >= (0 if warming else 1), "invalid repetition count")
        if type(count) is not int or count < 0:
            continue
        rows = d.get(name, [])
        require(len(rows) == count * len(keys), "missing " + name)
        coverage = Counter()
        for t in rows:
            require(type(t.get("trial_id")) is int and t["trial_id"] not in ids, "duplicate/invalid trial id")
            ids.add(t.get("trial_id"))
            require(t.get("warmup") is warming and type(t.get("repetition")) is int, "wrong warmup/repetition label")
            coverage[t.get("repetition"), case_key(t.get("config"))] += 1
            result = inspect_trial(t, seed)
            if not result["passed"]:
                errors.append(f"{name} trial {t.get('trial_id')} failed independent validation")
            if not warming:
                checked.append(result)
        require(coverage == Counter({(i,k):1 for i in range(count) for k in keys}), "repetition/case coverage mismatch: " + name)
    groups = []
    for key in keys:
        rows = [t for t in checked if case_key(t.get("config")) == key and t["passed"]]
        groups.append({"config": json.loads(key), "n": len(rows), "metrics": {
            k: {"median": statistics.median(t["metrics"][k] for t in rows), "min": min(t["metrics"][k] for t in rows), "max": max(t["metrics"][k] for t in rows)}
            for k in rows[0]["metrics"]} if rows else {}})
    return {"source": source, "passed": not errors, "total_trials": len(checked), "errors": errors,
        "trials": checked, "groups": groups, "notes": [
            "Ready request excludes its producer; pipeline includes producer graph to output in all modes.",
            "Placement is checked against recorded explicit stream selection. Source bytes and library identities require archive verification; no hardware counter proves AMX or overlap.",
            "Full canonical output hashes are independently recomputed from the bounded periodic integer formula.",
            "Finite pilot samples are not response bounds or representative real-model performance."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    result = analyze(json.loads(raw), str(args.input))
    result["source_sha256"] = sha(raw)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        with args.output.open("x") as out:
            out.write(encoded)
    else:
        print(encoded, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
