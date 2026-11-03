#!/usr/bin/env python3
"""Pure fixed native tool-boundary sources; no checker/runtime/model imports.

Each parent first consumes the actual service response, then calls the tool
with its fixed native-case literal. These sources are unchecked until their
separate actual frontend exports are executed and retained.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPERS = (
    ('src1b_native_parent_source', 'frontend/parent_fixture.py',
     '7003a59b29f8ee326181ee8eacd3b0212330522b453deade87947c63481436b8'),
    ('src1b_native_child_source', 'programs/build_source_response_programs.py',
     '541d1e807f9717bd4c4fc9832860c472af6bce6118bb856352a9d2c9baec8ee9'),
)


def load_pure(name, relative, expected):
    path = ROOT / relative
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError('fixed native pure helper changed: ' + relative)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, {'path': str(path), 'bytes': len(data), 'sha256': expected}


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if not args.output_dir.is_absolute():
        parser.error('absolute exclusive output directory required')
    output = args.output_dir.resolve(strict=False)
    if output.is_relative_to(ROOT) or ROOT.is_relative_to(output):
        parser.error('source and output roots must be disjoint')
    parent, parent_pin = load_pure(*HELPERS[0])
    child, child_pin = load_pure(*HELPERS[1])
    sources, cases = [], []
    for number in range(1, 15):
        case_id = 'B-T%02d' % number
        token = {2: 1, 3: 151643, 4: 151669, 5: 151936}.get(number, 0)
        source = parent.source()
        parent.tool_node(source)['argument'] = parent.word(token)
        name = 'parent-' + case_id
        sources.append((name, source, 'hostParent'))
        code = {3: 'ToolAddedTokenUnsupported', 4: 'ToolTokenMissing', 5: 'ToolArgumentOutOfRange',
                6: 'ToolOpenFailure', 7: 'ToolReadFailure', 8: 'ToolSchemaFailure',
                9: 'ToolDuplicateMapping', 10: 'ToolDuplicateMapping', 11: 'ToolAuthorityMismatch',
                12: 'ToolCloseUnknown', 13: 'ToolBudgetExceeded', 14: 'ToolBudgetExceeded'}.get(number)
        cases.append({'case_id': case_id, 'parent': name, 'child': 'child-unit_after_b',
            'tool_argument_decimal': str(token), 'worker_scenario': 'success',
            'expected_parent_exit': 0 if number in (1, 2) else 2 if number in (11, 12) else 1,
            'expected_tool_code': code,
            'native_scope': 'real checked response consumption followed by fixed literal tool argument',
            'claims_response_token_as_tool_argument': False})
    sources.append(('child-unit_after_b', child.source('unit_after_b'), 'mlxLeaf'))
    args.output_dir.mkdir(parents=True, exist_ok=False)
    records = []
    for name, raw, domain in sources:
        data = (json.dumps(raw, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        filename = name + '.json'
        with (args.output_dir / filename).open('xb') as output_file:
            output_file.write(data)
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        records.append({'name': name, 'file': filename, 'domain': domain, 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'canonical_sha256': hashlib.sha256(canonical).hexdigest()})
    record = {'schema': 'src1b-parent-native-tool-sources-v1', 'source_root': str(ROOT),
        'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'helpers': [parent_pin, child_pin], 'checked': False, 'runtime_started': False,
        'model_execution': False, 'sources': records, 'cases': cases,
        'scope': 'Native tool IO boundaries only; original B-C01...B-C10 are separate. Actual source checking, real worker and closure evidence must be obtained separately.'}
    with (args.output_dir / 'manifest.json').open('x') as output_file:
        json.dump(record, output_file, ensure_ascii=False, indent=2)
        output_file.write('\n')
    print(json.dumps({'output_dir': str(args.output_dir), 'sources': len(records),
                      'cases': len(cases), 'checked': False, 'runtime_started': False}))


if __name__ == '__main__':
    main()
