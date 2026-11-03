#!/usr/bin/env python3
"""SRC1 B decoder data preparation only; never starts a compiler or decoder.

sources imports one pinned pure builder. mutations imports the independently
reviewed, pinned 15-case pure oracle only after verifying four actual exports.
Root supplies bounded execution and preserves actual outer tool exits.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

PYTHON = '/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/bin/python3.14'
PINS = {
    'frontend/parent_fixture.py': (2869, '7003a59b29f8ee326181ee8eacd3b0212330522b453deade87947c63481436b8'),
    'frontend/parent_decoder_mutations.py': (6539, '90dd52dae21c9383ea23638a8d0cea777fe3c5a6d2917b09ef04325ecaad01d5'),
    'frontend/export_parent.py': (940, '28b67fc423ffdaba9cebb36071d8e496f4558507e1658ec3ec5528d37153779b'),
    'frontend/export.py': (925, '41b5068e07088de80244726aec42c49f4840e0fc38f6d26c40e3667821c7913c'),
    'Sources/RuntimeCore/Program.swift': (63836, 'e7b982727e272bf34bece17c8554427a3ba6adfb51bf57ac6be0772272ff90c9'),
}
CASES = (
    ('unknown-profile', 'program version'),
    ('wrong-sigma-id', 'fixed Sigma implementations'),
    ('source-bytes-hash', 'exact source bytes'),
    ('service-result-unit', 'parent fieldless Word contract'),
    ('service-tokens-seven', 'parent complete Pair contract'),
    ('tool-argument-nat', 'parent fieldless Word contract'),
    ('tool-result-word-length', 'parent fieldless Nat contract'),
    ('scope-kind', 'parent plain Scope kind'),
    ('scope-identity', 'parent current scope nominal correspondence'),
    ('service-effect-literal', 'parent HostService fixed literal'),
    ('service-effect-scope', 'parent current scope nominal correspondence'),
    ('tool-capability', 'parent LocalRead fixed literal'),
    ('missing-raise', 'parent exact effect arity'),
    ('extra-effect', 'parent exact effect arity'),
    ('command-direct-copy', 'source-directed direct usage'),
)
EXCHANGES = (('parent-golden', 'golden', 'parent'), ('parent-empty', 'empty', 'parent'),
             ('parent-service-only', 'service-only', 'parent'), ('child-empty', 'empty', 'child'))
BUDGETS = {'outer': 60, 'work': 58, 'term': 1, 'kill_reap': 0.5, 'report_reserve': 0.5}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def strict(data):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, 'duplicate JSON key: ' + key)
            result[key] = value
        return result
    def reject(value):
        raise ValueError('nonfinite JSON token: ' + value)
    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=reject)


def diagnostic(message):
    return {'phase': 'protocol', 'code': 'invalid-compiler-exchange', 'path': [], 'message': message}


def differences(left, right, path=()):
    if type(left) is type(right) and left == right:
        return []
    if type(left) is dict and type(right) is dict:
        keys = sorted(left.keys() | right.keys())
    elif type(left) is list and type(right) is list:
        keys = range(max(len(left), len(right)))
    else:
        keys = None
    if keys is None:
        return [{'pointer': '/' + '/'.join(str(x).replace('~', '~0').replace('/', '~1') for x in path),
                 'old_present': True, 'new_present': True, 'old': left, 'new': right}]
    rows = []
    for key in keys:
        old = key in left if type(left) is dict else key < len(left)
        new = key in right if type(right) is dict else key < len(right)
        if old and new:
            rows.extend(differences(left[key], right[key], path + (key,)))
        else:
            rows.append({'pointer': '/' + '/'.join(str(x).replace('~', '~0').replace('/', '~1') for x in path + (key,)),
                         'old_present': old, 'new_present': new,
                         'old': left[key] if old else None, 'new': right[key] if new else None})
    return rows


class Preparation:
    def __init__(self, root, output):
        self.root, self.output = root, output
        self.inputs, self.completed = {}, []
        self.current = 'input-preflight'

    def read(self, path):
        require(path.is_absolute() and path.is_file() and not path.is_symlink(), 'absolute regular input: ' + str(path))
        data = path.read_bytes()
        record = {'path': str(path), 'bytes': len(data), 'sha256': sha(data)}
        require(str(path) not in self.inputs or self.inputs[str(path)] == record, 'changed input: ' + str(path))
        self.inputs[str(path)] = record
        return data

    def pinned(self, record):
        require(set(record) == {'path', 'bytes', 'sha256'}, 'exact pin fields')
        data = self.read(Path(record['path']))
        require(self.inputs[record['path']] == record, 'input pin: ' + record['path'])
        return data

    def write(self, path, data):
        with path.open('xb') as stream:
            stream.write(data)
        return {'path': str(path), 'bytes': len(data), 'sha256': sha(data)}

    def save(self, path, value):
        return self.write(path, encoded(value) + b'\n')

    def recheck(self):
        for path, record in sorted(self.inputs.items()):
            data = Path(path).read_bytes()
            require(len(data) == record['bytes'] and sha(data) == record['sha256'], 'post input changed: ' + path)

    def fixed_inputs(self):
        for relative, expected in PINS.items():
            data = self.read(self.root / relative)
            require((len(data), sha(data)) == expected, 'pinned source basis: ' + relative)
        self.read(Path(__file__).resolve())

    def module(self, relative, name):
        # Only fixed, previously pinned pure helpers; never frontend/exporter.
        spec = importlib.util.spec_from_file_location(name, self.root / relative)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def common(self):
        return {'constructor': self.inputs[str(Path(__file__).resolve())],
                'program_source_basis': self.inputs[str(self.root / 'Sources/RuntimeCore/Program.swift')],
                'source_root': str(self.root), 'decoder_executed': False,
                'compiler_started': False, 'source_runtime_executed': False, 'model_loaded': False}

    def sources(self):
        f = self.module('frontend/parent_fixture.py', 'src1b_pinned_parent_data')
        self.current = 'source-construction'
        empty = {'schema': 'p8-runtime-closed-source-v0.1', 'main': {'result': f.t('Unit'), 'effect': f.eff(), 'body': f.ret()}}
        service_body = f.c('let_e', name='requestTokens', expression={'expression': 'value', 'value': f.tokens_value()}, next=f.service())
        service_only = f.in_scope(service_body, f.eff(f.atom('HostService', scope='ParentS', service='B'), f.atom('Raise')), f.t('Word'))
        for name, value in [('golden', f.source()), ('empty', empty), ('service-only', service_only)]:
            self.current = name
            self.completed.append({'id': name, 'source': self.write(self.output / (name + '.json'), encoded(value) + b'\n')})
        self.recheck()
        record = dict(self.common(), schema='src1b-decoder-sources-v1', construction_complete=True,
                      sources=self.completed, inputs=list(self.inputs.values()))
        self.save(self.output / 'manifest.json', record)

    def mutations(self, source_manifest_path, index_path):
        sources = strict(self.read(source_manifest_path))
        require(sources['schema'] == 'src1b-decoder-sources-v1' and sources['construction_complete'] is True, 'source manifest complete')
        require(sources['source_root'] == str(self.root) and sources['program_source_basis'] == self.common()['program_source_basis'], 'same source/program basis')
        for item in sources['inputs']:
            self.pinned(item)
        require([x['id'] for x in sources['sources']] == ['golden', 'empty', 'service-only'], 'fixed source population')
        source_rows = {x['id']: x['source'] for x in sources['sources']}
        index = strict(self.read(index_path))
        require(set(index) == {'schema', 'source_root', 'source_manifest', 'exchanges'}, 'exchange index fields')
        require(index['schema'] == 'src1b-decoder-exchange-index-v1' and index['source_root'] == str(self.root), 'exchange index schema/root')
        require(index['source_manifest'] == self.inputs[str(source_manifest_path)], 'source manifest identity')
        require([x['id'] for x in index['exchanges']] == [x[0] for x in EXCHANGES], 'four actual exchange order')
        originals, exchanges = {}, []
        for row, (name, source_name, exporter) in zip(index['exchanges'], EXCHANGES):
            self.current = name
            require(set(row) == {'id', 'exporter', 'source', 'response', 'stderr', 'run', 'actual_tool'}, 'exchange row fields')
            require(row['exporter'] == exporter and row['source'] == source_rows[source_name], 'fixed source/exporter selection')
            source, response, stderr = (self.pinned(row[k]) for k in ('source', 'response', 'stderr'))
            run, tool = (strict(self.pinned(row[k])) for k in ('run', 'actual_tool'))
            script = self.root / 'frontend' / ('export_parent.py' if exporter == 'parent' else 'export.py')
            command = [PYTHON, '-I', '-S', '-B', str(script)]
            require(run['schema'] == 'src1b-decoder-frontend-export-run-v1' and run['entry'] == name, 'actual frontend run identity')
            require(run['command'] == command and run['cwd'] == str(self.root) and run['source'] == row['source'], 'actual compiler command/stdin')
            require(run['files']['stdout.log'] == row['response'] and run['files']['stderr.log'] == row['stderr'], 'actual raw stdio identity')
            require(type(run['returncode']) is int and run['returncode'] == 0 and run['passed'] is True and run['direct_child_reaped'] is True and run['process_group_absent'] is True, 'actual ready compiler exit0')
            require(run['timed_out'] is False and run['interrupted'] == [] and run['signals'] == [] and run['failure'] is None, 'clean compiler terminal')
            require(run['budgets_seconds'] == BUDGETS and 0 <= run['work_elapsed_seconds'] < 58 and run['work_elapsed_seconds'] <= run['elapsed_seconds'] < 60, 'compiler work/total budget')
            require(run['outer_zero_requires_post_save_post_flush_total_budget_gate'] is True, 'compiler final gate contract')
            require(tool['schema'] == 'src1b-decoder-frontend-export-actual-tool-v1' and tool['entry'] == name and tool['run'] == row['run'] and tool['child_command'] == command, 'actual tool/run binding')
            require(type(tool['observation']['exit_code']) is int and tool['observation']['exit_code'] == 0, 'actual outer exit0, not provisional passed')
            ready = strict(response)
            profile = 'src1b-parent-host-close-v1' if exporter == 'parent' else 'src1-response-offer-owner1-v1'
            require(ready['schema'] == 'p8-runtime-frontend-response-v1' and ready['status'] == 'ready' and ready['diagnostic'] is None and ready['program']['profile'] == profile, 'actual ready profile')
            embedded = ready['program']['source']
            require(base64.b64decode(embedded['utf8_base64'], validate=True) == source and embedded['bytes_sha256'] == sha(source), 'actual exported source bytes')
            directory = self.output / name
            directory.mkdir()
            copies = {key: self.write(directory / filename, data) for key, filename, data in
                      [('source', 'source.json', source), ('response', 'frontend.stdout', response), ('stderr', 'frontend.stderr', stderr)]}
            exchanges.append(dict(row, original_copies=copies, actual_compiler_exit_supplied=0))
            originals[name] = (source, response)
        oracle = self.module('frontend/parent_decoder_mutations.py', 'src1b_pinned_parent_oracle')
        require(oracle.CASES == CASES, 'fixed reviewed mutation oracle')
        source, response = originals['parent-golden']
        original = strict(response)
        cases = oracle.build_cases(source, response, 0)
        require([(x['id'], x['expected_message']) for x in cases] == list(CASES), 'fixed 15 mutation population')
        for case, (name, message) in zip(cases, CASES):
            self.current = name
            changed = case['response']
            old_table, new_table = original['program']['static_values'], changed['program']['static_values']
            require(new_table[:len(old_table)] == old_table, 'original shared static prefix unchanged')
            changes = differences(original, changed)
            require(changes, 'negative must change actual response')
            directory = self.output / name
            directory.mkdir()
            source_pin = self.write(directory / 'source.json', source)
            response_pin = self.write(directory / 'frontend.stdout', encoded(changed) + b'\n')
            metadata = {'schema': 'src1b-parent-decoder-mutation-v1', 'id': name,
                        'source': source_pin, 'response': response_pin,
                        'expected_diagnostic': diagnostic(message), 'input_origin': case['input_origin'],
                        'actual_compiler_exit_supplied': 0, 'source_rewritten': False, 'decoder_executed': False,
                        'wire_changes': changes, 'wire_changed_count': len(changes),
                        'original_static_count': len(old_table), 'appended_static_count': len(new_table) - len(old_table),
                        'original_static_prefix_unchanged': True}
            metadata_pin = self.save(directory / 'mutation.json', metadata)
            self.completed.append({'id': name, 'source': source_pin, 'response': response_pin,
                                   'metadata': metadata_pin, 'expected_diagnostic': diagnostic(message)})
        self.recheck()
        record = dict(self.common(), schema='src1b-parent-decoder-inputs-v1', construction_complete=True,
                      source_manifest=self.inputs[str(source_manifest_path)], exchange_index=self.inputs[str(index_path)],
                      oracle=self.inputs[str(self.root / 'frontend/parent_decoder_mutations.py')],
                      exchanges=exchanges, mutations=self.completed, mutation_count=15,
                      observation_count=20, configuration_calls=5, inputs=list(self.inputs.values()))
        self.save(self.output / 'manifest.json', record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('sources', 'mutations'))
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--source-manifest', type=Path)
    parser.add_argument('--exchange-index', type=Path)
    args = parser.parse_args()
    paths = [args.source_root, args.output_dir] + [p for p in (args.source_manifest, args.exchange_index) if p is not None]
    require(all(p.is_absolute() for p in paths), 'absolute paths required')
    require((args.mode == 'mutations') == (args.source_manifest is not None and args.exchange_index is not None), 'mutations requires both original source manifest and actual exchange index')
    require(args.mode != 'sources' or (args.source_manifest is None and args.exchange_index is None), 'sources has no actual exchange inputs')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    work = Preparation(args.source_root.resolve(strict=True), args.output_dir)
    try:
        work.fixed_inputs()
        if args.mode == 'sources':
            work.sources()
        else:
            work.mutations(args.source_manifest, args.exchange_index)
        print(json.dumps({'construction_complete': True, 'mode': args.mode, 'output_dir': str(args.output_dir)}), flush=True)
        return 0
    except BaseException as error:
        failure = dict(work.common() if str(Path(__file__).resolve()) in work.inputs and str(work.root / 'Sources/RuntimeCore/Program.swift') in work.inputs else {},
                       schema='src1b-decoder-preparation-failure-v1', construction_complete=False,
                       first_failure_case=work.current, error_type=type(error).__name__, error=str(error),
                       completed_prefix=work.completed, inputs=list(work.inputs.values()))
        try:
            work.save(args.output_dir / 'first-failure.json', failure)
        except BaseException as save_error:
            print('first-failure save error: ' + repr(save_error), file=sys.stderr, flush=True)
        raise


if __name__ == '__main__':
    raise SystemExit(main())
