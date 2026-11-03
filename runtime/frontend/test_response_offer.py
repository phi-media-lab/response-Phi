#!/usr/bin/env python3
"""SRC1 A source/static controls; root owns their first actual invocation.

No worker, Swift runtime, request control, model or GPU is exercised here.
Acceptance of intent syntax is deliberately not proof of dynamic eligibility.
"""
import importlib.util
from pathlib import Path
import sys
import unittest

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import test_external_request as prior
from p8source.entry import check_closed_main, check_open_fragment
from p8source.execution import prepare_execution
from p8source.sigma import fixed_sigma, wire_sigma
from p8source.binding import free_terms
from p8foundation.model import FoundationError

c2,m,g=prior.c2,prior.m,prior.g


def complete_builder():
    spec=importlib.util.spec_from_file_location("src1_test_complete_source",
        prior.HERE.parent/"programs"/"build_source_response_programs.py")
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    return builder


def offer(owner=None,token=None,port="B"):
    return c2.c("offer_response",name=port,
        owner=c2.var("owner") if owner is None else owner,
        token=c2.word(271) if token is None else token)


def response_effect(owner="O"):
    return c2.eff(c2.atom("HostResponse",owner=owner),c2.atom("Raise"))


def minimal(body=None,result=None,effect=None):
    return m.with_owner(offer() if body is None else body,
        response_effect() if effect is None else effect,
        c2.t("Unit") if result is None else result)


def in_run(body,body_effect,prefix="A"):
    names,facts=m.run_parts(prefix)
    run=c2.c("run",owner=c2.var("owner"),names=names,name=prefix+"Scope",body=body)
    eps=c2.union(c2.eff(c2.atom("Raise")),c2.nu(names,facts,c2.union(
        body_effect,c2.eff(c2.atom("Enter",scope=prefix+"S"),c2.atom("Drain",scope=prefix+"S"),c2.atom("Raise")))))
    return run,eps


def run_offer():
    run,eps=in_run(offer(),response_effect())
    return minimal(run,effect=eps)


def decoder_source_cases():
    """Pure inputs for later real decoder controls; no fabricated exchange.

    `minimal_offer` contains Unit/Word/Owner and HostResponse/Raise annotations
    at distinct source occurrences. It is statically valid without a received
    request; production preflight/dynamic checks must still reject that use.
    `receive_only` specifically permits testing the separate missing-intent
    runtime boundary rather than changing it into static rejection.
    """
    return {"minimal_offer":minimal(),"inside_run":run_offer(),
            "receive_only":g.source(),"legacy_no_external":c2.waiter_source()}


def decode_static(program,ref):
    """Read this frontend's exported DAG for correspondence assertions only."""
    value=program["static_values"][int(ref["ref"])]
    if value is None or isinstance(value,(str,bool)): return value
    if "integer" in value: return int(value["integer"])
    if "array" in value: return [decode_static(program,x) for x in value["array"]]
    if "object" in value: return {k:decode_static(program,v) for k,v in value["object"]}
    raise AssertionError("unexpected static-table encoding")


