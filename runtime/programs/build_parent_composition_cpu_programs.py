#!/usr/bin/env python3
"""Pure fixed SRC1 B CPU source construction; no checking or runtime imports."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_pure(name, relative, expected):
    path = ROOT / relative
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError('fixed pure source helper changed: ' + relative)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    parent = load_pure('src1b_pure_parent', 'frontend/parent_fixture.py',
                      '7003a59b29f8ee326181ee8eacd3b0212330522b453deade87947c63481436b8')
    child = load_pure('src1b_pure_child', 'programs/build_source_response_programs.py',
                     '541d1e807f9717bd4c4fc9832860c472af6bce6118bb856352a9d2c9baec8ee9')
    sources = [('parent', parent.source(), 'hostParent')]
    sources.extend((name, child.source(name), 'mlxLeaf') for name in
                   ('unit_after_b', 'source_failure_after_offer', 'missing_intent'))
    args.output_dir.mkdir(parents=True, exist_ok=False)
    records = []
    for name, raw, domain in sources:
        encoded = (json.dumps(raw, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        filename = name + '.json'
        with (args.output_dir / filename).open('xb') as stream:
            stream.write(encoded)
        records.append({'name': name, 'file': filename, 'domain': domain,
                        'bytes': len(encoded), 'sha256': hashlib.sha256(encoded).hexdigest(),
                        'canonical_sha256': hashlib.sha256(json.dumps(raw, ensure_ascii=False,
                            sort_keys=True, separators=(',', ':')).encode()).hexdigest()})
    fixtures = []
    for number in range(1, 11):
        name = 'B-C%02d' % number
        fixtures.append({'fixture': name, 'parent': 'parent',
            'child': {6: 'source_failure_after_offer', 7: 'missing_intent'}.get(number, 'unit_after_b'),
            'worker_scenario': {3: 'src1b_added_token', 4: 'src1b_missing_token'}.get(number, 'success'),
            'parent_tokens': list(parent.B8),
            'expected_cpu_b': {3: 151643, 4: 151669}.get(number, sum(parent.B8) % 151936),
            'expected_parent_exit': 124 if number == 9 else 2 if number in (8, 10) else 1 if number in (3, 4, 5, 6, 7) else 0,
            'expected_child_exit': 124 if number == 9 else 2 if number in (7, 8) else 1 if number == 6 else 0,
            'close_gate': number != 2, 'cached_parent_wait': number == 2,
            'native_control': True, 'model_execution': False})
    record = {'schema': 'src1b-parent-cpu-source-generation-v1',
        'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'checked': False, 'runtime_started': False, 'sources': records, 'fixtures': fixtures,
        'scope': 'Pure sources only; real compiler, worker, IO, response and closure must be observed separately.'}
    with (args.output_dir / 'manifest.json').open('x') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'output_dir': str(args.output_dir), 'sources': len(records),
                      'fixtures': len(fixtures), 'checked': False, 'runtime_started': False}))


if __name__ == '__main__':
    main()
