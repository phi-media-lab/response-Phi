#!/usr/bin/env python3
"""Prepare six decode smokes or a balanced matrix from a frozen source attempt.

Standard library only. No model import, process execution or GPU work. External
file identities are inherited, not rehashed here; each actual execution retains
the frozen attempt's full pre/post external-byte checks.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import random
import shutil
import traceback

POLICIES = ('no-b', 'whole', 'fixed-32', 'fixed-128', 'fixed-512', 'cost-200')
SEED = 20261029


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''): result.update(block)
    return result.hexdigest()


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def schedule(mode):
    if mode == 'smoke':
        return [dict(cell_id=policy, block=0, design_row=None, position=i, policy=policy,
                     trials=1, warmup=0) for i, policy in enumerate(POLICIES)]
    if mode != 'matrix': raise ValueError('unknown batch mode')
    rows = list(range(6))
    random.Random(SEED).shuffle(rows)
    first = (0, 1, 5, 2, 4, 3)
    return [dict(cell_id=f'block-{block:02d}-position-{position:02d}-{POLICIES[(n+row)%6]}',
                 block=block, design_row=row, position=position, policy=POLICIES[(n+row)%6],
                 trials=1, warmup=1)
            for block, row in enumerate(rows) for position, n in enumerate(first)]


def verify_source(template):
    manifest_path = template/'manifest.json'
    manifest_bytes = manifest_path.read_bytes()
    identity = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    prepared = json.loads((template/'prepared.json').read_text())
    if (prepared.get('manifest_sha256') != identity or prepared.get('model_or_gpu_loaded') is not False
            or manifest.get('kind') != 'tiny-decode-cost-attempt' or manifest.get('schema_version') != 1):
        raise ValueError('invalid template identity or kind')
    payload = template/'payload'
    files = manifest['payload_files']
    inventory = list(payload.rglob('*'))
    if payload.is_symlink() or any(p.is_symlink() for p in inventory):
        raise ValueError('template contains a symlink')
    if {str(p.relative_to(payload)) for p in inventory if p.is_file()} != set(files):
        raise ValueError('template inventory changed')
    for relative, row in files.items():
        p = payload/relative
        if (Path(relative).is_absolute() or '..' in Path(relative).parts or p.is_symlink()
                or not p.resolve().is_relative_to(payload.resolve()) or p.stat().st_size != row['bytes']
                or sha(p) != row['sha256']):
            raise ValueError('template source changed: '+relative)
    for name in ('attempt.py', 'run.py', 'protocol.py', 'fixture.json', 'generated_runtime.py'):
        if 'experiments/tiny-decode-cost/'+name not in files:
            raise ValueError('required frozen source missing: '+name)
    if not manifest.get('generated_runtime_templates') or not manifest.get('external_dependencies'):
        raise ValueError('template must contain generated/runtime bindings')
    return manifest, identity


def validate_shape(shape, fixture):
    for name, limit in [('a_prompt_length', 1024), ('b_prompt_length', 4096), ('a_decode_steps', 40)]:
        if type(shape.get(name)) is not int or not 1 <= shape[name] <= limit:
            raise ValueError('invalid '+name)
    anchor = shape.get('arrival_after_a_decode')
    if type(anchor) is not int or not 1 <= anchor < shape['a_decode_steps']:
        raise ValueError('invalid arrival anchor')
    if (len(fixture['a_token_ids']) < shape['a_prompt_length']
            or len(fixture['b_token_ids']) < shape['b_prompt_length']):
        raise ValueError('literal fixture does not cover shape')


def prepare(template, destination, mode, shape):
    template, destination = Path(template).resolve(), Path(destination).resolve()
    original, identity = verify_source(template)
    fixture = json.loads((template/'payload/experiments/tiny-decode-cost/fixture.json').read_text())
    validate_shape(shape, fixture)
    if destination == template or destination.is_relative_to(template):
        raise ValueError('batch cannot be created inside its source attempt')
    destination.mkdir(parents=True, exist_ok=False)
    cells = schedule(mode)
    driver = Path(__file__).resolve()
    try:
        # Retain the original manifest bytes so a moved batch can independently
        # reconstruct each derivation without reading the original absolute path.
        shutil.copyfile(template/'manifest.json', destination/'template-manifest.json')
        shutil.copyfile(template/'prepared.json', destination/'template-prepared.json')
        if sha(destination/'template-manifest.json') != identity:
            raise ValueError('template manifest changed before archival')
        if json.loads((destination/'template-prepared.json').read_text())['manifest_sha256'] != identity:
            raise ValueError('template prepared identity changed before archival')
        shutil.copyfile(driver, destination/'prepare-driver.py')
        if sha(driver) != sha(destination/'prepare-driver.py'): raise ValueError('prepare driver changed while copying')
        plan = dict(schema_version=1, kind='tiny-decode-cost-batch', mode=mode, shape=shape,
            schedule_seed=SEED if mode == 'matrix' else None, cells=cells,
            template=dict(path=str(template), manifest_sha256=identity,
                          captured_at=original['created_at'],
                          archived_manifest='template-manifest.json',
                          archived_prepared='template-prepared.json',
                          prepared_sha256=sha(destination/'template-prepared.json')),
            prepare_driver_sha256=sha(destination/'prepare-driver.py'),
            external_full_hashes_checked_at_batch_preparation=False,
            external_identity_scope='Inherited from the identified template; actual execute checks full bytes before and after.',
            model_or_gpu_loaded=False,
            shape_scope='One shape/anchor only; the full eight-combination plan remains separate.')
        save(destination/'plan.json', plan)
        plan_sha = sha(destination/'plan.json')
        rows = []
        for cell in cells:
            target = destination/cell['cell_id']
            target.mkdir(exist_ok=False)
            shutil.copytree(template/'payload', target/'payload', copy_function=shutil.copyfile)
            # Verify every copied file; never edit the template or use hardlinks.
            if {str(p.relative_to(target/'payload')) for p in (target/'payload').rglob('*') if p.is_file()} != set(original['payload_files']):
                raise ValueError('copied payload inventory differs')
            for relative, row in original['payload_files'].items():
                p = target/'payload'/relative
                if sha(p) != row['sha256'] or p.stat().st_size != row['bytes']:
                    raise ValueError('copied payload differs: '+relative)
            manifest = json.loads(json.dumps(original))
            manifest['created_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            manifest['run_spec'] = dict(shape, policy=cell['policy'], trials=cell['trials'],
                                        warmup=cell['warmup'], model=original['run_spec']['model'])
            manifest['batch_derivation'] = dict(template_manifest_sha256=identity,
                template_path=str(template), original_capture_time=original['created_at'],
                plan_sha256=plan_sha, cell=cell,
                external_bytes_rehashed_during_clone=False)
            manifest['scope'].append('This attempt copies an identified frozen template. External capture rows are inherited; execution still verifies full external bytes before and after.')
            save(target/'manifest.json', manifest)
            manifest_sha = sha(target/'manifest.json')
            save(target/'prepared.json', dict(manifest_sha256=manifest_sha,
                payload_files=len(manifest['payload_files']), model_or_gpu_loaded=False,
                next_action='root-scheduled execution after the preceding correctness gate'))
            for p in (target/'payload').rglob('*'):
                if p.is_file(): p.chmod(0o444)
            for p in sorted((p for p in (target/'payload').rglob('*') if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True): p.chmod(0o555)
            (target/'payload').chmod(0o555)
            (target/'manifest.json').chmod(0o444); (target/'prepared.json').chmod(0o444)
            rows.append(dict(cell, path=str(target), manifest_sha256=manifest_sha,
                execution_exists=False,
                execute_argv=[original['runtime']['executable'], '-B',
                    str(target/'payload/experiments/tiny-decode-cost/attempt.py'), 'execute',
                    '--attempt', str(target), '--timeout', '300']))
        _, after = verify_source(template)
        if after != identity: raise ValueError('template manifest changed while preparing batch')
        result = dict(passed=True, plan_sha256=plan_sha, cells=rows, template_verified_before_and_after=True,
            model_or_gpu_loaded=False, external_full_hashes_checked=False,
            execution_order='Exactly the saved cells order; stop at the first failed or unknown attempt; no automatic retry.')
        save(destination/'prepared-index.json', result)
        (destination/'plan.json').chmod(0o444); (destination/'prepare-driver.py').chmod(0o444)
        (destination/'template-manifest.json').chmod(0o444); (destination/'template-prepared.json').chmod(0o444)
        return result
    except BaseException:
        save(destination/'preparation-error.json', dict(passed=False, error=traceback.format_exc(),
                                                      model_or_gpu_loaded=False))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--mode', choices=('smoke', 'matrix'), required=True)
    for key, default in [('a-prompt-length',64), ('b-prompt-length',1024), ('a-decode-steps',40), ('arrival-after-a-decode',4)]:
        parser.add_argument('--'+key, type=int, default=default)
    args = parser.parse_args()
    shape = {key:getattr(args,key) for key in ('a_prompt_length','b_prompt_length','a_decode_steps','arrival_after_a_decode')}
    result = prepare(args.template, args.output_dir, args.mode, shape)
    print(json.dumps(dict(passed=result['passed'], cells=len(result['cells']), plan_sha256=result['plan_sha256'], model_or_gpu_loaded=False)))


if __name__ == '__main__': main()
