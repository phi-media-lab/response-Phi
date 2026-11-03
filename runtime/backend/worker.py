#!/usr/bin/env python3
"""CM1 single-lease MLX worker. No MLX/reference import before LOAD_PERMIT.

EVAL receives actual operation/tokens; there is no A/B label lookup, predicted
output, hidden warmup or decode. Source epochs, wire leases and page allocation
generations have distinct domains. Native reference requests have no source ID.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import sys
import time
import traceback

LIMIT = 1 << 20


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value):
    return type(value) is int


def python_flags():
    return {'isolated': sys.flags.isolated, 'no_site': sys.flags.no_site,
            'dont_write_bytecode': sys.flags.dont_write_bytecode}


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def token_hash(tokens):
    return hashlib.sha256(json.dumps(tokens, separators=(',', ':')).encode()).hexdigest()


def strict_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    obj = json.loads(raw, object_pairs_hook=pairs,
                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON')))
    require(type(obj) is dict, 'expected JSON object')
    return obj


class Transport:
    def __init__(self):
        # Preserve the pipe for protocol first; Python and native fd1 output go to stderr.
        self.output = os.fdopen(os.dup(1), 'wb', buffering=0)
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        self.buffer = bytearray()

    def write(self, obj, truncated=False):
        line = (json.dumps(obj, separators=(',', ':'), allow_nan=False) + '\n').encode()
        require(len(line) <= LIMIT, 'output line exceeds 1 MiB')
        view = memoryview(line[:len(line)//2] if truncated else line)
        while view:
            written = self.output.write(view)
            if not written:
                raise BrokenPipeError('protocol write made no progress')
            view = view[written:]

    def read(self, timeout=30):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
                raise TimeoutError('command/permit input deadline')
            block = os.read(0, 65536)
            if not block:
                raise EOFError('stdin EOF' + (' with partial line' if self.buffer else ''))
            self.buffer.extend(block)
            require(len(self.buffer.split(b'\n', 1)[0]) < LIMIT, 'input line exceeds 1 MiB')
        raw, remainder = self.buffer.split(b'\n', 1)
        self.buffer = bytearray(remainder)
        require(len(raw) + 1 <= LIMIT, 'input line including LF exceeds 1 MiB')
        return strict_object(raw.decode('utf-8'))


def verify_inputs(config):
    for row in config['external_inputs']:
        path = Path(row['path'])
        require(path.is_absolute() and path.is_file(), f'missing frozen input {path}')
        require(str(path.resolve()) == row['resolved_path'], f'retargeted input {path}')
        require(path.stat().st_size == row['bytes'] and file_hash(path) == row['sha256'],
                f'changed input {path}')
    return {'files': len(config['external_inputs']), 'verified': True}


class RealBackend:
    name = 'mlx'

    def __init__(self, config):
        self.config = config
        self.verification = verify_inputs(config)
        require(Path(sys.executable).resolve() == Path(config['python']).resolve(), 'wrong Python')
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        sys.dont_write_bytecode = True
        venv = Path(config['tiny_root']) / '.venv'
        site = Path(config['site_packages'])
        expected_site = venv / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
        require(Path(config['python']).parent == venv / 'bin' and site == expected_site and site.is_dir(),
                'site-packages must be the exact frozen worker venv path')
        # -S suppresses site startup. Add this one frozen path only after permit;
        # do not call site.main/addsitedir and do not execute .pth hooks.
        sys.path.insert(0, str(site))
        sys.path.insert(0, str(Path(config['tiny_root']) / 'src'))
        sys.path.insert(0, config['tiny_root'])
        # All imports with possible device/extension effects are below the permit boundary.
        import mlx.core as mx
        from mlx_lm import load
        from tiny_llm_ref.qwen3_week3 import Qwen3ModelWeek3
        from extensions_ref import tiny_llm_ext_ref
        for symbol in ['paged_attention', 'paged_cache_update', 'rms_norm', 'rope',
                       'swiglu', 'decode_attention', 'quantized_matmul']:
            require(callable(getattr(tiny_llm_ext_ref, symbol, None)), f'extension missing {symbol}')
        mx.set_default_device(mx.gpu)
        loaded = load(config['model'], return_config=False)
        self.mx = mx
        self.model = Qwen3ModelWeek3(loaded[0], page_size=128)
        self.pools = self.model.page_pools
        require((self.model.num_hidden_layers, self.model.page_size, self.model.vocab_size) ==
                (28, 128, 151936), 'loaded geometry mismatch')

    def provenance(self, final=False):
        import importlib.metadata
        if final:
            self.verification = verify_inputs(self.config)
        frozen = {str(Path(x['path']).resolve()): x for x in self.config['external_inputs']}
        modules = []
        prefixes = ('tiny_llm_ref', 'extensions_ref', 'mlx', 'mlx_lm')
        for name, module in sorted(tuple(sys.modules.items())):
            if not any(name == prefix or name.startswith(prefix + '.') for prefix in prefixes):
                continue
            filename = getattr(module, '__file__', None)
            if not filename:
                continue
            path = Path(filename).resolve()
            require(str(path) in frozen, f'unfrozen imported module {name}: {path}')
            digest = file_hash(path)
            require(digest == frozen[str(path)]['sha256'], f'imported module changed: {name}')
            modules.append({'module': name, 'path': str(path), 'sha256': digest})
        versions = {name: importlib.metadata.version(name) for name in self.config['package_versions']}
        require(versions == self.config['package_versions'], 'package versions changed')
        return {'backend': self.name, 'model': self.config['model'], 'python': sys.executable,
                'site_packages': self.config['site_packages'], 'python_flags': python_flags(),
                'input_verification': self.verification, 'actual_modules': modules,
                'package_versions': versions}

    def create_cache(self):
        return self.model.create_kv_cache()

    def build(self, tokens, cache):
        logits = self.model(self.mx.array(tokens, dtype=self.mx.int32)[None, :],
                            0, cache, logits_to_keep=1)
        return self.mx.argmax(logits[:, -1, :], axis=-1)

    def materialize(self, token):
        arrays = [array for pool in self.pools for array in (pool.key_pages, pool.value_pages)
                  if array is not None]
        require(len(arrays) == 56, 'incomplete live pool arrays')
        self.mx.eval(token, *arrays)

    def item(self, token):
        return int(token.item())

    def release_layer(self, cache, layer):
        cache[layer].release()


class Worker:
    def __init__(self, args, backend_factory=RealBackend, scenario='success'):
        self.args, self.factory, self.scenario = args, backend_factory, scenario
        self.transport = Transport()
        require(python_flags() == {'isolated': 1, 'no_site': 1, 'dont_write_bytecode': 1},
                'worker requires actual -I -S -B startup flags')
        self.config = strict_object(Path(args.inputs).read_text())
        self.input_sha = file_hash(args.inputs)
        require(self.config.get('schema') == 'cm1-inputs-v1', 'wrong input schema')
        require((self.config.get('layers'), self.config.get('page_size'), self.config.get('vocab_size'))
                == (28, 128, 151936), 'wrong fixed geometry')
        require(self.config.get('first_run_evaluations') == 2 and 'requests' not in self.config,
                'CM1 first-run metadata, not a production request lookup or admission cap')
        for key, count in [('a32', 32), ('b_prefix7', 7)]:
            tokens = self.config.get(key)
            require(type(tokens) is list and len(tokens) == count and
                    all(integer(t) and 0 <= t < 151936 for t in tokens), 'wrong frozen input prefix')
        self.backend = None
        self.active = None
        self.current = None
        self.sequence = 0
        self.events = 0
        self.completed = 0
        self.claimed = 0
        self.last_lease = 0
        self.request_ids = set()
        self.page_epochs = {}
        self.phase = 'bootstrap'
        self.service_rows = []
        self.service_dropped = 0
        self.service_flushed = False

    def service_mark(self, event, frame=None):
        # BRS1 leaf diagnostics: no wire event IDs, validation, waits or IO.
        captured = time.monotonic_ns()
        try:
            if len(self.service_rows) >= 64:
                self.service_dropped += 1
                return
            value = frame if frame is not None else (self.current or {})
            source = value.get('source_context') or {}
            self.service_rows.append({
                'schema': 'brs1-service-v1', 'role': 'production', 'event': event,
                'clock_domain': 'python-monotonic-ns', 'capture_ns': str(captured),
                'append_seq': len(self.service_rows) + 1, 'recorded_ns': None,
                'execution_domain': 'python', 'execution_binding': None,
                'priority': None, 'priority_null_reason': 'non-task-domain',
                'request_origin': {k: source.get(k) for k in
                    ('job', 'task', 'command', 'scope', 'backend_return', 'source_epoch', 'owner', 'site')},
                'operation_key': {'session': self.args.session, 'worker_id': self.args.worker_id,
                    'scope': source.get('scope'), 'owner': source.get('owner'),
                    'source_epoch': source.get('source_epoch'), 'backend_return': source.get('backend_return'),
                    'command': value.get('request_id'), 'source_site': source.get('site'),
                    'wire_seq': value.get('seq'), 'wire_lease_epoch': value.get('lease_epoch'),
                    'event_seq': value.get('event_seq'), 'receipt_id': value.get('receipt_id'),
                    'wire_kind': value.get('kind')},
                'details': {'execution_binding_reason': 'separate-Python-worker-no-Swift-Task'}})
        except Exception:
            self.service_dropped += 1

    def service_flush(self, completion):
        # Common Worker.run also serves the CPU stub. Hard os._exit/kill may
        # lose this entire buffer; absence is unavailable, never a zero duration.
        if self.service_flushed:
            return
        self.service_flushed = True
        try:
            obj = {'schema': 'brs1-worker-service-snapshot-v1', 'role': 'production',
                   'session': self.args.session, 'worker_id': self.args.worker_id,
                   'pid': os.getpid(), 'clock_domain': 'python-monotonic-ns',
                   'capacity': 64, 'dropped': self.service_dropped,
                   'overflow': bool(self.service_dropped), 'completion': completion,
                   'observation_complete': completion == 'shutdown',
                   'measurement_valid': completion == 'shutdown' and self.service_dropped == 0,
                   'events': self.service_rows}
            data = b'BRS1_SERVICE_TRACE ' + json.dumps(obj, sort_keys=True, separators=(',', ':')).encode() + b'\n'
            if len(data) > 65536:
                return  # retained raw has no complete diagnostic: measurement invalid
            offset = 0
            while offset < len(data):
                count = os.write(2, data[offset:])
                if count <= 0:
                    return
                offset += count
        except Exception:
            pass  # diagnostic failure never changes the original worker exit

    def emit(self, kind, **fields):
        self.events += 1
        obj = {'v': 1, 'kind': kind, 'session': self.args.session,
               'worker_id': self.args.worker_id, 'pid': os.getpid(), 'pgid': os.getpgrp(),
               'event_seq': self.events, 'worker_ns': time.monotonic_ns()}
        if self.current:
            obj.update({k: self.current[k] for k in ['seq', 'request_id', 'lease_epoch',
                        'operation', 'purpose', 'source_context', 'native_context']
                        if k in self.current})
        obj.update(fields)
        if kind == 'READY' and self.scenario == 'wrong_session':
            obj['session'] += '-wrong'
        if kind == 'EVAL_RECEIPT' and self.scenario == 'wrong_seq':
            obj['seq'] += 1
        if kind == 'EVAL_RECEIPT' and self.scenario == 'wrong_receipt_request':
            obj['request_id'] += '-foreign'
        if kind == 'RELEASE_ACK' and self.scenario == 'wrong_release_epoch':
            obj['lease_epoch'] += 1
        if kind == 'EVAL_RECEIPT' and self.scenario == 'truncated_receipt':
            self.transport.write(obj, truncated=True)
            os._exit(73)
        if kind == 'RELEASE_ACK':
            self.service_mark('ack.write_pre', obj)
        self.transport.write(obj)
        if kind == 'RELEASE_ACK':
            self.service_mark('ack.write_return', obj)
        if kind == 'EVAL_RECEIPT' and self.scenario == 'duplicate_receipt':
            self.transport.write(obj)
        if kind == 'RELEASE_ACK' and self.scenario == 'duplicate_release_ack':
            self.transport.write(obj)
        return obj

    def pool_id(self, layer):
        return f'{self.args.session}/{self.args.worker_id}/L{layer}'

    def provenance(self, final=False):
        require(file_hash(self.args.inputs) == self.input_sha, 'inputs manifest changed')
        result = self.backend.provenance(final=final)
        result['inputs_manifest'] = {'path': self.args.inputs, 'sha256': self.input_sha,
                                     'bytes': Path(self.args.inputs).stat().st_size}
        return result

    def snapshots(self):
        if self.backend is None:
            return []
        rows = []
        for layer, pool in enumerate(self.backend.pools):
            cache = self.active['cache'][layer] if self.active else None
            pages = list(cache.page_ids) if cache else []
            epochs = self.active['epochs'][layer] if self.active else {}
            rows.append({'layer': layer, 'pool_id': self.pool_id(layer),
                         'pool_object_id': str(id(pool)), 'cache_page_ids': pages,
                         'page_lens': list(cache.page_lens) if cache else [],
                         'offset': cache.offset if cache else 0,
                         'owned_pages': [{'page_id': p, 'allocation_epoch': epochs.get(p)} for p in pages],
                         'used_page_ids': sorted(pool.used_page_ids),
                         'free_page_ids': list(pool.free_page_ids), 'num_pages': pool.num_pages,
                         'capacity': pool.capacity, 'storage_nbytes': pool.storage_nbytes})
        return rows

    def check_partition(self, rows, active=False):
        require(len(rows) == 28, 'missing layer')
        for row in rows:
            used, free = row['used_page_ids'], row['free_page_ids']
            require(len(used) == len(set(used)) and len(free) == len(set(free)) and
                    set(used).isdisjoint(free) and set(used + free) == set(range(row['num_pages'])),
                    'used/free partition invalid')
            require(row['capacity'] >= row['num_pages'], 'backing capacity too small')
            if active:
                require(used == row['cache_page_ids'] and len(used) == 1, 'active ownership mismatch')
                require(row['offset'] == self.active['token_count'] and
                        row['page_lens'] == [self.active['token_count']], 'cache token coverage')
                require(all(integer(item['allocation_epoch']) and item['allocation_epoch'] > 0 and
                            item['allocation_epoch'] == self.page_epochs[(row['layer'], item['page_id'])]
                            for item in row['owned_pages']), 'unexpected per-pool/page allocation generation')
            else:
                require(not used and not row['cache_page_ids'] and not row['page_lens'] and
                        row['offset'] == 0, 'live pages remain')

    def bootstrap(self):
        require(os.getppid() == self.args.swift_pid and os.getpgrp() == self.args.pgid,
                'worker parent/group mismatch before model import')
        self.emit('HELLO', swift_pid=self.args.swift_pid, inputs_sha256=self.input_sha,
                  backend=self.factory.name, python_flags=python_flags())
        permit = self.transport.read()
        expected = {'v': 1, 'kind': 'LOAD_PERMIT', 'session': self.args.session,
                    'worker_id': self.args.worker_id, 'pid': os.getpid(), 'pgid': os.getpgrp(),
                    'swift_pid': self.args.swift_pid, 'inputs_sha256': self.input_sha}
        require(all(integer(permit.get(k)) for k in ['v', 'pid', 'pgid', 'swift_pid']) and
                all(permit.get(k) == value for k, value in expected.items()) and
                type(permit.get('permit_id')) is str and bool(permit['permit_id']), 'invalid permit')
        self.phase = 'load-permitted'
        self.backend = self.factory(self.config)
        self.check_partition(self.snapshots())
        self.phase = 'ready'
        self.emit('READY', backend=self.backend.name, layers=28, page_size=128, vocab_size=151936,
                  pools=[{'layer': i, 'pool_id': self.pool_id(i), 'pool_object_id': str(id(pool))}
                         for i, pool in enumerate(self.backend.pools)],
                  provenance=self.provenance())

    def command(self):
        cmd = self.transport.read()
        require(integer(cmd.get('v')) and cmd.get('v') == 1 and cmd.get('session') == self.args.session and
                cmd.get('worker_id') == self.args.worker_id, 'command identity mismatch')
        require(integer(cmd.get('seq')) and cmd['seq'] == self.sequence + 1, 'command sequence mismatch')
        require(cmd.get('kind') in ['EVAL', 'RELEASE', 'SHUTDOWN'], 'unknown command')
        base = {'v', 'kind', 'session', 'worker_id', 'seq'}
        identity = {'request_id', 'lease_epoch', 'operation', 'purpose', 'source_context', 'native_context'}
        fields = base if cmd['kind'] == 'SHUTDOWN' else base | identity | (
            {'token_ids', 'input_sha256'} if cmd['kind'] == 'EVAL' else {'receipt_id', 'eval_seq'})
        require(set(cmd) == fields, 'exact command fields required')
        self.sequence = cmd['seq']
        self.current = cmd
        return cmd

    def evaluate(self, cmd):
        require(self.active is None, 'EVAL while a prior lease is live')
        tokens = cmd.get('token_ids')
        count = {'Prefill32': 32, 'Prefill8': 8}.get(cmd.get('operation'))
        # Reject before graph construction, cache creation, or KV allocation.
        require(count is not None and type(tokens) is list and len(tokens) == count and
                all(integer(t) and 0 <= t < 151936 for t in tokens), 'operation/token shape or range')
        require(cmd.get('input_sha256') == token_hash(tokens), 'actual token digest mismatch')
        require(type(cmd.get('request_id')) is str and bool(cmd['request_id']) and
                cmd['request_id'] not in self.request_ids, 'fresh request identity required')
        require(integer(cmd.get('lease_epoch')) and cmd['lease_epoch'] == self.last_lease + 1,
                'wire lease must be fresh monotonic counter')
        if cmd.get('purpose') == 'checked-source':
            context = cmd.get('source_context')
            require(type(context) is dict and cmd.get('native_context') is None,
                    'checked request context')
            require(context.get('command') == cmd['request_id'] and context.get('operation') == cmd['operation'] and
                    context.get('tokens') == tokens and context.get('input_sha256') == cmd['input_sha256'],
                    'checked request echo mismatch')
            require(all(type(context.get(k)) is str and bool(context[k]) for k in
                        ['owner', 'source_epoch', 'scope', 'backend_return', 'job', 'task', 'invocation', 'region', 'access', 'site']),
                    'checked source identity fields')
            require(integer(context.get('owner_ordinal')) and context['owner_ordinal'] > 0,
                    'source owner ordinal')
        else:
            context = cmd.get('native_context')
            require(cmd.get('purpose') == 'native-reference' and cmd.get('source_context') is None and
                    type(context) is dict and set(context) == {'request_id', 'task_id', 'purpose'} and
                    context.get('purpose') == 'native-reference' and context.get('request_id') == cmd['request_id'] and
                    type(context.get('task_id')) is str and bool(context['task_id']), 'native reference identity')
        self.check_partition(self.snapshots())
        self.claimed += 1
        self.last_lease = cmd['lease_epoch']
        self.request_ids.add(cmd['request_id'])
        self.phase = 'graph'
        self.active = {'request_id': cmd['request_id'], 'lease_epoch': cmd['lease_epoch'],
                       'operation': cmd['operation'], 'purpose': cmd['purpose'],
                       'source_context': cmd['source_context'], 'native_context': cmd['native_context'],
                       'eval_seq': cmd['seq'], 'cache': self.backend.create_cache(),
                       'epochs': [{} for _ in range(28)], 'token_count': len(tokens),
                       'receipt': None}
        token = self.backend.build(tokens, self.active['cache'])
        for layer, cache in enumerate(self.active['cache']):
            for page in cache.page_ids:
                key = (layer, page)
                self.page_epochs[key] = self.page_epochs.get(key, 0) + 1
                self.active['epochs'][layer][page] = self.page_epochs[key]
        snapshots = self.snapshots()
        self.check_partition(snapshots, active=True)
        self.phase = 'eval'
        self.emit('EVAL_ENTER', input_sha256=cmd['input_sha256'], token_count=len(tokens),
                  layers=snapshots)
        if self.scenario == 'eof_after_enter':
            os._exit(72)
        if self.scenario == 'timeout_after_enter':
            while True:
                select.select([], [], [], 60)
        begin = time.monotonic_ns()
        self.backend.materialize(token)
        end = time.monotonic_ns()
        token_id = self.backend.item(token)
        item_end = time.monotonic_ns()
        require(integer(token_id) and 0 <= token_id < 151936, 'invalid token')
        snapshots = self.snapshots()
        self.check_partition(snapshots, active=True)
        self.phase = 'sealed'
        receipt = {'receipt_id': f'{self.args.session}/{self.args.worker_id}/eval/{cmd["seq"]}',
                   'input_sha256': cmd['input_sha256'], 'token_count': len(tokens),
                   'token_id': token_id, 'eval_begin_ns': begin, 'eval_end_ns': end,
                   'item_end_ns': item_end, 'sealed': True, 'layers': snapshots}
        self.active['receipt'] = receipt
        self.emit('EVAL_RECEIPT', **receipt)
        if self.scenario == 'nonzero_after_receipt':
            os._exit(75)

    def release(self, cmd):
        self.service_mark('release.enter', cmd)
        require(self.active is not None and self.active['receipt'] is not None, 'release without sealed receipt')
        active, receipt = self.active, self.active['receipt']
        require(all(cmd.get(k) == active[k] for k in ['request_id', 'lease_epoch', 'eval_seq',
                    'operation', 'purpose', 'source_context', 'native_context']) and
                cmd.get('receipt_id') == receipt['receipt_id'], 'release identity mismatch')
        require(integer(cmd.get('lease_epoch')) and integer(cmd.get('eval_seq')), 'invalid release integer')
        before = self.snapshots()
        require(before == receipt['layers'], 'active pages changed since receipt')
        self.phase = 'release'
        for layer in range(28):
            if layer == 1 and self.scenario == 'partial_release':
                raise RuntimeError('injected stub failure after layer 0 release')
            self.backend.release_layer(active['cache'], layer)
        after = self.snapshots()
        self.check_partition(after)
        self.service_mark('pools.return_verified', cmd)
        rows = [{'layer': i, 'pool_id': self.pool_id(i), 'before': before[i],
                 'released_pages': before[i]['owned_pages'], 'after': after[i]} for i in range(28)]
        self.active = None
        self.completed += 1
        self.phase = 'ready'
        if self.scenario == 'lost_release_ack':
            os._exit(74)
        self.emit('RELEASE_ACK', receipt_id=receipt['receipt_id'], eval_seq=active['eval_seq'],
                  released=True, layers=rows)
        if self.scenario == 'nonzero_after_release_ack':
            os._exit(76)

    def run(self):
        try:
            self.bootstrap()
            while True:
                cmd = self.command()
                if cmd['kind'] == 'EVAL':
                    self.evaluate(cmd)
                elif cmd['kind'] == 'RELEASE':
                    self.release(cmd)
                else:
                    require(self.active is None and self.completed == self.claimed,
                            'shutdown before releases complete')
                    self.check_partition(self.snapshots())
                    provenance = self.provenance(final=True)
                    self.emit('SHUTDOWN_ACK', all_released=True, completed_evaluations=self.completed,
                              pools=self.snapshots(), provenance=provenance)
                    self.service_flush('shutdown')
                    return 0
        except Exception as exc:
            active = None if self.active is None else {k: self.active[k]
                for k in ['request_id', 'lease_epoch', 'eval_seq', 'token_count']}
            try:
                snapshots = self.snapshots()
            except Exception as snapshot_error:
                snapshots = [{'snapshot_error': repr(snapshot_error)}]
            try:
                self.emit('ERROR', fault_id=f'{self.args.session}/{self.args.worker_id}/error/{self.events + 1}',
                          code=type(exc).__name__, message=str(exc), phase=self.phase,
                          unresolved=True, active_request=active, partial_layers=snapshots,
                          traceback=traceback.format_exc())
            except Exception:
                traceback.print_exc(file=sys.stderr)
            # No release/reset/synchronize fallback; unresolved roots remain in the saved prefix.
            self.service_flush('caught-failure')
            return 1


def arguments(stub=False):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inputs', required=True)
    p.add_argument('--session', required=True)
    p.add_argument('--worker-id', required=True)
    p.add_argument('--swift-pid', type=int, required=True)
    p.add_argument('--pgid', type=int, required=True)
    if stub:
        p.add_argument('--scenario', default='success')
    args = p.parse_args()
    require(Path(args.inputs).is_absolute(), 'inputs must be absolute')
    return args


if __name__ == '__main__':
    sys.exit(Worker(arguments()).run())
