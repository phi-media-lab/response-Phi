#!/usr/bin/env python3
"""CPU transport controls using the production Worker state machine; no MLX."""
import sys
import json
from pathlib import Path
# -I omits the script directory. Only this frozen sibling is added pre-permit;
# no external site-packages or .pth startup is enabled for the stub.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from worker import Worker, arguments, require, integer


class Pool:
    def __init__(self):
        self.used_page_ids = set()
        self.free_page_ids = []
        self.num_pages = 0
        self.capacity = 0
        self.storage_nbytes = 0

    def allocate(self):
        page = self.free_page_ids.pop() if self.free_page_ids else self.num_pages
        if page == self.num_pages:
            self.num_pages += 1
        self.used_page_ids.add(page)
        self.capacity = 4
        self.storage_nbytes = 2097152  # Explicit fake geometry, never a device observation.
        return page


class Cache:
    def __init__(self, pool):
        self.pool = pool
        self.page_ids = []
        self.page_lens = []
        self.offset = 0

    def release(self):
        for page in self.page_ids:
            require(page in self.pool.used_page_ids, 'duplicate stub release')
            self.pool.used_page_ids.remove(page)
            self.pool.free_page_ids.append(page)
        self.page_ids.clear()
        self.page_lens.clear()
        self.offset = 0


class FakeBackend:
    name = 'stub'

    def __init__(self, config):
        self.pools = [Pool() for _ in range(28)]

    def provenance(self, final=False):
        return {'backend': 'stub', 'physical_completion_evidence': False,
                'description': 'stdlib logical pools; production Worker protocol'}

    def create_cache(self):
        return [Cache(pool) for pool in self.pools]

    def build(self, tokens, cache):
        for layer in cache:
            layer.page_ids = [layer.pool.allocate()]
            layer.page_lens = [len(tokens)]
            layer.offset = len(tokens)
        return sum(tokens) % 151936

    def materialize(self, token):
        pass

    def item(self, token):
        return token

    def release_layer(self, cache, layer):
        cache[layer].release()


class HeldEvaluationWorker(Worker):
    """C1 only: retain the real first EVAL at its emitted ENTER milestone.

    This class is never selected by the real-model worker. The control frame
    does not advance the EVAL/RELEASE command sequence or create another EVAL.
    """
    def emit(self, kind, **fields):
        result = super().emit(kind, **fields)
        if kind == 'EVAL_ENTER' and self.claimed == 1:
            require(self.scenario == 'cm2_eval_hold' and self.factory is FakeBackend,
                    'CPU hold belongs only to the selected stub')
            control = self.transport.read(timeout=3)
            keys = ['v', 'session', 'worker_id', 'seq', 'request_id', 'lease_epoch',
                    'operation', 'purpose', 'source_context', 'native_context']
            expected = {key: self.current[key] for key in keys}
            expected['kind'] = 'CONTINUE_EVAL'
            require(set(control) == set(expected) and
                    json.dumps(control, sort_keys=True, separators=(',', ':')) ==
                    json.dumps(expected, sort_keys=True, separators=(',', ':')) and
                    all(integer(control.get(key)) for key in ['v', 'seq', 'lease_epoch']),
                    'CPU continuation must match the original pending EVAL exactly')
            super().emit('CONTINUE_EVAL_ACK', control_kind='CONTINUE_EVAL')
        return result


class SRC1BTokenBackend(FakeBackend):
    """CPU-only B8 result rule, before the production Worker builds a receipt.

    The full request still allocates its 28 logical cache layers and passes
    through materialize/item, EVAL_RECEIPT, RELEASE and RELEASE_ACK. No already
    published frame, driver result or source argument is overwritten.
    """
    token_id = None
    rule = None
    expected_b8 = (2127, 19010, 19287, 13098, 1724, 7344, 1626, 72)

    def build(self, tokens, cache):
        ordinary = super().build(tokens, cache)
        if len(tokens) == 8:
            require(tuple(tokens) == self.expected_b8,
                    'SRC1 B token fixture requires the complete fixed B8')
            require(self.token_id in (151643, 151669), 'fixed CPU token rule')
            return self.token_id
        require(len(tokens) == 32, 'SRC1 B fixture permits only original A32/B8')
        return ordinary

    def provenance(self, final=False):
        value = super().provenance(final=final)
        value['src1b_token_rule'] = {
            'id': self.rule, 'b8': list(self.expected_b8), 'b_token': self.token_id,
            'a32_rule': 'sum-mod-151936', 'model_execution': False,
            'boundary': 'backend-build-before-materialize-item-and-receipt'}
        return value


class SRC1BAddedTokenBackend(SRC1BTokenBackend):
    token_id = 151643
    rule = 'src1b-added-token-151643-v1'


class SRC1BMissingTokenBackend(SRC1BTokenBackend):
    token_id = 151669
    rule = 'src1b-missing-token-151669-v1'


if __name__ == '__main__':
    args = arguments(stub=True)
    aliases = {'normal': 'success', 'wrong_identity': 'wrong_session',
               'eof_eval': 'eof_after_enter', 'timeout_eval': 'timeout_after_enter'}
    scenario = aliases.get(args.scenario, args.scenario)
    require(scenario in {'success', 'wrong_session', 'wrong_seq', 'duplicate_receipt',
                         'truncated_receipt', 'eof_after_enter', 'timeout_after_enter',
                         'partial_release', 'lost_release_ack', 'nonzero_after_receipt', 'no_permit',
                         'wrong_release_epoch', 'duplicate_release_ack', 'nonzero_after_release_ack',
                         'wrong_receipt_request', 'cm2_eval_hold',
                         'src1b_added_token', 'src1b_missing_token'},
            'unknown stub scenario')
    selected = HeldEvaluationWorker if scenario == 'cm2_eval_hold' else Worker
    factory = {'src1b_added_token': SRC1BAddedTokenBackend,
               'src1b_missing_token': SRC1BMissingTokenBackend}.get(scenario, FakeBackend)
    sys.exit(selected(args, factory, scenario).run())
