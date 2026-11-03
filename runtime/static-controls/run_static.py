#!/usr/bin/env python3
"""SRC1 re-export of fourteen original CM2 methods / twenty-three inputs; no worker or runtime.

All sources/expectations are saved before the first fixed exporter child starts.
Each child receives original source bytes over stdin and its entire stdout and
stderr go directly to files, including malformed/failed/timeout responses.
The root runs this once under the fixed 60-second recorded outer budget.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "frontend/export.py"
GENERATOR = ROOT / "programs/build_cm2_programs.py"
CONTRACT = {"id": "p8-cm1-mlx-backend-return-v1", "allocation_kind": "MLXPrefillOwner1",
    "word_slots": 1, "private_kv_layers": 28, "return_obligation": "BackendReturn-v1",
    "unknown_policy": "host-incomplete-sticky-v1"}
SIGMA = {"id": "p8-src1-fixed-sigma-v1", "abi": "p8-src1-runtime-sigma-abi-v1",
    "operations": [
        {"name": "Prefill32", "kind": "kernel", "implementation": "qwen3-week3-mlx-prefill32-token1-v1"},
        {"name": "Prefill8", "kind": "kernel", "implementation": "qwen3-week3-mlx-prefill8-token1-v1"},
        {"name": "body_error", "kind": "pure", "implementation": "body-error-unit-v1"},
        {"name": "B", "kind": "host-input", "implementation": "ebd1-receive-request-tokens8-v1"},
        {"name": "B.response", "kind": "host-response", "implementation": "src1-offer-response-word-v1"}],
    "backend_contract": CONTRACT,
    "host_input_contract": {"id": "p8-ebd1-external-request-v1", "port": "B", "command": "receive_request",
        "result_type": "Tokens8", "caller": "init-main-job-only", "owner_authority": "identity-only-no-storage-access",
        "submission": "single-irrevocable-complete-request-v1"},
    "host_response_contract": {"id": "src1-explicit-response-intent-v1", "port": "B", "registry_name": "B.response",
        "command": "offer_response", "payload_type": "Word", "result_type": "Unit", "caller": "init-main-job-only",
        "owner_authority": "identity-only-no-storage-access", "submission": "single-valid-intent-after-receipt-v1",
        "commit": "original-main-joined-all-source-safe-v1"}}
METHOD_CASES = ()  # populated from the fixed pure builder in main
INPUT_CASES = ()

def pin(path):
    data = path.read_bytes()
    return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def save(path, value): path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n")


def data_nodes(value, path=()):
    """JSON traversal only; no import of source parser/checker or AST execution."""
    yield path, value
    if type(value) is dict:
        for key, child in value.items(): yield from data_nodes(child, path + (key,))
    elif type(value) is list:
        for index, child in enumerate(value): yield from data_nodes(child, path + (index,))


def pointer(path): return "/" + "/".join(str(x).replace("~", "~0").replace("/", "~1") for x in path)


def wire_decoder(program):
    cache = {}
    def visit(ref):
        index = int(ref["ref"])
        if index in cache: return cache[index]
        raw = program["static_values"][index]
        if type(raw) is dict and "integer" in raw: value = int(raw["integer"])
        elif type(raw) is dict and "array" in raw: value = [visit(x) for x in raw["array"]]
        elif type(raw) is dict and "object" in raw: value = {k: visit(v) for k, v in raw["object"]}
        else: value = raw
        cache[index] = value
        return value
    return visit


def ty(tag, **fields): return {"family": "type", "tag": tag, "fields": fields}


def tokens_type(count):
    result = ty("Word")
    for _ in range(count - 1): result = ty("Pair", left=ty("Word"), right=result)
    return result


def package_type():
    q = {"bound_depth": 0, "slot": 0, "sort": "Job"}
    return {"binder": "exists", "ordered_sorts": ["Job"], "facts": [
        {"family": "relation", "tag": "result", "fields": {"job": q, "result": ty("Word")}}],
        "body": ty("Pair", left=ty("Shared", job=q, payload=ty("Word")), right=ty("Word"))}


class Records:
    def __init__(self, output, builder, deadline):
        self.output, self.builder = output, builder
        self.deadline, self.abort_reason = deadline, None
        self.cases, self.methods = {}, []
        # Materialize all finite inputs before any candidate child. This is
        # generation, not a checker result; unstarted cases remain explicit.
        for method, case in INPUT_CASES:
            directory = output / case
            directory.mkdir()
            data = builder.source_bytes(case)
            (directory / "source.json").write_bytes(data)
            save(directory / "expectation.json", builder.EXPECTATIONS[case])
            observation = {"case": case, "method": method, "state": "not-started",
                "source": pin(directory / "source.json"), "expected": builder.EXPECTATIONS[case]}
            save(directory / "observation.json", observation)
            self.cases[case] = {"directory": directory, "data": data, "observation": observation}

    def exchange(self, case):
        record = self.cases[case]
        directory, data, observation = record["directory"], record["data"], record["observation"]
        if observation["state"] != "not-started": raise RuntimeError("no duplicate static case execution")
        remaining = self.deadline - time.monotonic()
        if self.abort_reason is not None or remaining <= 0:
            self.abort_reason = self.abort_reason or "internal 55-second deadline exhausted before child"
            observation["not_started_reason"] = self.abort_reason
            save(directory / "observation.json", observation)
            raise RuntimeError(self.abort_reason)
        exporter = ROOT / "static-controls/check_open.py" if self.builder.EXPECTATIONS[case]["route"] == "open" else EXPORT
        argv = [str(Path(sys.executable).resolve()), "-I", "-S", "-B", str(exporter)]
        observation.update({"state": "started", "argv": argv, "cwd": str(ROOT),
            "remaining_internal_seconds_at_start": remaining, "exporter": pin(exporter), "timeout": False})
        save(directory / "observation.json", observation)
        started = time.monotonic()
        child = None
        try:
            with (directory / "response.stdout.json").open("wb") as stdout, (directory / "response.stderr.log").open("wb") as stderr:
                child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, cwd=ROOT)
                observation["child_pid"] = child.pid
                timeout = max(0, self.deadline - time.monotonic())
                observation["child_timeout_seconds"] = timeout
                try:
                    child.communicate(input=data, timeout=timeout)
                    observation["actual_exit"] = child.returncode
                    observation["direct_child_reaped"] = True
                except subprocess.TimeoutExpired:
                    self.abort_reason = "exporter timeout; no subsequent case is started"
                    observation.update({"timeout": True, "kill_sent": True})
                    child.kill()
                    # Five seconds remain outside the 55-second internal work
                    # deadline. Only this direct exporter is killed and reaped.
                    observation["actual_exit"] = child.wait(timeout=min(3, max(0, self.deadline + 3 - time.monotonic())))
                    observation["direct_child_reaped"] = True
                finally:
                    if child.stdin is not None: child.stdin.close()
        except BaseException:
            self.abort_reason = self.abort_reason or "exporter launch/transport failure"
            observation["launch_exception"] = traceback.format_exc()
            if child is not None:
                if child.poll() is None:
                    child.kill()
                    observation["kill_sent"] = True
                try:
                    observation["actual_exit"] = child.wait(timeout=min(3, max(0, self.deadline + 3 - time.monotonic())))
                    observation["direct_child_reaped"] = True
                except subprocess.TimeoutExpired:
                    observation["direct_child_reaped"] = False
                    observation["reap_exception"] = traceback.format_exc()
            raise
        finally:
            observation["elapsed_seconds"] = time.monotonic() - started
            observation["state"] = "child-finished"
            observation["abort_reason"] = self.abort_reason
            for name in ("response.stdout.json", "response.stderr.log"):
                if (directory / name).exists(): observation[name] = pin(directory / name)
            save(directory / "observation.json", observation)
        if observation["timeout"]:
            raise RuntimeError(self.abort_reason)
        raw = (directory / "response.stdout.json").read_bytes()
        try:
            response = json.loads(raw)
            if type(response) is not dict: raise ValueError("frontend response is not an object")
        except (ValueError, UnicodeError):
            self.abort_reason = "invalid exporter response; no subsequent case is started"
            observation["response_parse_error"] = traceback.format_exc()
            save(directory / "observation.json", observation)
            raise
        observation.update({"actual_status": response.get("status"), "actual_diagnostic": response.get("diagnostic")})
        save(directory / "observation.json", observation)
        if response.get("status") not in ("ready", "static-rejected", "execution-profile-not-supported") or observation["actual_exit"] not in (0, 1, 3):
            self.abort_reason = "exporter infrastructure failure; no subsequent case is started"
            observation["abort_reason"] = self.abort_reason
            save(directory / "observation.json", observation)
            raise RuntimeError(self.abort_reason)
        return response, observation

    def observe(self, case, **values):
        record = self.cases[case]
        record["observation"].setdefault("assertion_data", {}).update(values)
        save(record["directory"] / "observation.json", record["observation"])


class CM2StaticTests(unittest.TestCase):
    records = None

    def exchange(self, case):
        response, observed = self.records.exchange(case)
        expected = self.records.builder.EXPECTATIONS[case]
        self.assertFalse(observed["timeout"])
        self.assertTrue(observed["direct_child_reaped"])
        self.assertEqual(observed["actual_exit"], expected["frontend_exit"])
        self.assertEqual(response["schema"], "cm2-open-static-response-v1" if expected["route"] == "open" else "p8-runtime-frontend-response-v1")
        self.assertEqual(response["status"], expected["status"], response.get("diagnostic"))
        if expected["status"] != "ready":
            self.assertIsNone(response["program"])
            diagnostic = response["diagnostic"]
            self.assertEqual(diagnostic["phase"], expected["diagnostic_phase"])
            self.assertEqual(diagnostic["code"], expected["diagnostic_code"])
            self.assertEqual(diagnostic["message"], expected["diagnostic_message"])
            self.assertIsInstance(diagnostic["path"], list)
            self.records.observe(case, exact_diagnostic=diagnostic, rejection_before_runtime=True)
            return response
        program = response["program"]
        self.assertIsNone(response["diagnostic"])
        self.assertEqual(program["profile"], expected["profile"])
        self.assertEqual(program["sigma"], SIGMA)
        data = self.records.cases[case]["data"]
        self.assertEqual(base64.b64decode(program["source"]["utf8_base64"], validate=True), data)
        source = json.loads(data)
        digest = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode()).hexdigest()
        sites = {row["site"]["pointer"]: row for row in program["sites"]}
        self.assertEqual(len(sites), len(program["sites"]))
        source_sites = {pointer(path): value for path,value in data_nodes(source) if type(value) is dict and any(k in value for k in ("command","expression","value")) and ("command" in value or "expression" in value or type(value.get("value")) is str)}
        self.assertEqual(set(sites),set(source_sites))
        decode = wire_decoder(program)
        interfaces = []
        for path,row in sites.items():
            self.assertEqual(row["site"]["source_sha256"],digest)
            if row["frame"] is not None:
                if row["frame"]["kind"] == "ScopeExit":
                    self.assertEqual(row["frame"]["backend_return_contract"],CONTRACT if row["node_kind"] == "command:run" else None)
                else: self.assertNotIn("backend_return_contract",row["frame"])
            if row["node_kind"] != "command:fork": continue
            raw = source_sites[path]
            generation = row["generation"]
            self.assertEqual(generation["kind"],"fork")
            self.assertEqual(row["frame"]["kind"],"GenBind")
            self.assertEqual([n["sort"] for n in generation["nominals"]],["Job","Task","Invocation","Region"])
            for index,nominal in enumerate(generation["nominals"]):
                self.assertEqual(nominal,{"site":row["site"],"role":"fork","index":str(index),"sort":["Job","Task","Invocation","Region"][index]})
            interface = decode(generation["interface"])
            self.assertEqual(interface["binder"],"generation-fork")
            self.assertEqual(interface["ordered_sorts"],["Job","Task","Invocation","Region"])
            self.assertEqual([f["tag"] for f in interface["facts"]],["job","result","jobRegion","bodyRegion","outlives"])
            self.assertEqual(interface["facts"][3]["fields"]["role"],"ordinary")
            self.assertEqual(interface["body"],ty("Unit"))
            self.assertEqual(row["usage"]["move"],[])
            self.assertEqual(len(row["capture"]),len(raw["job"]["captures"]))
            self.assertEqual(len(row["bindings"]),len(raw["job"]["captures"])+2)
            self.assertEqual([x["source"] for x in row["capture"]],row["usage"]["copy"])
            for index,capture in enumerate(row["capture"]):
                self.assertEqual(capture["binder"],{"site":row["site"],"role":"capture","index":str(index)})
                self.assertEqual(capture["use"],"copy")
                binding=next(b for b in row["bindings"] if b["binder"] == capture["binder"])
                self.assertEqual(binding["name"],raw["job"]["captures"][index])
                self.assertEqual(binding["type"],capture["type"])
            shared=next(b for b in row["bindings"] if b["binder"]["role"] == "generated" and b["binder"]["index"] == "0")
            control=next(b for b in row["bindings"] if b["binder"]["role"] == "generated" and b["binder"]["index"] == "1")
            shared_type,control_type=decode(shared["type"]),decode(control["type"])
            self.assertEqual(shared_type["tag"],"Shared")
            self.assertEqual(control_type["tag"],"Control")
            self.assertEqual(shared_type["fields"]["job"],{"nominal":generation["nominals"][0]})
            self.assertEqual(control_type["fields"]["job"],shared_type["fields"]["job"])
            self.assertEqual(shared_type["fields"]["payload"],decode(sites[path+"/job/body"]["type"]))
            self.assertEqual([x["kind"] for x in generation["success_bindings"]],["Shared","Control"])
            self.assertEqual([x["binder"] for x in generation["success_bindings"]],[shared["binder"],control["binder"]])
            interfaces.append({"site":path,"generation":generation,"interface":interface,"shared_type":shared_type,"captures":row["capture"],"frame":row["frame"],"complete_effect":decode(row["effect"])})
        self.records.observe(case, full_response_file="response.stdout.json", fork_interfaces=interfaces,
            site_count=len(sites), static_value_count=len(program["static_values"]), canonical_sha256=digest)
        return response

    def cases(self, *names):
        results=[]
        for name in names:
            with self.subTest(case=name): results.append(self.exchange(name))
            if self.records.abort_reason is not None: break
        return results

    def test_targets(self):
        program=self.exchange("targets")["program"]
        self.assertEqual(sum(x["node_kind"] == "command:fork" for x in program["sites"]),2)
        self.assertEqual(sum(x["node_kind"] == "command:produce" for x in program["sites"]),0)
    def test_full_stable_result(self):
        program=self.exchange("full_stable")["program"];decode=wire_decoder(program)
        full=next(row for row in program["sites"] if row["node_kind"] == "command:fork" and any(b["name"] == "FullH" for b in row["bindings"]))
        typ=decode(next(b["type"] for b in full["bindings"] if b["name"] == "FullH"))["fields"]["payload"]
        fn=typ["fields"]["left"];package=typ["fields"]["right"]
        self.assertEqual(fn["tag"],"Fn");self.assertEqual(fn["fields"]["kappa"]["tag"],"true")
        self.assertEqual(package["binder"],"exists");self.assertEqual(package["facts"][0]["tag"],"result")
        self.assertIn("command:call",[x["node_kind"] for x in program["sites"]])
        self.assertIn("command:unpack",[x["node_kind"] for x in program["sites"]])
    def test_compound_scope_capture(self): self.exchange("compound_scope")
    def test_hidden_scope_interface(self): self.cases("hidden_scope_strong","hidden_scope_weak")
    def test_shorter_scope(self): self.exchange("shorter_scope")
    def test_hidden_read(self): self.cases("read_direct","read_closure","read_exists")
    def test_affine_and_frame(self):
        self.exchange("affine_capture")
        program=self.exchange("affine_frame")["program"]
        fork=next(x for x in program["sites"] if x["node_kind"] == "command:fork")
        self.assertEqual(len(fork["frame"]["retained"]),1);self.assertEqual(fork["capture"],[])
    def test_caller_kappa(self): self.cases("kappa_hold","kappa_call")
    def test_exact_captures(self): self.cases("missing_capture","duplicate_capture")
    def test_freshness_escape(self): self.cases("fresh_reuse","bare_shared")
    def test_full_effect(self): self.exchange("missing_effect")
    def test_bottom_paths(self):
        self.exchange("bottom_child")
        program=self.exchange("bottom_continuation")["program"]
        row=next(x for x in program["sites"] if x["node_kind"] == "command:fork")
        self.assertEqual(row["execution"],"unreachable-continuation")
    def test_actual_waiter_source(self):
        program=self.exchange("normal")["program"]
        source=json.loads(self.records.cases["normal"]["data"])
        rows=[value for _,value in data_nodes(source) if type(value) is dict]
        commands=[value for value in rows if "command" in value]
        self.assertEqual(sum(x["command"] == "produce" for x in commands),2)
        self.assertEqual(sum(x["command"] == "load" for x in commands),2)
        self.assertEqual(sum(x["command"] == "await_cancel" for x in commands),4)
        self.assertEqual(sum(x["command"] == "request_cancel" for x in commands),2)
        self.assertEqual(sum(x["command"] == "await" for x in commands),1)
        b=next(x for x in commands if x["command"] == "produce" and x["kernel"] == "Prefill8")
        tokens=[];tail=b["argument"]
        while tail["value"] == "pair": tokens.append(tail["left"]);tail=tail["right"]
        tokens.append(tail)
        self.assertEqual(tokens,[{"value":"word","literal":x} for x in self.records.builder.m.B_PREFIX7]+[{"value":"var","name":"aToken"}])
        w=next(x for x in commands if x["command"] == "fork")
        self.assertEqual(w["job"]["result"],self.records.builder.WTYPE)
        self.assertEqual(w["job"]["captures"],["AH"])
        self.assertEqual(w["job"]["body"]["next"]["next"]["value"]["right"],{"value":"word","literal":7001})
        self.records.observe("normal", trusted_policy_sites=self.records.builder.source_sites(source), b_arguments=tokens,
            unique_N_D_source=True, waiter_complete_result=w["job"]["result"])
    def test_lease_write_boundary(self): self.cases("open_lease","open_write")


class Result(unittest.TextTestResult):
    records = None
    def startTest(self, test):
        self.records.methods.append({"id": test.id(), "state": "started"})
        save(self.records.output / "methods.json", self.records.methods)
        super().startTest(test)
    def finish_record(self, state, detail=None):
        self.records.methods[-1].update({"state": state, "detail": detail})
        save(self.records.output / "methods.json", self.records.methods)
    def addSuccess(self, test):
        self.finish_record("passed")
        super().addSuccess(test)
    def addFailure(self, test, err):
        self.finish_record("failed", self._exc_info_to_string(err, test))
        super().addFailure(test, err)
    def addError(self, test, err):
        self.finish_record("error", self._exc_info_to_string(err, test))
        if self.records.abort_reason is not None: self.stop()
        super().addError(test, err)
    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.finish_record("failed", self._exc_info_to_string(err, test))
            if self.records.abort_reason is not None: self.stop()
        super().addSubTest(test, subtest, err)


def main():
    global METHOD_CASES, INPUT_CASES
    deadline = time.monotonic() + 55
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    # Resolve only this frozen tree. INPUT_ORIGIN in the generator is metadata;
    # neither that historical file nor a working checkout is imported/read.
    module_spec = importlib.util.spec_from_file_location("cm2_source_data", GENERATOR)
    builder = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(builder)
    # Only export identities migrate. Preserve all original source bytes,
    # semantic diagnostics, method populations and assertion bodies.
    builder.EXPECTATIONS = {name: ({**value, "profile": "src1-response-offer-owner1-v1"}
                           if value["status"] == "ready" else value)
                           for name, value in builder.EXPECTATIONS.items()}
    METHOD_CASES = builder.METHOD_CASES
    INPUT_CASES = tuple((method,case) for method,cases in METHOD_CASES for case in cases)
    files = sorted((ROOT / "frontend").rglob("*.py")) + [GENERATOR, ROOT / "programs/build_cm1_programs.py", ROOT / "programs/a2_constructors.py", ROOT / "static-controls/check_open.py", Path(__file__).resolve()]
    origins = [pin(p) for p in files]
    save(output / "inputs.json", {"schema": "cm2-static-inputs-v1", "root": str(ROOT),
        "python": pin(Path(sys.executable).resolve()), "python_version": sys.version,
        "files": origins, "methods": [{"method": m, "case": c} for m, c in INPUT_CASES],
        "outer_budget_seconds": 60, "internal_work_budget_seconds": 55,
        "candidate_runtime_or_worker_started": False})
    records = Records(output, builder, deadline)
    CM2StaticTests.records, Result.records = records, records
    suite = unittest.TestSuite(CM2StaticTests(method) for method, _ in METHOD_CASES)
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    after = [pin(p) for p in files]
    unchanged = after == origins
    summary = {"schema": "cm2-static-controls-v1", "planned_methods": len(METHOD_CASES),
        "planned_cases": len(INPUT_CASES), "abort_reason": records.abort_reason,
        "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "input_files_unchanged": unchanged, "after": after,
        "passed": result.wasSuccessful() and unchanged,
        "runtime_started": False, "worker_started": False, "gpu_submissions": 0}
    save(output / "summary.json", summary)
    print(json.dumps(summary))
    return 0 if summary["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
