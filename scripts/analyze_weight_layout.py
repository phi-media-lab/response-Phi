#!/usr/bin/env python3
"""Independent standard-library audit of MLX layout work, lifecycle and clocks.

Does not import MLX, NumPy, the harness, its oracle or its protocol. Full dense
FP32 byte hashes are rebuilt from the integer formulas using their period.
"""
from __future__ import annotations
import argparse
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import struct

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = ("transpose-view", "contiguous-each-request", "contiguous-once")
M, K, N = 128, 1024, 3072
WEIGHT_BYTES, INPUT_BYTES, OUTPUT_BYTES = N*K*4, M*K*4, M*N*4
TRIAL_CHECKS = {"trial_succeeded", "all_fixed_dense_work_completed", "all_outputs_match_independent_integer_oracle",
    "all_layout_handles_closed", "all_explicit_streams_drained", "preparation_count_matches_strategy", "view_count_matches_strategy"}
PROBE_CHECKS = {"probe_succeeded", "stale_generation_rejected_before_submission", "both_generations_match_full_oracle",
    "all_probe_handles_closed", "all_probe_streams_drained"}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def integer(x, minimum=0):
    return type(x) is int and x >= minimum


def finite(x):
    return type(x) in (int, float) and math.isfinite(x)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read(path):
    def invalid(value):
        raise ValueError("nonfinite JSON number: " + value)
    return json.loads(path.read_text(), parse_constant=invalid)


def exact(actual, expected, label):
    # JSON equality also distinguishes true/false from integer zero/one.
    need(json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False), label)


def subset(actual, expected, label):
    need(isinstance(actual, dict), label + " must be object")
    for key, value in expected.items():
        exact(actual.get(key), value, label + "." + key)


def status(value, checks=None):
    need(isinstance(value, dict) and value.get("passed") is True, "failed/missing status must remain failed")
    need(not value.get("error") and not value.get("errors") and not value.get("cleanup_errors")
         and not value.get("truncated"), "unexpected error/truncation preserved")
    if checks is not None:
        rows = value.get("checks")
        need(isinstance(rows, list) and len(rows) == len(checks), "missing/extra checks")
        need({r.get("name") for r in rows} == checks, "check identities differ")
        need(all(r.get("passed") is True and not r.get("error") for r in rows), "original checks failed")


def periodic_hash(rows, row_repeats, column_repeats):
    block = b"".join(struct.pack("<16f", *row) * column_repeats for row in rows)
    h = hashlib.sha256()
    for _ in range(row_repeats):
        h.update(block)
    return h.hexdigest()


