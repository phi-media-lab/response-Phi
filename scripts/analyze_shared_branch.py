#!/usr/bin/env python3
"""Independent replay of tiny-shared-branch JSON; never imports the harness/model.

Output tokens are checked against separately recorded isolated executions, not
recomputed by loading a model. Page snapshots lack event indices: their fixed
labels bind them to declared protocol boundaries, not exact host timestamps.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path

PAGE, SUFFIX, INITIAL, EXTRA = 128, 120, 4, 8
CHECKS = {"trial_succeeded", "all_tokens_match_isolated_same_path", "fixed_forward_input_and_head_work",
          "every_layer_cache_pool_refcount_consistent", "all_owners_and_pages_finally_closed",
          "loser_release_preserves_frozen_prefix", "winner_reuses_loser_private_page_every_layer",
          "no_loser_forward_after_stop"}
HELPER_SHA = "cf2cb10f68165a7caa54f340f755e8828cbefb45e6bfcb4f92891546776c9201"
HELPER_PROTOCOL_SHA = "cc1db8957f019869d6f591af61e55b58434d76e50a0e116fdd5dbf91df6528ee"


def need(ok, message):
    if not ok:
        raise ValueError(message)


def exact(a, b):
    if type(a) is not type(b): return False
    if isinstance(b, dict): return a.keys() == b.keys() and all(exact(a[k], v) for k, v in b.items())
    if isinstance(b, list): return len(a) == len(b) and all(exact(x, y) for x, y in zip(a, b))
    return a == b


def integer(x, low=0, high=None):
    return type(x) is int and x >= low and (high is None or x <= high)


def finite(x):
    return type(x) in (float, int) and math.isfinite(x)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""): h.update(block)
    return h.hexdigest()


def events_checked(events):
    need(isinstance(events, list) and bool(events), "missing events")
    previous = -1
    for event in events:
        need(isinstance(event, dict) and set(event) == {"name", "request_id", "t_ms", "details"}, "event schema differs")
        need(isinstance(event["name"], str) and isinstance(event["request_id"], str) and isinstance(event["details"], dict), "invalid event identity/details")
        need(finite(event["t_ms"]) and event["t_ms"] >= previous, "nonfinite/nonmonotonic event")
        previous = event["t_ms"]
    return events


def token_list(value, length=None):
    return isinstance(value, list) and (length is None or len(value) == length) and all(integer(x) for x in value)


def check_inputs(inputs):
    need(token_list(inputs.get("prefix_token_ids"), PAGE), "invalid actual prefix input")
    suffix = inputs.get("suffix_token_ids", {})
    need(set(suffix) == {"A", "B"} and all(token_list(suffix[who], SUFFIX) for who in suffix), "invalid actual steering input")
    need(exact(inputs.get("selection_trace"), {"after_each_branch_outputs": INITIAL}), "selection input trace differs")
    need(inputs.get("eos_token_id") is None or integer(inputs["eos_token_id"]), "invalid EOS token")
    need(isinstance(inputs.get("decoded_prefix"), str) and set(inputs.get("decoded_suffixes", {})) == {"A", "B"}
         and all(isinstance(x, str) for x in inputs["decoded_suffixes"].values()), "missing decoded input evidence")


def check_reference(record, owner, inputs, layers):
    need(record.get("passed") is True and record.get("error") is None, "isolated reference failed")
    outputs = record["output_token_ids"]
    need(token_list(outputs, INITIAL + EXTRA), "isolated reference output count/type differs")
    events = events_checked(record["events"])
    need(len(events) == 29, "isolated reference must contain 13 complete calls and safe release")
    offset = 0
    for step in range(13):
        head = step != 0
        tokens = inputs["prefix_token_ids"] if step == 0 else inputs["suffix_token_ids"][owner] if step == 1 else [outputs[step-2]]
        kind = "prefix" if step == 0 else "suffix" if step == 1 else "decode"
        begin, end = events[2*step:2*step+2]
        need(begin["name"] == "reference.forward_started" and end["name"] == "reference.eval_returned"
             and begin["request_id"] == end["request_id"] == owner, "reference call ordering/owner differs")
        need(exact(begin["details"], dict(input_token_ids=tokens, input_tokens=len(tokens), offset=offset,
             head_graph_constructed=True, head_evaluated=head, kind=kind)), "reference inputs/head work differ")
        offset += len(tokens)
        need(exact(end["details"], dict(offset=offset, actual_offsets=[offset]*layers, head_evaluated=head)), "reference offset/completion differs")
    need([e["name"] for e in events[-3:]] == ["reference.synchronize_started", "reference.synchronize_completed", "reference.cache_released"]
         and all(e["request_id"] == owner and e["details"] == {} for e in events[-3:]), "reference release lacks successful synchronization")
    pool = record["final_pool"]
    wanted = dict(used_pages=0, free_pages=3*layers, allocated_page_ids=3*layers,
                  backing_capacity_pages=4*layers, backing_bytes=pool.get("backing_bytes"), conserved=True)
    need(exact(pool, wanted) and integer(pool["backing_bytes"], 4*layers) and pool["backing_bytes"] % (4*layers) == 0, "reference pool not safely conserved")
    return dict(output_token_ids=outputs, forward_calls=13, model_input_tokens=259, evaluated_heads=12,
                backing_bytes=pool["backing_bytes"])


class Replay:
    """Sequential proof state reconstructed from raw events, not summary flags."""
    def __init__(self, selected, inputs, references, layers, backing_bytes):
        self.selected, self.loser = selected, "B" if selected == "A" else "A"
        self.inputs, self.references, self.layers = inputs, references, layers
        self.page_bytes = backing_bytes // (4*layers)
        self.owners, self.caches, self.pages, self.generations = {}, {}, {}, {}
        self.pools = [dict(used=set(), free=[], allocated=0, reused=0, capacity=0, growths=0) for _ in range(layers)]
        self.pending = self.delivery = self.must_free = self.closing = None
        self.close_order, self.closed = ["prefix", self.loser, selected], []
        self.sync_index, self.sync_pending, self.sync_credit = 0, None, None
        self.fork_child, self.fork_layers, self.forked = None, set(), []
        self.selection, self.started, self.work_completed, self.winner_completed, self.drained = 0, False, False, False, False
        self.outputs = {"A": [], "B": []}
        self.snapshots, self.anchors, self.calls, self.input_count, self.heads = [], {}, 0, 0, 0
        self.loser_freed, self.winner_reused = set(), set()

    def empty_cache(self):
        return [dict(offset=0, page_ids=[], page_lens=[], page_generations=[]) for _ in range(self.layers)]

    def ledger_state(self):
        return dict(owners=dict(self.owners), pending=None if self.pending is None else [self.pending["owner"], self.pending["step"]],
                    pages=[dict(layer=layer, page_id=page, allocation_generation=row["generation"], frozen=row["frozen"],
                                owners=sorted(row["owners"]), refcount=len(row["owners"])) for (layer, page), row in sorted(self.pages.items())])

    def capture(self, label):
        need(label not in {s["label"] for s in self.snapshots}, "duplicate snapshot anchor")
        need(self.pending is None and self.delivery is None and self.must_free is None, "snapshot anchor before complete work")
        pools = [dict(layer=layer, used_page_ids=sorted(p["used"]), free_page_ids=list(p["free"]), allocated_page_ids=p["allocated"],
                      backing_capacity_pages=p["capacity"], backing_bytes=p["capacity"]*self.page_bytes,
                      storage_growths=p["growths"], copied_bytes_on_growth=0, reused_page_allocations=p["reused"])
                 for layer, p in enumerate(self.pools)]
        self.snapshots.append(copy.deepcopy(dict(label=label, pools=pools, ledger=self.ledger_state(), caches=self.caches, consistent=True)))

    def identity(self, details):
        layer, page, generation = (details.get(k) for k in ("layer", "page_id", "allocation_generation"))
        need(integer(layer, 0, self.layers-1) and integer(page) and integer(generation, 1), "invalid page identity")
        return (layer, page), generation

    def schedule(self, step):
        need(0 <= step < 17, "extra forward call")
        if step == 0: return "prefix", "prefix", self.inputs["prefix_token_ids"], False
        if step <= 2:
            owner = "A" if step == 1 else "B"
            return owner, "suffix", self.inputs["suffix_token_ids"][owner], True
        owner = ("A" if step % 2 else "B") if step <= 8 else self.selected
        need(bool(self.outputs[owner]), "decode has no preceding actual output")
        return owner, "decode-before-selection" if step <= 8 else "decode-after-selection", [self.outputs[owner][-1]], True

    def idle(self):
        return self.pending is None and self.delivery is None and self.must_free is None and self.closing is None and self.sync_pending is None

    def event(self, e):
        name, who, d = e["name"], e["request_id"], e["details"]
        need(not self.drained, "events after final drain")
        if self.must_free is not None: need(name == "page.slot_freed", "zero reference not followed by actual slot free")
        if name == "work.started":
            need(not self.started and who == "system" and exact(d, {"selected": self.selected}), "bad workload start")
            self.started = True; self.owners["prefix"] = "active"; self.caches["prefix"] = self.empty_cache()
        elif name == "model.forward_started":
            need(self.started and self.idle() and self.owners.get(who) == "active", "new forward lacks idle active owner")
            owner, kind, tokens, head = self.schedule(self.calls)
            offset = self.caches[who][0]["offset"]
            need(who == owner and exact(d, dict(step_id=self.calls, input_token_ids=tokens, input_tokens=len(tokens), offset=offset, kind=kind)), "forward differs from fixed real input schedule")
            need(all(c["offset"] == offset for c in self.caches[who]), "layers disagree before forward")
            if self.calls > 0: need(self.closed[:1] == ["prefix"] and self.forked == ["A", "B"], "branch forward before parent transfer")
            if self.calls >= 9: need(self.selection == 3 and self.loser in self.closed, "winner continuation before loser closes")
            self.pending = dict(owner=who, step=self.calls, offset=offset, tokens=len(tokens), head=head, stage="graph", writes=set())
        elif name == "page.allocated":
            key, generation = self.identity(d); layer, page = key; p = self.pools[layer]
            need(self.pending is not None and self.pending["owner"] == who and self.pending["stage"] == "graph"
                 and self.owners.get(who) == "active" and key not in self.pages, "allocation without unique live construction")
            reused = bool(p["free"])
            expected_page = p["free"].pop() if reused else p["allocated"]
            need(page == expected_page, "actual allocator/free-list page differs")
            if reused: p["reused"] += 1
            else: p["allocated"] += 1
            need(generation == self.generations.get(key, 0)+1 and exact(d, dict(layer=layer, page_id=page, allocation_generation=generation, refcount=1, reused=reused)), "allocation generation/ref/reused claim differs")
            self.generations[key] = generation; p["used"].add(page)
            self.pages[key] = dict(generation=generation, frozen=False, owners={who})
            if reused and who == self.selected: self.winner_reused.add((layer, page, generation-1))
        elif name == "cache.write_constructed":
            key, generation = self.identity(d); layer, page = key
            row, pending = self.pages.get(key), self.pending
            need(row is not None and row["generation"] == generation and row["owners"] == {who} and row["frozen"] is False,
                 "write to missing/stale/shared/frozen page")
            need(pending is not None and pending["owner"] == who and pending["stage"] == "graph" and self.owners.get(who) == "active" and layer not in pending["writes"], "write outside sole current layer call")
            cache = self.caches[who][layer]
            tail = bool(cache["page_ids"]) and cache["page_lens"][-1] < PAGE
            start = cache["page_lens"][-1] if tail else 0
            if tail: need(cache["page_ids"][-1] == page, "append bypassed existing private tail")
            else:
                need(page not in cache["page_ids"], "new full page repeats cache slot")
                cache["page_ids"].append(page); cache["page_lens"].append(0); cache["page_generations"].append(generation)
            need(exact(d, dict(layer=layer, page_id=page, allocation_generation=generation, start=start, tokens=pending["tokens"],
                 boundary="paged_cache_update graph constructed; actual completion is enclosing eval")) and start+pending["tokens"] <= PAGE, "write extent differs from actual model inputs")
            cache["page_lens"][-1] += pending["tokens"]; cache["offset"] += pending["tokens"]
            p = self.pools[layer]
            if p["capacity"] == 0: p["capacity"], p["growths"] = 4, 1
            need(p["allocated"] <= 4, "unexpected backing growth outside fixed schedule")
            pending["writes"].add(layer)
        elif name in ("model.forward_returned", "model.eval_started", "model.eval_returned"):
            p = self.pending
            need(p is not None and p["owner"] == who and d.get("step_id") == p["step"], "completion lacks matching pending call")
            if name == "model.forward_returned":
                need(p["stage"] == "graph" and p["writes"] == set(range(self.layers)) and exact(d, dict(step_id=p["step"], input_tokens=p["tokens"], head_graph_constructed=True, head_evaluated=p["head"])), "forward returned before every layer write")
                p["stage"] = "forward-returned"
            elif name == "model.eval_started":
                need(p["stage"] == "forward-returned" and exact(d, dict(step_id=p["step"], head_evaluated=p["head"])), "eval start skips construction boundary")
                p["stage"] = "eval"
            else:
                need(p["stage"] == "eval" and exact(d, dict(step_id=p["step"], head_evaluated=p["head"])), "eval completion lacks started work")
                self.pending = None; self.calls += 1; self.input_count += p["tokens"]; self.heads += int(p["head"])
                if p["head"]: self.delivery = p
                else: self.capture("after-step-"+str(p["step"]))
        elif name == "token.delivered":
            p = self.delivery
            need(p is not None and p["owner"] == who and self.pending is None, "token before corresponding eval return")
            index = len(self.outputs[who]); token = self.references[who][index]
            offset = p["offset"] + p["tokens"]
            need(exact(d, dict(step_id=p["step"], token_id=token, output_index=index, actual_offsets=[offset]*self.layers)), "delivered token/offset differs from isolated reference")
            self.outputs[who].append(token); self.delivery = None
            self.capture("after-step-"+str(p["step"]))
        elif name == "prefix.frozen":
            need(self.idle() and self.calls == 1 and who == "prefix" and self.owners.get(who) == "active" and exact(d, dict(tokens=PAGE, layers=self.layers)), "freeze before completed full prefix")
            need(len(self.pages) == self.layers and all(c["page_lens"] == [PAGE] and c["offset"] == PAGE for c in self.caches[who]), "freeze includes partial/missing prefix pages")
            for row in self.pages.values():
                need(row["owners"] == {"prefix"} and row["frozen"] is False, "freeze lacks sole original owner")
                row["frozen"] = True
            self.owners[who] = "frozen"; self.capture("prefix-frozen")
        elif name == "page.retained":
            need(self.idle() and self.owners.get("prefix") == "frozen" and who in ("A", "B"), "retain before safe freeze")
            if self.fork_child is None:
                need(who not in self.owners and who == ("A" if not self.forked else "B"), "nonfresh/out-of-order branch")
                self.fork_child, self.fork_layers = who, set()
                self.owners[who] = "active"; self.caches[who] = copy.deepcopy(self.caches["prefix"])
            need(self.fork_child == who, "interleaved partial fork")
            key, generation = self.identity(d); layer, page = key; row = self.pages.get(key)
            need(row is not None and row["generation"] == generation and row["frozen"] is True and "prefix" in row["owners"]
                 and who not in row["owners"] and layer not in self.fork_layers, "retain stale/duplicate/nonprefix reference")
            row["owners"].add(who); self.fork_layers.add(layer)
            need(exact(d, dict(layer=layer, page_id=page, allocation_generation=generation, refcount=len(row["owners"]), owners=sorted(row["owners"]))), "retain refcount/owners differs")
        elif name == "branch.forked":
            need(self.fork_child == who and self.fork_layers == set(range(self.layers)) and d == {"parent": "prefix"}, "fork omitted a layer reference")
            self.forked.append(who); self.fork_child = None; self.fork_layers = set(); self.capture("fork-"+who)
        elif name == "synchronize.started":
            scopes = ("parent-reference-transfer", "loser-close", "final-drain")
            need(self.idle() and who == "system" and self.sync_index < 3 and d.get("scope") == scopes[self.sync_index]
                 and d.get("stream") == "default GPU stream used by all model calls" and len(d) == 2, "unexpected synchronization boundary")
            if self.sync_index == 0: need(self.forked == ["A", "B"] and self.calls == 1, "parent transfer before complete forks")
            elif self.sync_index == 1: need(self.selection == 3 and self.calls == 9, "loser close before stop")
            else: need(self.work_completed, "final drain before complete fixed work")
            self.sync_pending = d["scope"]
        elif name == "synchronize.completed":
            need(self.sync_pending is not None and who == "system" and d == {"scope": self.sync_pending}, "synchronization completion without start")
            self.sync_credit = self.sync_pending; self.sync_pending = None; self.sync_index += 1
        elif name == "owner.close_started":
            need(self.idle() and len(self.closed) < 3 and who == self.close_order[len(self.closed)] and d == {}, "close lacks unique expected live owner")
            need(self.sync_credit == ("parent-reference-transfer", "loser-close", "final-drain")[len(self.closed)], "close without matching successful synchronization")
            need(self.owners.get(who) == {"prefix": "frozen", self.loser: "stopped", self.selected: "active"}[who], "close owner state differs")
            self.owners[who] = "closing"; self.closing = who; self.sync_credit = None
        elif name == "page.reference_released":
            key, generation = self.identity(d); layer, page = key; row = self.pages.get(key)
            need(self.pending is None and self.closing == who and row is not None and row["generation"] == generation and who in row["owners"], "release without observed close/current generation")
            cache = self.caches[who][layer]
            need(page in cache["page_ids"], "release absent from owner cache")
            index = cache["page_ids"].index(page)
            need(index == 0, "cache release changed within-layer order")
            count = cache["page_lens"].pop(index); cache["page_ids"].pop(index); cache["page_generations"].pop(index); cache["offset"] -= count
            row["owners"].remove(who)
            need(exact(d, dict(layer=layer, page_id=page, allocation_generation=generation, refcount=len(row["owners"]), owners=sorted(row["owners"]), frozen=row["frozen"])), "release reference summary differs")
            if not row["owners"]: self.must_free = (key, generation, who, row["frozen"])
        elif name == "page.slot_freed":
            key, generation = self.identity(d)
            need(self.must_free is not None and self.must_free[:3] == (key, generation, who), "physical slot freed before last valid reference")
            need(exact(d, dict(layer=key[0], page_id=key[1], allocation_generation=generation)), "slot free identity differs")
            if who == self.loser:
                need(self.must_free[3] is False, "loser freed shared frozen prefix")
                self.loser_freed.add((key[0], key[1], generation))
            del self.pages[key]; p = self.pools[key[0]]; p["used"].remove(key[1]); p["free"].append(key[1]); self.must_free = None
        elif name == "owner.closed":
            need(self.closing == who and d == {} and all(who not in row["owners"] for row in self.pages.values())
                 and exact(self.caches[who], self.empty_cache()), "owner close omitted references/cache metadata")
            self.owners[who] = "closed"; self.closing = None; self.closed.append(who)
            if who == "prefix": self.capture("parent-closed-two-branch-references")
            elif who == self.loser: self.capture("loser-closed-prefix-held-by-winner")
        elif name == "selection.requested":
            need(self.idle() and self.selection == 0 and self.calls == 9 and who == "controller" and all(len(x) == INITIAL for x in self.outputs.values()), "selection not at fully observed fixed boundary")
            need(exact(d, dict(selected=self.selected, loser=self.loser, after_each_branch_outputs=INITIAL,
                 actual_output_counts={"A": INITIAL, "B": INITIAL}, phase="between fully observed eval calls; no GPU in-flight action claim")), "selection trace differs")
            self.selection = 1
        elif name == "selection.accepted":
            need(self.idle() and self.selection == 1 and who == "controller" and d == {"selected": self.selected}, "selection acceptance before request")
            self.selection = 2
        elif name == "branch.submissions_stopped":
            need(self.idle() and self.selection == 2 and who == self.loser and self.owners.get(who) == "active" and d == {}, "stop lacks selected loser")
            self.owners[who] = "stopped"; self.selection = 3; self.capture("selection-before-loser-close")
        elif name == "winner.completed":
            need(self.idle() and self.calls == 17 and who == self.selected and exact(d, dict(output_tokens=12)) and not self.winner_completed, "winner completion before full work")
            self.winner_completed = True
        elif name == "work.completed":
            need(self.idle() and self.winner_completed and not self.work_completed and who == "system" and exact(d, dict(total_delivered_tokens=16)), "work completion differs")
            self.work_completed = True
        elif name == "trial.drained":
            need(self.idle() and self.closed == self.close_order and not self.pages and all(not p["used"] for p in self.pools)
                 and self.sync_index == 3 and who == "system" and exact(d, dict(recovery=False)), "drain lacks actual final cleanup")
            self.capture("final-drained"); self.drained = True
        else:
            raise ValueError("unsupported/failed-path event cannot establish successful trial: " + name)
        self.anchors.setdefault(name, []).append(e)


def analyze_trial(trial, selected, inputs, references, layers, backing_bytes):
    need(trial.get("passed") is True, "original trial failed")
    expected_config = dict(selected=selected, page_size=PAGE, prefix_tokens=PAGE, suffix_tokens_per_branch=SUFFIX,
                           initial_outputs_per_branch=INITIAL, winner_additional_decode_steps=EXTRA, fixed_length_ignores_eos=True)
    need(exact(trial["config"], expected_config), "fixed trial config differs")
    checks = trial["checks"]
    need(len(checks) == len(CHECKS) and {c["name"] for c in checks} == CHECKS and all(c.get("passed") is True for c in checks), "missing/failed original safety checks")
    need(next(c for c in checks if c["name"] == "trial_succeeded").get("error") is None, "trial error was hidden by passed flag")
    r = Replay(selected, inputs, references, layers, backing_bytes)
    for event in events_checked(trial["events"]): r.event(event)
    need(r.drained and (r.calls, r.input_count, r.heads) == (17, 382, 16), "fixed complete model work missing")
    need(len(r.loser_freed) == layers and r.winner_reused == r.loser_freed, "winner did not reuse exactly each loser's private slot in the next generation")
    observations = trial["observations"]
    need(observations.get("diagnostic_errors") == [], "diagnostic failures cannot establish successful final state")
    need(exact(observations["output_token_ids"], r.outputs), "output consumer list differs from actual deliveries")
    need(exact(observations["snapshots"], r.snapshots), "cache/pool/ref snapshot differs from replayed declared boundary")
    eos = {owner: [i for i, value in enumerate(tokens) if value == inputs.get("eos_token_id")] for owner, tokens in r.outputs.items()}
    need(exact(observations["eos_output_indices"], eos), "EOS handling differs from fixed work")
    for name, value in dict(forward_calls=17, model_input_tokens=382, constructed_head_graphs=17, evaluated_token_heads=16).items():
        need(exact(observations.get(name), value), "work counter differs: " + name)
    for name in ("hardware_parallelism_claimed", "inflight_selection_claimed", "physical_buffer_identity_proven", "logical_capacity_quota_imposed"):
        need(observations.get(name) is False, "unsupported execution/storage claim: " + name)
    def event(name, owner=None, index=None):
        found = [e for e in r.anchors[name] if (owner is None or e["request_id"] == owner) and (index is None or e["details"].get("output_index") == index)]
        need(len(found) == 1, "ambiguous metric anchor: " + name)
        return found[0]["t_ms"]
    requested = event("selection.requested")
    metrics = dict(selection_ack_ms=event("selection.accepted")-requested, loser_release_ms=event("owner.closed", r.loser)-requested,
                   winner_next_token_ms=event("token.delivered", selected, INITIAL)-requested,
                   work_ms=event("work.completed")-event("work.started"), through_drain_ms=event("trial.drained")-event("work.started"))
    need(set(trial["metrics"]) == set(metrics) and all(finite(trial["metrics"][k]) and math.isclose(trial["metrics"][k], v, abs_tol=1e-7, rel_tol=1e-9) for k, v in metrics.items()), "metrics differ from real event anchors")
    return dict(passed=True, trial_id=trial["trial_id"], selected=selected, forward_calls=r.calls, model_input_tokens=r.input_count,
                constructed_heads=r.calls, evaluated_heads=r.heads, output_token_ids=r.outputs, snapshots=len(r.snapshots),
                loser_private_slots_reused=layers, final_owners=r.owners, final_refs=0, final_used_slots=0, metrics=metrics,
                snapshot_scope="Fixed labels bind protocol boundary states; no exact snapshot timestamp/event index is available")


def analyze_document(document):
    need(type(document.get("schema_version")) is int and document.get("schema_version") == 1 and document.get("experiment") == "tiny-shared-branch" and document.get("passed") is True
         and not document.get("error"), "wrong/failed document; incomplete or quarantine evidence is not a pass")
    config = document["config"]
    need(config.get("selected") in ("A", "B") and integer(config.get("trials"), 1, 20) and integer(config.get("warmup"), 0, 5), "invalid bounded run config")
    setup = document["setup"]
    need(not setup.get("error") and not setup.get("cleanup_error"), "model setup failed")
    events = events_checked(setup["events"])
    need([e["name"] for e in events] == ["model.load_started", "model.load_returned", "model.constructed"], "model setup chain differs")
    need(all(e["request_id"] == "system" for e in events) and events[0]["details"] == events[1]["details"] == {}, "setup identity/details differ")
    layers = events[-1]["details"].get("layers")
    need(integer(layers, 1, 128) and exact(events[-1]["details"], dict(layers=layers, page_size=PAGE)), "invalid model layer/page geometry")
    inputs = document["inputs"]; check_inputs(inputs)
    need(set(document["reference"]) == {"A", "B"}, "missing isolated branch reference")
    refs = {owner: check_reference(document["reference"][owner], owner, inputs, layers) for owner in ("A", "B")}
    need(refs["A"]["backing_bytes"] == refs["B"]["backing_bytes"], "isolated model pool geometry differs")
    references = {owner: row["output_token_ids"] for owner, row in refs.items()}
    rows, warmups = [], []
    for group, count, output in (("trials", config["trials"], rows), ("warmups", config["warmup"], warmups)):
        need(len(document[group]) == count, "missing/extra " + group)
        for index, trial in enumerate(document[group]):
            need(type(trial.get("trial_id")) is int and trial["trial_id"] == (index if group == "trials" else -(index+1)), "wrong/duplicate trial identity")
            output.append(analyze_trial(trial, config["selected"], inputs, references, layers, refs["A"]["backing_bytes"]))
    return dict(passed=True, total_trials=len(rows), warmups=warmups, trials=rows, layers=layers, reference=refs,
                numerical_scope="Actual delivered token IDs equal separately recorded isolated same-path outputs; no model import or independent whole-KV bitwise oracle")


def attempt_audit(attempt_dir, external_full_hashes=False):
    """Independently check saved bytes; do not execute snapshot verification code."""
    root = Path(attempt_dir)
    manifest_path, payload = root / "manifest.json", root / "payload"
    manifest = json.loads(manifest_path.read_text()); prepared = json.loads((root / "prepared.json").read_text())
    digest = sha(manifest_path)
    need(manifest.get("schema_version") == 1 and manifest.get("kind") == "tiny-shared-branch-attempt", "wrong attempt manifest")
    need(prepared.get("manifest_sha256") == digest and prepared.get("model_or_gpu_loaded") is False, "prepared snapshot identity differs")
    files = manifest["payload_files"]
    need(integer(prepared.get("payload_files"), 1) and prepared["payload_files"] == len(files), "prepared payload count differs")
    actual = {str(p.relative_to(payload)) for p in payload.rglob("*") if p.is_file()}
    need(actual == set(files), "snapshot payload inventory changed")
    required = {"experiments/tiny-shared-branch/"+name for name in ("run.py", "protocol.py", "attempt.py", "scenario.json", "README.md", "test_protocol.py", "test_attempt.py")}
    required |= {"experiments/tiny-interleave/run.py", "experiments/tiny-interleave/protocol.py", "scripts/run_recorded.py",
                 "tiny-source/src/tiny_llm_ref/qwen3_week3.py", "tiny-source/src/tiny_llm_ref/paged_kv_cache.py", "tiny-source/src/tiny_llm_ref/attention.py",
                 "tiny-source/src/extensions_ref/src/paged_attention.cpp", "tiny-source/src/extensions_ref/src/paged_attention.metal", "model-metadata/config.json"}
    need(required <= set(files) and any(name.startswith("tiny-source/src/extensions_ref/tiny_llm_ext_ref/_ext.") and name.endswith(".so") for name in files), "snapshot lacks required source/native/model metadata")
    for relative, row in files.items():
        path = payload / relative
        need(not Path(relative).is_absolute() and ".." not in Path(relative).parts and not path.is_symlink() and path.resolve().is_relative_to(payload.resolve()), "unsafe payload path")
        need(integer(row.get("bytes")) and path.stat().st_size == row["bytes"] and sha(path) == row.get("sha256"), "payload hash/size differs: " + relative)
    need(files["experiments/tiny-interleave/run.py"]["sha256"] == HELPER_SHA and files["experiments/tiny-interleave/protocol.py"]["sha256"] == HELPER_PROTOCOL_SHA, "frozen helper hash differs")
    external = manifest["external_dependencies"]
    need(isinstance(external, list) and bool(external) and len({x["path"] for x in external}) == len(external), "missing/duplicate external dependencies")
    runtime_files = set(manifest["runtime"]["files"])
    need({x["path"] for x in external if x["kind"] == "runtime"} == runtime_files, "runtime inventory differs")
    for row in external:
        need(row.get("kind") in ("runtime", "model-weight", "model-metadata") and integer(row.get("bytes"))
             and isinstance(row.get("sha256"), str) and len(row["sha256"]) == 64 and all(c in "0123456789abcdef" for c in row["sha256"]), "invalid external identity")
        if row["kind"] != "model-weight": need(row.get("preparation_hash_verified") is True, "metadata/runtime preparation hash missing")
        if row["kind"] == "model-metadata":
            name = "model-metadata/"+Path(row["path"]).name
            need(name in files and files[name]["sha256"] == row["sha256"] and files[name]["bytes"] == row["bytes"], "copied model metadata differs from external declaration")
        if external_full_hashes:
            path = Path(row["path"])
            need(path.stat().st_size == row["bytes"] and sha(path) == row["sha256"], "external full bytes differ: " + str(path))
    weights = [x for x in external if x["kind"] == "model-weight"]
    need(bool(weights), "no model weight identity")
    if external_full_hashes:
        actual_model = {str(p) for p in Path(manifest["run_spec"]["model"]).iterdir() if p.is_file()}
        need(actual_model == {x["path"] for x in external if x["kind"].startswith("model-")}, "actual cached model file set changed")
    return manifest, dict(passed=True, manifest_sha256=digest, payload_files=len(files), external_dependencies=len(external),
                          payload_full_hashes_verified=True, external_full_hashes_verified=external_full_hashes,
                          external_bytes=sum(x["bytes"] for x in external), model_weights=[dict(path=x["path"], sha256=x["sha256"], bytes=x["bytes"]) for x in weights])


def artifact_audit(path, attempt_dir=None, external_full_hashes=False):
    path = Path(path); root = Path(attempt_dir) if attempt_dir else path.parent.parent
    document = json.loads(path.read_text())
    manifest, result = attempt_audit(root, external_full_hashes)
    snapshot, config, provenance = document["snapshot"], document["config"], document["provenance"]
    original_manifest = Path(snapshot["manifest_path"]); original_payload = original_manifest.parent / "payload"
    files, external = manifest["payload_files"], {x["path"]: x for x in manifest["external_dependencies"]}
    need(snapshot.get("manifest_sha256") == config.get("snapshot_sha256") == result["manifest_sha256"]
         and config.get("attempt_manifest") == str(original_manifest), "raw snapshot/config identity differs")
    need(snapshot.get("pre_execution_full_hashes_verified") is True and snapshot.get("post_execution_full_hashes_verified") is True
         and not snapshot.get("verification_error"), "actual run did not preserve before/after source and dependency verification")
    need(exact(snapshot.get("payload_files"), result["payload_files"]) and exact(snapshot.get("external_dependencies"), result["external_dependencies"])
         and snapshot.get("reference_origin") == manifest["reference_origin"], "raw snapshot inventory/origin differs")
    spec = manifest["run_spec"]
    need(all(exact(config.get(k), spec[k]) for k in ("selected", "trials", "warmup", "model")), "frozen run specification differs")
    need(config.get("tiny_root") == str(original_payload / "tiny-source") and config.get("output") is None and config.get("json") is True,
         "runner escaped frozen source path or recorded output channel")
    expected_keys = {"selected", "trials", "warmup", "model", "tiny_root", "output", "json", "attempt_manifest", "snapshot_sha256"}
    need(set(config) == expected_keys, "unrecognized runtime configuration")
    bindings = {"harness_sha256": "experiments/tiny-shared-branch/run.py", "protocol_sha256": "experiments/tiny-shared-branch/protocol.py",
                "helper_sha256": "experiments/tiny-interleave/run.py", "helper_protocol_sha256": "experiments/tiny-interleave/protocol.py"}
    for field, relative in bindings.items(): need(provenance.get(field) == files[relative]["sha256"], "harness/helper provenance differs: " + field)
    origin = manifest["reference_origin"]
    need(provenance.get("tiny_llm_original_root") == origin["root"] and provenance.get("tiny_llm_commit") == origin["commit"]
         and provenance.get("tiny_llm_dirty_status") == origin["dirty_status"] and provenance.get("tiny_llm_root") == config["tiny_root"], "tiny origin/import provenance differs")
    runtime = manifest["runtime"]
    need(provenance.get("python") == runtime["version"] and provenance.get("python_executable") == runtime["executable"]
         and provenance.get("packages") == runtime["packages"], "runtime provenance differs")
    need(exact(provenance.get("per_trial_work"), dict(forward_calls=17, constructed_output_head_graphs=17, evaluated_token_heads=16, model_input_tokens=382)), "provenance work contract differs")
    need(provenance.get("model_path") == spec["model"] and provenance.get("model_snapshot") == Path(spec["model"]).name, "model snapshot path differs")
    wanted_metadata = {Path(k).name: v["sha256"] for k, v in files.items() if k.startswith("model-metadata/") and k.endswith(".json")}
    need(provenance.get("model_metadata_hashes") == wanted_metadata, "model metadata provenance differs")
    wanted_weights = [dict(name=Path(x["path"]).name, bytes=x["bytes"], resolved_path=x["resolved_path"]) for x in external.values() if x["kind"] == "model-weight"]
    need(exact(provenance.get("model_weight_files"), wanted_weights), "weight file provenance differs")
    reference_hashes = {k.removeprefix("tiny-source/"): v["sha256"] for k, v in files.items()
                        if Path(k).parent == Path("tiny-source/src/tiny_llm_ref") and k.endswith(".py")}
    need(provenance.get("reference_source_hashes") == reference_hashes, "reference Python provenance differs")
    imported = snapshot.get("imported_files", {})
    need({"mlx.core", "mlx_lm", "tiny_llm_ref.qwen3_week3", "tiny_llm_ref.paged_kv_cache"} <= set(imported)
         and any(name.endswith("._ext") for name in imported), "missing actual Python/native import identities")
    for name, row in imported.items():
        address = Path(row["path"])
        if name in ("mlx.core", "mlx_lm"):
            need(row["path"] in external and row["sha256"] == external[row["path"]]["sha256"], "runtime import escaped pinned environment")
        else:
            need(name == "model_names" or name.startswith(("tiny_llm_ref", "extensions_ref")), "unexpected import binding")
            need(address.is_relative_to(original_payload / "tiny-source"), "reference import escaped captured tiny source")
            relative = str(address.relative_to(original_payload))
            need(relative in files and row["sha256"] == files[relative]["sha256"], "imported source/native bytes differ")
    imported_provenance = {name: row for name, row in imported.items() if name.startswith(("tiny_llm_ref", "extensions_ref"))}
    need(provenance.get("imported_modules") == imported_provenance, "actual imported module lists differ")
    for address, digest in provenance.get("additional_reference_hashes", {}).items():
        p = Path(address)
        need(p.is_relative_to(original_payload / "tiny-source") and files[str(p.relative_to(original_payload))]["sha256"] == digest, "additional reference hash escaped snapshot")
    need(len(provenance.get("additional_reference_hashes", {})) == 4, "missing additional branch/native source identities")
    scenario_path = root / "payload/experiments/tiny-shared-branch/scenario.json"
    need(document["inputs"].get("fixture_sha256") == sha(scenario_path) and document["inputs"].get("fixture") == json.loads(scenario_path.read_text()), "actual input fixture differs from captured text")
    model = json.loads((root / "payload/model-metadata/config.json").read_text())
    layers = document["setup"]["events"][-1]["details"]["layers"]
    need(layers == model["num_hidden_layers"] and document["inputs"]["eos_token_id"] == model["eos_token_id"], "model geometry/EOS differs from bound metadata")
    all_tokens = document["inputs"]["prefix_token_ids"] + sum(document["inputs"]["suffix_token_ids"].values(), [])
    all_tokens += sum((r["output_token_ids"] for r in document["reference"].values()), [])
    need(all(integer(token, 0, model["vocab_size"]-1) for token in all_tokens), "token ID outside bound vocabulary")
    bytes_per_element = document["reference"]["A"]["final_pool"]["backing_bytes"] / (layers * 4 * 2 * PAGE * model["num_key_value_heads"] * model["head_dim"])
    need(bytes_per_element in (2, 4), "actual KV backing bytes incompatible with supported page shape")
    run_path = path.parent / "run.json"; run = json.loads(run_path.read_text())
    need(path.name == "stdout.json" and type(run.get("returncode")) is int and run["returncode"] == 0 and run.get("timed_out") is False, "recorded model process failed/timed out")
    need(finite(run.get("elapsed_seconds")) and run["elapsed_seconds"] >= 0 and finite(run.get("timeout_seconds")) and run["timeout_seconds"] > 0, "invalid recorded process timing")
    expected_command = [runtime["executable"], str(original_payload / "experiments/tiny-shared-branch/run.py"), "--selected", spec["selected"],
                        "--trials", str(spec["trials"]), "--warmup", str(spec["warmup"]), "--model", spec["model"], "--tiny-root", str(original_payload / "tiny-source"),
                        "--attempt-manifest", str(original_manifest), "--snapshot-sha256", result["manifest_sha256"], "--json"]
    need(run.get("command") == expected_command and run.get("cwd") == str(original_payload / "experiments/tiny-shared-branch"), "actual process command/cwd differs from frozen specification")
    need(set(run.get("files", {})) == {"stdout.json", "stderr.log"}, "recording missing stdout/stderr")
    for name in ("stdout.json", "stderr.log"):
        actual = path.parent / name
        need(exact(run["files"][name], dict(bytes=actual.stat().st_size, sha256=sha(actual))), "recorded log bytes/hash differs: " + name)
    return dict(result, run_sha256=sha(run_path), stdout_sha256=sha(path), stderr_sha256=sha(path.parent / "stderr.log"),
                imported_files=len(imported), inferred_kv_element_bytes=int(bytes_per_element),
                external_evidence="Independent full bytes checked now" if external_full_hashes else "Manifest binding plus before/after execution attestations; external bytes not independently reread by this invocation",
                input_scope="Actual IDs and fixture bytes bound; tokenizer encoding not independently rerun")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+"); parser.add_argument("--output", type=Path)
    parser.add_argument("--attempt-dir", type=Path); parser.add_argument("--external-full-hashes", action="store_true")
    args = parser.parse_args(); rows = []
    for path in args.inputs:
        try:
            document = json.loads(path.read_text())
            result = analyze_document(document)
            result["artifacts"] = artifact_audit(path, args.attempt_dir, args.external_full_hashes)
        except (ValueError, KeyError, TypeError, IndexError, OSError) as error:
            result = dict(passed=False, total_trials=0, errors=[str(error)])
        rows.append(dict(result, source=str(path), raw_sha256=sha(path) if path.is_file() else None))
    result = dict(passed=all(row["passed"] for row in rows), total_trials=sum(row["total_trials"] for row in rows), documents=rows,
                  external_full_hashes_requested=args.external_full_hashes,
                  external_full_hashes_verified=bool(rows) and all(row.get("artifacts", {}).get("external_full_hashes_verified") is True for row in rows),
                  scope="Independent event/numeric and immutable attempt/actual import/CLI/hash audit. External full byte hashing is explicit; no model/GPU execution or speed ranking. Hashes provide consistency, not signed authenticity.")
    encoded = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)+"\n"
    if args.output:
        with args.output.open("x") as output: output.write(encoded)
    else: print(encoded, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