class ResponseOfferTests(unittest.TestCase):
    def accepted(self,raw):
        result=check_closed_main(fixed_sigma(),raw)
        self.assertEqual(result.verdict,"checked-closed-main-source",result.diagnostic)
        return result

    def rejected(self,raw,message=None,route="closed"):
        check=check_closed_main if route=="closed" else check_open_fragment
        result=check(fixed_sigma(),raw)
        self.assertIn(result.verdict,("semantic-no","unsupported"),result.diagnostic)
        self.assertIsNone(result.derivation)
        if message: self.assertIn(message,result.diagnostic["message"])
        return result

    def exported(self,raw):
        result=prepare_execution(fixed_sigma(),g.encode(raw))
        self.assertEqual(result["status"],"ready",result.get("diagnostic"))
        return result["program"]

    def test_new_exact_registry_profile_and_contract(self):
        program=self.exported(minimal())
        self.assertEqual(program["profile"],"src1-response-offer-owner1-v1")
        self.assertEqual(program["sigma"],wire_sigma())
        self.assertEqual(program["sigma"]["id"],"p8-src1-fixed-sigma-v1")
        self.assertEqual(program["sigma"]["abi"],"p8-src1-runtime-sigma-abi-v1")
        self.assertEqual(program["sigma"]["operations"][-1],{
            "name":"B.response","kind":"host-response","implementation":"src1-offer-response-word-v1"})
        self.assertEqual(program["sigma"]["host_response_contract"],{
            "id":"src1-explicit-response-intent-v1","port":"B","registry_name":"B.response",
            "command":"offer_response","payload_type":"Word","result_type":"Unit",
            "caller":"init-main-job-only","owner_authority":"identity-only-no-storage-access",
            "submission":"single-valid-intent-after-receipt-v1",
            "commit":"original-main-joined-all-source-safe-v1"})

    def test_offer_result_unit_and_effect_exactly_response_raise(self):
        result=self.accepted(minimal())
        derivations=[x for x in prior.walk(result.derivation)
                     if x.family=="derivation" and x.tag=="offer_response"]
        self.assertEqual(len(derivations),1)
        d=derivations[0]
        self.assertEqual(d.get("type").tag,"Unit")
        self.assertEqual(d.get("type").fields,())
        atoms=d.get("effect").get("atoms")
        self.assertEqual(len(atoms),2)
        self.assertEqual({a.tag for a in atoms},{"HostResponse","Raise"})
        self.assertEqual(set(dict(next(a for a in atoms if a.tag=="HostResponse").fields)),{"owner"})
        self.assertEqual(next(a for a in atoms if a.tag=="Raise").fields,())
        self.assertTrue(d.get("normal"));self.assertTrue(d.get("exceptional"))

    def test_wire_owner_token_occurrences_and_nominal_effect_correspond(self):
        program=self.exported(minimal())
        rows=program["sites"]
        offer_rows=[r for r in rows if r["node_kind"]=="command:offer_response"]
        self.assertEqual(len(offer_rows),1)
        row=offer_rows[0];path=row["site"]["pointer"]
        by_path={r["site"]["pointer"]:r for r in rows}
        owner=by_path[path+"/owner"];token=by_path[path+"/token"]
        self.assertEqual(rows.index(owner),rows.index(row)+1)
        self.assertEqual(rows.index(token),rows.index(row)+2)
        typ=lambda r:decode_static(program,r["type"])
        self.assertEqual(typ(row),{"family":"type","tag":"Unit","fields":{}})
        self.assertEqual(typ(token),{"family":"type","tag":"Word","fields":{}})
        self.assertEqual(typ(owner)["tag"],"Owner")
        eps=decode_static(program,row["effect"])
        atoms=eps["fields"]["atoms"]
        self.assertEqual(len(atoms),2)
        response=next(a for a in atoms if a["tag"]=="HostResponse")
        self.assertEqual(response["fields"],{"owner":typ(owner)["fields"]["owner"]})
        self.assertEqual(owner["usage"]["move"],[])
        self.assertEqual(len(owner["usage"]["copy"]),1)
        self.assertEqual(free_terms(offer(token=c2.var("actualB"))),frozenset(("owner","actualB")))

    def test_original_main_run_may_offer_before_run_exit(self):
        self.accepted(run_offer());self.exported(run_offer())

    def test_owner_reusable_by_later_offer(self):
        # Dynamic once validation rejects the second call; the type system does
        # not consume Owner or falsely prove a once-only control-flow property.
        raw=minimal(c2.bind("firstIntent",offer(),offer()))
        self.accepted(raw);self.exported(raw)

    def test_caught_duplicate_remains_source_exportable(self):
        caught=c2.c("try",body=offer(),failure_name="duplicate",handler=c2.ret())
        self.exported(minimal(c2.bind("firstIntent",offer(),caught)))

    def test_literal_equality_is_not_a_static_receipt_proof(self):
        # No RECEIVE or actual B receipt is present. Export acceptance is not
        # runtime permission to reply; that distinct boundary is deliberate.
        raw=minimal()
        self.exported(raw)
        self.assertFalse(any(n.get("command")=="receive_request" for _,n in c2.nodes(raw)))

    def test_receive_only_source_remains_exportable_for_dynamic_missing_intent(self):
        program=self.exported(g.source())
        self.assertEqual(sum(r["node_kind"]=="command:receive_request" for r in program["sites"]),1)
        self.assertFalse(any(r["node_kind"]=="command:offer_response" for r in program["sites"]))

    def test_legacy_no_external_complete_responsibility_source_remains_exportable(self):
        program=self.exported(c2.waiter_source())
        self.assertFalse(any(r["node_kind"] in ("command:receive_request","command:offer_response") for r in program["sites"]))

    def test_nonword_stable_payloads_rejected(self):
        for token in (c2.unit(),c2.v("nat",literal=271),c2.v("bool",literal=True),
                      c2.pair(c2.word(1),c2.word(2)),c2.var("owner"),c2.pure_fn()):
            with self.subTest(token=token):
                self.rejected(minimal(offer(token=token)),"OFFER_RESPONSE token must be Word")

    def test_nonowner_rejected(self):
        for owner in (c2.unit(),c2.word(271),c2.v("nat",literal=0)):
            with self.subTest(owner=owner):
                self.rejected(minimal(offer(owner=owner)),"OFFER_RESPONSE needs Owner")

    def test_result_is_not_word(self):
        # NEW checks its inferred body before closing the generated interface;
        # its result mismatch is the actual earliest boundary for this source.
        self.rejected(minimal(result=c2.t("Word")),"generation result type")

    def test_wrong_source_port_including_registry_name_rejected(self):
        for port in ("C","B.response","Prefill8"):
            message="identifier required" if port=="B.response" else "unregistered host-response port"
            with self.subTest(port=port): self.rejected(minimal(offer(port=port)),message)

    def test_response_registry_is_neither_pure_nor_kernel(self):
        for name in ("B","B.response"):
            with self.subTest(name=name):
                with self.assertRaises(FoundationError): fixed_sigma().pure(name)
                with self.assertRaises(FoundationError): fixed_sigma().kernel(name)

    def test_exact_source_fields(self):
        for key,value in (("effect",response_effect()),("receipt","forged"),("request","forged"),("epoch","AG")):
            raw=offer();raw[key]=value
            with self.subTest(extra=key): self.rejected(minimal(raw))
        for key in ("name","owner","token"):
            raw=offer();del raw[key]
            with self.subTest(missing=key): self.rejected(minimal(raw))

    def test_response_effect_cannot_be_omitted_or_replaced_by_read_input(self):
        for effect in (c2.eff(c2.atom("Raise")),m.read_effect(),prior.host_effect()):
            with self.subTest(effect=effect):
                self.rejected(minimal(effect=effect),"main source effect upper bound")

    def test_valid_but_different_owner_nominal_does_not_cover_effect(self):
        names=[c2.nm("NOther","Size"),c2.nm("OOther","Owner")]
        facts=[c2.rel("positiveSize",size={"witness":"NOther"}),
               c2.rel("owner",owner="OOther",size={"witness":"NOther"}),
               c2.rel("sizeEqual",left={"witness":"NOther"},right=1)]
        body=c2.c("new_owner",value=c2.v("nat",literal=1),names=names,name="otherOwner",body=offer())
        # Both names have proper Owner sort; actual operand is outer O, while
        # the claimed HostResponse upper bound mentions the distinct OOther.
        eps=c2.union(c2.eff(c2.atom("Alloc"),c2.atom("Raise")),c2.nu(names,facts,response_effect("OOther")))
        self.rejected(minimal(body,effect=eps),"main source effect upper bound")

    def test_response_effect_has_no_port_literal(self):
        self.rejected(minimal(effect=c2.eff(c2.atom("HostResponse",owner="O",name="B"),c2.atom("Raise"))))

    def test_response_owner_effect_requires_owner_sort(self):
        self.rejected(minimal(effect=response_effect("N")))

    def test_fork_cannot_inherit_init_main_authority(self):
        for label,body,eps in (("direct",offer(),response_effect()),("inside-run",*in_run(offer(),response_effect(),"Child"))):
            with self.subTest(context=label):
                job,je=c2.fork("Offer","TestScope","TestS","TestR",c2.t("Unit"),body,["owner"],c2.ret(),body_effect=eps)
                scoped,se=c2.scope("Test",job,je)
                self.rejected(minimal(scoped,effect=se),"OFFER_RESPONSE requires original INIT main Job")

    def test_producer_cleanup_cannot_offer(self):
        for label,body,ce in (("direct",offer(),response_effect()),("inside-run",*in_run(offer(),response_effect(),"Cleanup"))):
            with self.subTest(context=label):
                run=m.producer_run("A","Prefill32",m.tokens_value([c2.word(x) for x in m.A32]),
                                   c2.ret(),payload=c2.t("Unit"),cleanup=body)
                run["body"]["cleanup"]["captures"]=["owner"]
                eps=m.run_effect("A",c2.t("Unit"),ce,c2.eff())
                self.rejected(minimal(run,effect=eps),"OFFER_RESPONSE requires original INIT main Job")

    def test_symbolic_lambda_cannot_claim_main_authority(self):
        for label,body,eps in (("direct",offer(),response_effect()),("inside-run",*in_run(offer(),response_effect(),"Lambda"))):
            with self.subTest(context=label):
                fn=c2.lam("unused",c2.t("Unit"),c2.t("Unit"),body,eps,["owner"])
                self.rejected(minimal(c2.letv("offerFn",fn,c2.call("offerFn")),effect=eps),"OFFER_RESPONSE requires original INIT main Job")

    def test_open_caller_has_no_private_init_authority(self):
        raw=c2.open_storage(False)
        raw["entry"]["inputs"]=[{"name":"owner","type":c2.t("Owner",owner="O",size={"witness":"N"})}]
        raw["entry"].update(result=c2.t("Unit"),effect=response_effect(),body=offer())
        self.rejected(raw,"OFFER_RESPONSE requires original INIT main Job",route="open")

    def test_offer_does_not_grant_read(self):
        for operand in ("owner","intent"):
            body=c2.bind("intent",offer(),c2.c("load",read=c2.var(operand),index=c2.v("nat",literal=0)))
            with self.subTest(operand=operand):
                self.rejected(minimal(body,c2.t("Word"),c2.union(response_effect(),m.read_effect())),"LOAD needs Read")

    def test_affine_closure_remains_in_real_bind_frame(self):
        body=c2.letv("affineFn",c2.pure_fn("A"),c2.bind("intent",offer(),c2.call("affineFn")))
        raw=minimal(body,c2.t("Word"));self.accepted(raw)
        program=self.exported(raw)
        frames=[r["frame"] for r in program["sites"] if r["node_kind"]=="command:let_c" and r["frame"] is not None]
        self.assertEqual(len(frames),1)
        self.assertEqual(frames[0]["kind"],"Bind")
        self.assertEqual(len(frames[0]["retained"]),1)
        self.assertEqual(frames[0]["normal_restore"],"move-once")

    def test_offer_does_not_duplicate_affine_closure(self):
        after=c2.bind("first",c2.call("affineFn"),c2.call("affineFn"))
        body=c2.letv("affineFn",c2.pure_fn("A"),c2.bind("intent",offer(),after))
        self.rejected(minimal(body,c2.t("Word")))

    def test_bottom_continuation_infers_unit_without_granting_authority(self):
        raw=minimal(c2.bind("never",c2.throw_body(),offer()))
        self.accepted(raw);self.exported(raw)

    def test_duplicate_wire_fields_rejected_before_source_check(self):
        original=g.encode(minimal())
        changed=original.replace(b'"name":"B"',b'"name":"B","name":"B"',1)
        self.assertNotEqual(changed,original)
        response=prepare_execution(fixed_sigma(),changed)
        self.assertEqual(response["status"],"static-rejected")
        self.assertIn("duplicate JSON field",response["diagnostic"]["message"])

    def test_all_nine_complete_sources_export_unit_and_preserve_real_b_input(self):
        builder=complete_builder()
        expected={"unit_after_b":1,"inside_b_run":1,"source_failure_after_offer":1,
                  "duplicate_uncaught":2,"duplicate_caught":2,"missing_intent":0,
                  "wrong_token":1,"wrong_owner":1,"offer_before_receive":1}
        self.assertEqual(tuple(expected),builder.CASES)
        original=g.source();original_a=original["main"]["body"]["body"]["first"]
        old_b=next(n for _,n in c2.nodes(original) if n.get("command")=="produce" and n.get("kernel")=="Prefill8")
        old_history=next(n for _,n in c2.nodes(original) if n.get("command")=="let_c" and n.get("name")=="oldAToken")
        for case,count in expected.items():
            with self.subTest(case=case):
                raw=builder.source(case);self.assertEqual(raw["main"]["result"],c2.t("Unit"))
                self.assertEqual(raw["main"]["body"]["body"]["first"],original_a)
                receives=[n for _,n in c2.nodes(raw) if n.get("command")=="receive_request"]
                self.assertEqual(receives,[prior.receive()])
                producers=[n for _,n in c2.nodes(raw) if n.get("command")=="produce" and n.get("kernel")=="Prefill8"]
                self.assertEqual(len(producers),1)
                producer=producers[0]
                self.assertEqual(producer["argument"],c2.var("externalBInput"))
                self.assertEqual(producer["cleanup"],old_b["cleanup"])
                self.assertEqual(producer["names"],old_b["names"])
                histories=[n for _,n in c2.nodes(raw) if n.get("command")=="let_c" and n.get("name")=="oldAToken"]
                self.assertEqual(len(histories),1)
                self.assertEqual(histories[0]["first"],old_history["first"])
                if case!="inside_b_run": self.assertEqual(producer,old_b)
                else:
                    self.assertEqual(producer["next"]["name"],"actualBInside")
                    self.assertEqual(producer["next"]["first"],old_b["next"])
                offers=[(p,n) for p,n in c2.nodes(raw) if n.get("command")=="offer_response"]
                self.assertEqual(len(offers),count)
                sites=builder.source_sites(raw)
                self.assertEqual(sites["offer_sites"],["/"+"/".join(map(str,p)) for p,_ in offers])
                self.assertEqual(set(sites["original_sites"]),set(g.source_sites(original)))
                program=self.exported(raw)
                self.assertEqual(sum(r["node_kind"]=="command:offer_response" for r in program["sites"]),count)
                self.assertEqual(sum(r["node_kind"]=="command:receive_request" for r in program["sites"]),1)
                main=next(r for r in program["sites"] if r["site"]["pointer"]=="/main/body")
                self.assertEqual(decode_static(program,main["type"]),{"family":"type","tag":"Unit","fields":{}})


if __name__=="__main__": unittest.main(verbosity=2)