@lru_cache(maxsize=8)
def oracle(seed, requests):
    need(type(seed) is int and requests in (1, 8, 32), "unsupported input formula configuration")
    xs = []; ws = {}; ys = {"0": [], "1": []}
    for r in range(requests):
        rows = [[(32*((5*i+11*k+seed) % 16-8)+r)/4096 for k in range(16)] for i in range(16)]
        xs.append(periodic_hash(rows, M//16, K//16))
    for g in (0, 1):
        weight = [[(7*n+3*k+seed+g) % 16-8 for k in range(16)] for n in range(16)]
        ws[str(g)] = periodic_hash([[v/128 for v in row] for row in weight], N//16, K//16)
        for r in range(requests):
            rows = []
            for i in range(16):
                row = []
                for n in range(16):
                    numerator = sum((32*((5*i+11*k+seed) % 16-8)+r)*weight[n][k] for k in range(16))*(K//16)
                    need(abs(numerator) < 2**24, "integer oracle exceeds exact FP32 range")
                    row.append(numerator/(4096*128))
                rows.append(row)
            ys[str(g)].append(periodic_hash(rows, M//16, N//16))
    return {"input_sha256": xs, "weight_sha256": ws, "expected_sha256": ys}


def source(g, hashes):
    return {"source_id": "up-projection", "generation": g, "content_sha256": hashes["weight_sha256"][str(g)],
            "shape": [N, K], "dtype": "float32"}


def handle(h, g, hashes, contiguous, uses, closed=True, retired=False):
    return {"handle_id": h, "source": source(g, hashes), "layout": "row-contiguous" if contiguous else "transpose-view",
        "ready": True, "retired": retired, "closed": closed, "active_leases": [], "total_acquired": uses,
        "completed_uses": uses, "holds_source": not closed, "holds_payload": not closed}


def preparation(h, g, hashes, contiguous):
    d = {"handle_id": h, "source": source(g, hashes), "layout": "row-contiguous" if contiguous else "transpose-view"}
    payload = {"shape": [K, N], "dtype": "mlx.core.float32", "logical_nbytes": WEIGHT_BYTES}
    rows = [("layout.prepare_started", d)] if contiguous else []
    rows.append(("layout.view_constructed", dict(d, payload=payload)))
    if contiguous:
        rows.extend([("layout.contiguous_constructed", dict(d, payload=payload, allow_col_major=False, placement="cpu")),
            ("layout.eval_started", d), ("layout.eval_returned", d),
            ("layout.ready_observed", dict(d, evidence="explicit preparation eval returned"))])
    else:
        rows.append(("layout.view_available", dict(d, evidence="source values ready; transpose view graph constructed, no separate preparation eval")))
    return rows


def use(h, g, lease, request):
    d = {"handle_id": h, "generation": g, "lease_id": lease, "request_index": request}
    return [("layout.use_acquired", d), ("consumer.graph_started", dict(d, placement="gpu")),
        ("consumer.graph_created", dict(d, shape=[M, N], dtype="mlx.core.float32")),
        ("consumer.eval_started", d), ("consumer.eval_returned", d), ("result.available", d),
        ("layout.use_completed", dict(d, evidence="consumer eval returned"))]


def sync():
    return [(name, {"stream": stream}) for stream in ("cpu", "gpu")
            for name in ("cleanup.synchronize_started", "cleanup.synchronize_completed")]


def events(actual, expected):
    need(isinstance(actual, list) and len(actual) == len(expected), "missing/extra lifecycle event (including unexpected work/error)")
    last = -1
    for index, (row, (name, details)) in enumerate(zip(actual, expected)):
        need(isinstance(row, dict) and row.get("name") == name and row.get("request_id") == "system", f"event order/identity at {index}: expected {name}")
        stamp = row.get("t_ms")
        need(finite(stamp) and stamp >= 0 and stamp >= last, "event clocks must be finite, nonnegative and monotonic")
        last = stamp
        subset(row.get("details"), details, name)
    return actual


def validations(rows, hashes):
    need(isinstance(rows, list) and len(rows) == len(hashes), "missing full output validation")
    for row, expected in zip(rows, hashes):
        subset(row, {"passed": True, "shape_matches": True, "dtype_matches": True, "finite": True,
            "elements_checked": M*N, "mismatched_elements": 0, "actual_canonical_sha256": expected}, "independent full-output oracle")
        need(finite(row.get("max_abs_error")) and row["max_abs_error"] == 0 and finite(row.get("max_rel_error")) and row["max_rel_error"] == 0,
            "full numerical error must be zero")
        need(isinstance(row.get("actual_sha256"), str) and re.fullmatch("[0-9a-f]{64}", row["actual_sha256"]), "missing raw-byte hash")


def setup(document, maximum):
    value = document["setup"]
    need(not value.get("error") and not value.get("cleanup_errors"), "setup failure must not disappear")
    events(value["events"], [("cpu_inputs_oracle.started", {}), ("cpu_inputs_oracle.completed", {})] +
           [(name, {"stream": "cpu"}) for name in ("base_inputs.stream_context_entered", "base_inputs.eval_started",
                                                   "base_inputs.eval_returned", "base_inputs.stream_context_exited")])
    subset(value.get("logical_payload"), {"max_requests": maximum, "ready_mlx_inputs_bytes": maximum*INPUT_BYTES,
        "ready_mlx_weight_generations_bytes": 2*WEIGHT_BYTES, "weight_generations": [0, 1],
        "cpu_input_arrays_bytes": maximum*INPUT_BYTES, "cpu_weight_arrays_bytes": 2*WEIGHT_BYTES,
        "cpu_full_oracle_outputs_bytes": maximum*2*OUTPUT_BYTES,
        "construction_temporaries_included": False, "allocator_or_process_peak_claimed": False}, "setup logical payload")


def probe(value, hashes):
    status(value, PROBE_CHECKS)
    rejection = value.get("expected_rejection")
    subset(rejection, {"type": "StaleGenerationError", "message": "prepared weight generation is stale"}, "expected rejection")
    traceback = rejection.get("traceback")
    need(isinstance(traceback, str) and traceback.count("Traceback (most recent call last):") == 1
         and not any(s in traceback for s in ("During handling", "direct cause"))
         and re.fullmatch(r"(?:[A-Za-z_]\w*\.)*StaleGenerationError: prepared weight generation is stale", traceback.strip().splitlines()[-1]),
         "unknown/chained exception cannot become expected rejection")
    counters = {"graph_calls": 1, "eval_calls": 1}
    subset(value, {"counters_before": counters, "counters_after": counters, "input_index": 0}, "rejection counters")
    sequence = preparation(0, 0, hashes, True) + use(0, 0, 0, 0) + [
        ("source.generation_published", {"source": source(1, hashes), "retired_handle": handle(0, 0, hashes, True, 1, False, True)}),
        ("stale_use.attempted", {"handle_id": 0, "requested_generation": 1, "counters": counters}),
        ("stale_use.rejected", rejection)] + preparation(1, 1, hashes, True) + use(1, 1, 0, 1) + [
        ("generation_recovery.result_validated", {"generation": 1, "passed": True})] + sync() + [
        ("layout.closed", {"handle_id": 0, "generation": 0}), ("layout.closed", {"handle_id": 1, "generation": 1}), ("trial.drained", {})]
    events(value["events"], sequence)
    validations(value["validation"], [hashes["expected_sha256"][str(g)][0] for g in (0, 1)])
    exact(value["final_handles"], [handle(0, 0, hashes, True, 1, retired=True), handle(1, 1, hashes, True, 1)], "probe final handles")
    return {"passed": True, "generation_0_and_1_full_hashes_verified": True, "no_stale_graph_or_eval": True,
            "publication_with_outstanding_leases": False, "exact_device_submission_observed": False}


def intervals(rows, start, end, key):
    a = {e["details"][key]: e["t_ms"] for e in rows if e["name"] == start}
    b = {e["details"][key]: e["t_ms"] for e in rows if e["name"] == end}
    need(a.keys() == b.keys(), "incomplete metric interval")
    return [b[i]-a[i] for i in sorted(a)]


def metric_equal(actual, expected, label):
    if isinstance(expected, list):
        need(isinstance(actual, list) and len(actual) == len(expected), "metric array length: " + label)
        for a, b in zip(actual, expected):
            metric_equal(a, b, label)
    elif type(expected) is int:
        exact(actual, expected, label)
    else:
        need(finite(actual) and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-8), "metric does not follow events: " + label)


def trial(value, hashes, config):
    status(value, TRIAL_CHECKS)
    c = value["config"]; strategy, requests = c["strategy"], c["R"]
    need(strategy in STRATEGIES and type(requests) is int and requests in (1, 8, 32), "unsupported strategy/R")
    exact(c, dict(strategy=strategy, R=requests, M=M, K=K, N=N, dtype="float32",
                  consumer_placement="gpu", preparation_placement="cpu", generation=0), "trial config")
    contiguous, once = strategy != "transpose-view", strategy == "contiguous-once"
    count = 1 if once else requests
    prepared = count if contiguous else 0
    sequence = [("batch.started", {"strategy": strategy, "R": requests})]
    if once:
        sequence += preparation(0, 0, hashes, True)
    for r in range(requests):
        sequence.append(("request.started", {"request_index": r}))
        if not once:
            sequence += preparation(r, 0, hashes, contiguous)
        sequence += use(0 if once else r, 0, r if once else 0, r)
        if not once:
            sequence.append(("layout.closed", {"handle_id": r, "generation": 0}))
    sequence += [("batch.results_available", {"outputs": requests}), ("validation.started", {}), ("validation.completed", {})] + sync()
    if once:
        sequence.append(("layout.closed", {"handle_id": 0, "generation": 0}))
    sequence.append(("trial.drained", {}))
    rows = events(value["events"], sequence)
    obs = value["observations"]
    validations(obs["validation"], hashes["expected_sha256"]["0"][:requests])
    exact(obs["final_handles"], [handle(h, 0, hashes, contiguous, requests if once else 1) for h in range(count)], "final handle states")
    subset(obs, {"source_weight_bytes": WEIGHT_BYTES, "prepared_payload_bytes_per_object": WEIGHT_BYTES,
        "total_prepared_payload_bytes": prepared*WEIGHT_BYTES,
        "peak_explicit_handle_prepared_payload_bytes": WEIGHT_BYTES if contiguous else 0,
        "used_input_subset_bytes": requests*INPUT_BYTES, "retained_output_bytes": requests*OUTPUT_BYTES,
        "physical_buffer_identity_proven": False, "amx_usage_claimed": False}, "trial logical payload")
    stamp = lambda name: next(e["t_ms"] for e in rows if e["name"] == name)
    batch = stamp("batch.results_available")-stamp("batch.started")
    need(batch > 0, "batch duration must be positive")
    measured = {"batch_ms": batch, "amortized_ms": batch/requests,
        "request_ms": intervals(rows, "request.started", "result.available", "request_index"),
        "consumer_ms": intervals(rows, "consumer.graph_started", "result.available", "request_index"),
        "prepare_ms": intervals(rows, "layout.prepare_started", "layout.ready_observed", "handle_id"),
        "prepare_eval_ms": intervals(rows, "layout.eval_started", "layout.eval_returned", "handle_id"),
        "contiguous_calls": prepared, "view_calls": count, "consumer_calls": requests}
    need(set(value["metrics"]) == set(measured), "missing/unknown metric")
    for key, expected in measured.items():
        metric_equal(value["metrics"][key], expected, key)
    return {"passed": True, "trial_id": value["trial_id"], "repetition": value["repetition"],
        "order_position": value["order_position"], "warmup": value["warmup"], "config": c, "metrics": measured,
        "validation_ms": stamp("validation.completed")-stamp("validation.started"),
        "final_cleanup_ms": stamp("trial.drained")-stamp("validation.completed"),
        "complete_output_elements_checked": requests*M*N, "prepared_objects": prepared,
        "logical_payload": {k: v for k, v in obs.items() if k.endswith("bytes") or k.endswith("bytes_per_object")}}


def stats(values):
    return {"n": len(values), "median": statistics.median(values), "min": min(values), "max": max(values)}


def analyze_document(document, name="<memory>"):
    result = {"source": name, "passed": False, "errors": [], "trials": [], "warmups": [], "paired": []}
    try:
        status(document)
        exact(document.get("schema_version"), 1, "unsupported schema")
        need(document.get("experiment") == "mlx-weight-layout", "unsupported experiment")
        c = document["config"]
        subset(c, {"M": M, "K": K, "N": N, "dtype": "float32", "consumer_placement": "gpu", "preparation_placement": "cpu"}, "top config")
        need(type(c.get("seed")) is int and type(c.get("matrix")) is bool and integer(c.get("trials_per_config"), 1)
             and integer(c.get("warmup_per_config")), "invalid top config counts")
        planned = c["planned_cases"]
        if c["matrix"]:
            exact(planned, [{"strategy": s, "R": r} for r in (1, 8, 32) for s in STRATEGIES], "complete nine-cell plan")
        else:
            need(isinstance(planned, list) and len(planned) == 1 and planned[0].get("strategy") in STRATEGIES
                 and type(planned[0].get("R")) is int and planned[0]["R"] in (1, 8, 32), "invalid one-cell plan")
        maximum = max(p["R"] for p in planned)
        hashes = oracle(c["seed"], maximum)
        subset(document["inputs"], dict(hashes, seed=c["seed"], M=M, K=K, N=N, period=16, input_scale=4096, weight_scale=128), "full input/weight/reference hashes")
        setup(document, maximum)
        result["generation_probe"] = probe(document["generation_probe"], hashes)
        provenance = document["provenance"]
        for key in ("harness_sha256", "protocol_sha256", "helper_sha256", "helper_protocol_sha256", "mlx_module_sha256", "mlx_library_sha256"):
            need(isinstance(provenance.get(key), str) and re.fullmatch("[0-9a-f]{64}", provenance[key]), "missing source/library hash")
        need(provenance.get("amx_usage_claimed") is False and provenance.get("clock") == "time.perf_counter_ns; host observations, no exact GPU timestamps", "incorrect evidence scope")
        result["source_fingerprint"] = digest(provenance)
        result["comparison_context"] = {"seed": c["seed"], "maximum_setup_requests": maximum,
                                        "matrix": c["matrix"], "warmup_per_config": c["warmup_per_config"]}
        next_id = 0
        for warming, key, repetitions in ((True, "warmups", c["warmup_per_config"]), (False, "trials", c["trials_per_config"])):
            raw = document[key]
            need(isinstance(raw, list) and len(raw) == len(planned)*repetitions, "incomplete trial/warmup count")
            cursor = 0
            for rep in range(repetitions):
                order = []
                for requests in sorted({p["R"] for p in planned}):
                    group = [p for p in planned if p["R"] == requests]
                    off = rep % len(group)
                    group = group[off:]+group[:off]
                    order += list(reversed(group)) if rep % 2 else group
                for position, case in enumerate(order):
                    value = raw[cursor]; cursor += 1
                    subset(value, {"trial_id": next_id, "repetition": rep, "order_position": position, "warmup": warming}, "trial identity/order")
                    subset(value["config"], case, "recorded strategy sequence")
                    result[key].append(trial(value, hashes, c)); next_id += 1
        if c["matrix"]:
            result["order_balance"] = []
            for requests in (1, 8, 32):
                permutations = {}
                positions = {s: [0, 0, 0] for s in STRATEGIES}
                for rep in range(c["trials_per_config"]):
                    ordered = sorted((r for r in result["trials"] if r["config"]["R"] == requests and r["repetition"] == rep), key=lambda r: r["order_position"])
                    names = [r["config"]["strategy"] for r in ordered]
                    label = " → ".join(names)
                    permutations[label] = permutations.get(label, 0)+1
                    for index, strategy in enumerate(names):
                        positions[strategy][index] += 1
                balanced = all(len(set(v)) == 1 for v in positions.values())
                if c["trials_per_config"] % 6 == 0:
                    need(len(permutations) == 6 and set(permutations.values()) == {c["trials_per_config"]//6} and balanced,
                         "six-repetition plan must cover all permutations and balanced positions")
                result["order_balance"].append({"R": requests, "permutation_counts": permutations,
                    "position_counts": positions, "position_balanced": balanced,
                    "all_six_permutations_observed": len(permutations) == 6,
                    "R_group_order": "fixed ascending; not randomized"})
                for left, right in (("contiguous-each-request", "transpose-view"), ("contiguous-once", "transpose-view"), ("contiguous-once", "contiguous-each-request")):
                    pairs = []
                    for rep in range(c["trials_per_config"]):
                        group = {r["config"]["strategy"]: r for r in result["trials"] if r["config"]["R"] == requests and r["repetition"] == rep}
                        pairs.append({"repetition": rep, "left_trial_id": group[left]["trial_id"], "right_trial_id": group[right]["trial_id"],
                                      "batch_delta_ms": group[left]["metrics"]["batch_ms"]-group[right]["metrics"]["batch_ms"],
                                      "amortized_delta_ms": group[left]["metrics"]["amortized_ms"]-group[right]["metrics"]["amortized_ms"],
                                      "batch_ratio": group[left]["metrics"]["batch_ms"]/group[right]["metrics"]["batch_ms"],
                                      "amortized_ratio": group[left]["metrics"]["amortized_ms"]/group[right]["metrics"]["amortized_ms"]})
                    result["paired"].append({"R": requests, "difference": left+" minus "+right, "pairs": pairs,
                        "batch_delta_ms": stats([p["batch_delta_ms"] for p in pairs]),
                        "amortized_delta_ms": stats([p["amortized_delta_ms"] for p in pairs]),
                        "ratio_meaning": left+" divided by "+right,
                        "batch_ratio": stats([p["batch_ratio"] for p in pairs]),
                        "amortized_ratio": stats([p["amortized_ratio"] for p in pairs])})
        result["passed"] = True
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError, StopIteration) as error:
        result["errors"].append(str(error))
    return result


def artifact_audit(path):
    d = read(path); p = d["provenance"]; run = read(path.with_name("run.json"))
    need(path.name == "stdout.json" and type(run.get("returncode")) is int and run["returncode"] == 0
         and run.get("timed_out") is False, "unsuccessful recorded process")
    command = run.get("command")
    need(isinstance(command, list) and len(command) >= 4 and command[:2] == [p["python_executable"], "-B"]
         and Path(run["cwd"]).resolve() == ROOT and (ROOT/command[2]).resolve() == ROOT/"experiments/mlx-weight-layout/run.py",
         "recorded interpreter/harness/cwd differ")
    options = {"--matrix": False, "--json": False, "--strategy": "contiguous-once", "--requests": 1,
               "--trials": 3, "--warmup": 1, "--seed": 7, "--model": p["shape_source"]["model_path"]}
    seen_options = set(); index = 3
    while index < len(command):
        option = command[index]; index += 1
        need(option in options or option == "--output", "unrecognized recorded option")
        need(option not in seen_options, "duplicated recorded option")
        seen_options.add(option)
        if option in ("--matrix", "--json"):
            options[option] = True
        else:
            need(index < len(command), "missing recorded option value")
            value = command[index]; index += 1
            options[option] = int(value) if option in ("--requests", "--trials", "--warmup", "--seed") else value
    c = d["config"]
    need(options["--json"] is True, "recorded command must emit complete stdout JSON")
    exact({"matrix": options["--matrix"], "trials_per_config": options["--trials"], "warmup_per_config": options["--warmup"], "seed": options["--seed"]},
          {k: c[k] for k in ("matrix", "trials_per_config", "warmup_per_config", "seed")}, "command/config agreement")
    if not options["--matrix"]:
        exact(c["planned_cases"], [{"strategy": options["--strategy"], "R": options["--requests"]}], "command's planned case")
    need(Path(options["--model"]).resolve() == Path(p["shape_source"]["model_path"]).resolve(), "recorded shape source differs")
    need(set(run["files"]) == {"stdout.json", "stderr.log"}, "unexpected process output set")
    for file, item in run["files"].items():
        saved = path.with_name(file)
        exact(item, {"bytes": saved.stat().st_size, "sha256": sha(saved)}, "recorded output digest")
    actual_sources = {str(ROOT/"experiments/mlx-weight-layout/run.py"): p["harness_sha256"],
        str(ROOT/"experiments/mlx-weight-layout/protocol.py"): p["protocol_sha256"],
        p["helper_source"]: p["helper_sha256"], str(Path(p["helper_source"]).with_name("protocol.py")): p["helper_protocol_sha256"]}
    library = Path(p["mlx_module"]).parent / "lib/libmlx.dylib"
    for file, expected in dict(actual_sources, **{p["mlx_module"]: p["mlx_module_sha256"], str(library): p["mlx_library_sha256"]}).items():
        need(sha(Path(file)) == expected, "recorded source/library changed: " + file)
    shape = p["shape_source"]; model = Path(shape["model_path"])
    need(sha(model/"config.json") == shape["config_sha256"], "model shape config changed")
    with (model/"model.safetensors").open("rb") as stream:
        length = struct.unpack("<Q", stream.read(8))[0]
        need(length <= 16*1024*1024, "unexpected safetensors header length")
        header = stream.read(length)
    need(hashlib.sha256(header).hexdigest() == shape["safetensors_header_sha256"], "model header changed")
    model_config = read(model/"config.json")
    need((model_config["hidden_size"], model_config["intermediate_size"]) == (K, N), "model dimensions differ")
    tensor = json.loads(header)[shape["tensor"]]
    need(tensor["shape"] == [N, K//8] and tensor["dtype"] == "U32", "packed shape provenance differs")
    return {"passed": True, "run_sha256": sha(path.with_name("run.json")), "source_sha256": actual_sources,
            "model_used_for_shape_only": True}


def analyze_paths(paths):
    documents = []; groups = {}; errors = []; sources = {}
    seen = set()
    for path in paths:
        path = Path(path)
        try:
            need(path.resolve() not in seen, "duplicate input file")
            seen.add(path.resolve())
            result = analyze_document(read(path), str(path))
            result["artifact_audit"] = artifact_audit(path)
            sources.update(result["artifact_audit"]["source_sha256"])
            documents.append(result)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            documents.append({"source": str(path), "passed": False, "errors": [str(error)], "trials": [], "warmups": []})
    for d in documents:
        if not d["passed"]:
            errors.extend(f"{d['source']}: {e}" for e in d["errors"])
            continue
        for row in d["trials"]:
            identity = {"source_fingerprint": d["source_fingerprint"], "context": d["comparison_context"], "config": row["config"]}
            key = digest(identity)
            group = groups.setdefault(key, dict(identity, trial_records=[]))
            group["trial_records"].append({"source": d["source"], "trial_id": row["trial_id"], "repetition": row["repetition"],
                                            "order_position": row["order_position"], "metrics": row["metrics"]})
    for group in groups.values():
        group["n"] = len(group["trial_records"])
        group["metrics"] = {key: stats([t["metrics"][key] for t in group["trial_records"]]) for key in ("batch_ms", "amortized_ms")}
    return {"analysis_schema_version": 1, "passed": bool(documents) and not errors,
        "total_trials": sum(len(d["trials"]) for d in documents), "errors": errors,
        "source_sha256": sources, "analyzer_sha256": sha(Path(__file__)), "groups": list(groups.values()), "documents": documents,
        "notes": ["No harness/oracle/MLX import; full input, two-generation weight and output hashes are independently recomputed.",
            "All clocks are host observations. Batch includes strategy preparation; request timing differs in scope across strategies.",
            "Pairs are within a complete recorded matrix and repetition, then summarized; separate smoke processes are not paired.",
            "Payload is logical held tensor data, not physical residency, traffic, allocator peak or AMX evidence.",
            "Generation probe has no outstanding lease at publication. It does not test in-flight device replacement.",
            "Pilot medians/ranges only; neither performance improvement nor a stable tail bound is a PASS requirement."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = analyze_paths(args.inputs)
    encoded = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)+"\n"
    if args.output:
        with args.output.open("x") as stream:
            stream.write(encoded)
    else:
        print(encoded, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
