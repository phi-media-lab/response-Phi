#!/usr/bin/env python3
"""EBD1 frontend semantic controls. Root records the first actual invocation.

No Swift, model, worker, GPU, network, or generated-result fixtures are used.
The pure source generator and frontend are first executed only by this suite
or the separately recorded root source-generation command.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import unittest

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
spec=importlib.util.spec_from_file_location("ebd1_test_pure_source",HERE.parent/"programs"/"build_external_demand_programs.py")
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
c2,m=g.cm2,g.m
from p8source.entry import check_closed_main, check_open_fragment
from p8source.execution import prepare_execution
from p8source.sigma import fixed_sigma, wire_sigma, tokens_type
from p8source.binding import free_terms
from p8foundation.model import RNode, Binder, FoundationError
from p8closure.support import copyable, stable


def receive(owner=None,port="B"):
    return c2.c("receive_request",name=port,owner=c2.var("owner") if owner is None else owner)


def host_effect(): return c2.eff(c2.atom("HostInput",owner="O"),c2.atom("Raise"))


def minimal(body=None,result=None,effect=None):
    return m.with_owner(receive() if body is None else body,
        host_effect() if effect is None else effect,g.tokens_type() if result is None else result)


def walk(value):
    if isinstance(value,RNode):
        yield value
        for _,child in value.fields: yield from walk(child)
    elif isinstance(value,Binder):
        yield from walk(value.body)
    elif isinstance(value,(tuple,list)):
        for child in value: yield from walk(child)


class ExternalRequestTests(unittest.TestCase):
    def accepted(self,raw):
        checked=check_closed_main(fixed_sigma(),raw)
        self.assertEqual(checked.verdict,"checked-closed-main-source",checked.diagnostic)
        return checked

    def rejected(self,raw,message=None,route="closed"):
        check=check_closed_main if route=="closed" else check_open_fragment
        result=check(fixed_sigma(),raw)
        self.assertIn(result.verdict,("semantic-no","unsupported"),result.diagnostic)
        self.assertIsNone(result.derivation)
        if message: self.assertIn(message,result.diagnostic["message"])
        return result

    def test_complete_source_is_exportable(self):
        raw=g.source();self.accepted(raw)
        result=prepare_execution(fixed_sigma(),g.encode(raw))
        self.assertEqual(result["status"],"ready",result.get("diagnostic"))
        program=result["program"]
        self.assertEqual(program["profile"],"src1-response-offer-owner1-v1")
        self.assertEqual(program["sigma"],wire_sigma())
        self.assertEqual(program["sigma"]["host_input_contract"],{
            "id":"p8-ebd1-external-request-v1","port":"B","command":"receive_request",
            "result_type":"Tokens8","caller":"init-main-job-only",
            "owner_authority":"identity-only-no-storage-access",
            "submission":"single-irrevocable-complete-request-v1"})
        self.assertEqual(program["sigma"]["id"],"p8-src1-fixed-sigma-v1")
        self.assertEqual(program["sigma"]["abi"],"p8-src1-runtime-sigma-abi-v1")

    def test_full_external_input_controls_b_source_dataflow(self):
        old=c2.waiter_source();new=g.source()
        old_owner=old["main"]["body"];new_owner=new["main"]["body"]
        old_sequence=old_owner["body"];new_sequence=new_owner["body"]
        self.assertEqual(new_sequence["command"],"let_c")
        self.assertEqual(new_sequence["name"],"aRunResult")
        # A RUN contains W's fork, cancellation, await and A result publication.
        self.assertEqual(new_sequence["first"],old_sequence["first"])
        self.assertFalse(any(n.get("command")=="receive_request" for _,n in c2.nodes(new_sequence["first"])))
        receives=[(p,n) for p,n in c2.nodes(new) if n.get("command")=="receive_request"]
        self.assertEqual(len(receives),1)
        self.assertEqual(receives[0][1],receive())
        self.assertEqual(receives[0][0][:4],("main","body","body","next"))
        binds=[n for _,n in c2.nodes(new) if n.get("command")=="let_c" and n.get("name")=="externalBInput"]
        self.assertEqual(len(binds),1);binding=binds[0]
        self.assertEqual(binding["first"],receive())
        self.assertIs(binding,new_sequence["next"])
        self.assertEqual(binding["next"]["command"],"let_e")
        b_candidates=[n for _,n in c2.nodes(binding["next"]) if n.get("command")=="let_c" and n.get("name")=="bToken"]
        self.assertEqual(len(b_candidates),1);b=b_candidates[0]
        self.assertEqual(b["first"]["command"],"run")
        producer=b["first"]["body"]
        self.assertEqual(producer["kernel"],"Prefill8")
        self.assertEqual(producer["argument"],c2.var("externalBInput"))
        self.assertEqual(free_terms(producer["argument"],"value"),frozenset(("externalBInput",)))
        self.assertEqual(new["main"]["result"],old["main"]["result"])
        old_b=[n for _,n in c2.nodes(old) if n.get("command")=="let_c" and n.get("name")=="bToken"][0]
        self.assertEqual(b["next"],old_b["next"])  # history A + W/A/B/history return
        restored=deepcopy(b);restored["first"]["body"]["argument"]=deepcopy(old_b["first"]["body"]["argument"])
        self.assertEqual(restored,old_b)
        self.assertEqual(set(g.source_sites(new)),{"a_produce","w_fork","waiter_scope","w_await","b_receive","a_run","b_run","b_produce"})

    def test_receive_effect_is_host_identity_and_raise_only(self):
        result=self.accepted(minimal())
        derivations=[n for n in walk(result.derivation) if n.family=="derivation" and n.tag=="receive_request"]
        self.assertEqual(len(derivations),1)
        atoms=derivations[0].get("effect").get("atoms")
        self.assertEqual({a.tag for a in atoms},{"HostInput","Raise"})
        host=[a for a in atoms if a.tag=="HostInput"][0]
        self.assertEqual(set(dict(host.fields)),{"owner"})
        self.assertEqual(derivations[0].get("type"),tokens_type(8))
        self.assertTrue(copyable(tokens_type(8)));self.assertTrue(stable(tokens_type(8)))
        self.assertFalse(any(n.family=="type" and n.tag in ("Read","Write","Lease") for n in walk(derivations[0].get("type"))))

    def test_owner_is_reusable_after_receive(self):
        # The full source uses the SAME owner in B RUN after RECEIVE.
        self.accepted(g.source())
        body=c2.bind("input",receive(),c2.ret(c2.pair(c2.var("owner"),c2.var("input"))))
        # Owner may not escape NEW's nominal generation; identity-only input
        # does not erase this original freshness obligation.
        self.rejected(minimal(body,c2.t("Unit")),"generation result name escape")

    def test_wrong_port_rejected(self): self.rejected(minimal(receive(port="C")),"unregistered host-input port")

    def test_host_port_not_pure_or_kernel(self):
        with self.assertRaises(FoundationError): fixed_sigma().pure("B")
        with self.assertRaises(FoundationError): fixed_sigma().kernel("B")
        self.assertIn({"name":"B","kind":"host-input","implementation":"ebd1-receive-request-tokens8-v1"},wire_sigma()["operations"])

    def test_non_owner_rejected(self):
        for value in (c2.unit(),c2.word(3),c2.v("nat",literal=0),c2.pair(c2.word(1),c2.word(2))):
            with self.subTest(value=value): self.rejected(minimal(receive(value)),"RECEIVE_REQUEST needs Owner")

    def test_wrong_receive_fields_rejected(self):
        cases=[]
        for field,value in (("payload",c2.word(4)),("effect",host_effect()),("epoch","AG"),("requires",{"requires":"true"})):
            candidate=receive();candidate[field]=value;cases.append(candidate)
        for field in ("owner","name"):
            candidate=receive();del candidate[field];cases.append(candidate)
        for case in cases:
            with self.subTest(case=case): self.rejected(minimal(case))

    def test_missing_hostinput_declaration_rejected(self):
        self.rejected(minimal(effect=c2.eff(c2.atom("Raise"))),"main source effect upper bound")

    def test_read_declaration_does_not_cover_hostinput(self):
        self.rejected(minimal(effect=m.read_effect()),"main source effect upper bound")

    def test_port_literal_is_not_a_hostinput_effect_field(self):
        atom=c2.atom("HostInput",owner="O",name="B")
        self.rejected(minimal(effect=c2.eff(atom,c2.atom("Raise"))))

    def test_effect_owner_must_be_owner_nominal(self):
        self.rejected(minimal(effect=c2.eff(c2.atom("HostInput",owner="N"),c2.atom("Raise"))))

    def test_child_fork_cannot_inherit_main_receive_authority(self):
        job,je=c2.fork("Input","TestScope","TestS","TestR",g.tokens_type(),receive(),["owner"],c2.ret(),body_effect=host_effect())
        scoped,se=c2.scope("Test",job,je)
        self.rejected(minimal(scoped,c2.t("Unit"),se),"RECEIVE_REQUEST requires original INIT main Job")

    def test_producer_cleanup_cannot_receive(self):
        run=m.producer_run("A","Prefill32",m.tokens_value([c2.word(x) for x in m.A32]),c2.ret(),payload=g.tokens_type(),cleanup=receive())
        run["body"]["cleanup"]["captures"]=["owner"]
        eps=m.run_effect("A",g.tokens_type(),host_effect(),c2.eff())
        self.rejected(minimal(run,c2.t("Unit"),eps),"RECEIVE_REQUEST requires original INIT main Job")

    def test_symbolic_lambda_cannot_claim_main(self):
        fn=c2.lam("unused",c2.t("Unit"),g.tokens_type(),receive(),host_effect(),["owner"])
        body=c2.letv("inputFn",fn,c2.call("inputFn"))
        self.rejected(minimal(body),"RECEIVE_REQUEST requires original INIT main Job")

    def test_open_caller_cannot_claim_private_init(self):
        raw=c2.open_storage(False)
        raw["entry"]["inputs"]=[{"name":"owner","type":c2.t("Owner",owner="O",size={"witness":"N"})}]
        raw["entry"].update(result=g.tokens_type(),effect=host_effect(),body=receive())
        self.rejected(raw,"RECEIVE_REQUEST requires original INIT main Job",route="open")

    def test_hostinput_does_not_grant_read(self):
        for operand in ("owner","input"):
            body=c2.bind("input",receive(),c2.c("load",read=c2.var(operand),index=c2.v("nat",literal=0)))
            with self.subTest(operand=operand):
                self.rejected(minimal(body,c2.t("Word"),c2.union(host_effect(),m.read_effect())),"LOAD needs Read")

    def test_affine_frame_survives_receive(self):
        body=c2.letv("affineFn",c2.pure_fn("A"),c2.bind("input",receive(),c2.call("affineFn")))
        self.accepted(minimal(body,c2.t("Word")))

    def test_receive_does_not_duplicate_affine_value(self):
        after=c2.bind("first",c2.call("affineFn"),c2.call("affineFn"))
        body=c2.letv("affineFn",c2.pure_fn("A"),c2.bind("input",receive(),after))
        self.rejected(minimal(body,c2.t("Word")))

    def test_storage_capture_regressions_remain_rejected(self):
        for name in ("read_direct","read_closure","read_exists","affine_capture","open_lease","open_write"):
            with self.subTest(case=name):
                self.rejected(c2.source(name),c2.MESSAGES[name],route="open" if name.startswith("open_") else "closed")

    def test_duplicate_wire_fields_rejected_before_checking(self):
        data=g.encode(minimal()).replace(b'"name":"B"',b'"name":"B","name":"B"',1)
        self.assertNotEqual(data,g.encode(minimal()))
        response=prepare_execution(fixed_sigma(),data)
        self.assertEqual(response["status"],"static-rejected")
        self.assertIn("duplicate JSON field",response["diagnostic"]["message"])


if __name__=="__main__": unittest.main(verbosity=2)
