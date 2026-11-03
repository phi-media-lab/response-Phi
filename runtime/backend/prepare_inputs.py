#!/usr/bin/env python3
"""Freeze CM1 model/prefix inputs using only stdlib/file reads; never import MLX."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

TINY = Path('/Users/fbsh/tiny-llm')
MODEL = Path('/Users/fbsh/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B-MLX-4bit/snapshots/173234aa840d113125e9f2271100ddbaf16c9620')
A32 = [10711, 5043, 13037, 21429, 1682, 2473, 27011, 17659,
       3184, 12082, 19196, 2000, 29909, 16727, 7135, 1328,
       2916, 14309, 13802, 2389, 7986, 3072, 18156, 14010,
       2036, 27194, 18628, 4156, 7415, 20764, 20659, 19203]
B_PREFIX7 = [2127, 19010, 19287, 13098, 1724, 7344, 1626]


def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def pin(path):
    return {'path': str(path), 'resolved_path': str(path.resolve(strict=True)),
            'bytes': path.stat().st_size, 'sha256': digest_file(path)}


def tokens_digest(tokens):
    return hashlib.sha256(json.dumps(tokens, separators=(',', ':')).encode()).hexdigest()


def git(root, *args):
    result = subprocess.run(['/usr/bin/git', '-C', str(root), *args],
                            capture_output=True, timeout=10, check=False)
    if result.returncode:
        raise RuntimeError(f'git {args}: {result.stderr.decode(errors="replace")}')
    return result.stdout.decode()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--tiny-root', type=Path, default=TINY)
    p.add_argument('--model', type=Path, default=MODEL)
    p.add_argument('--python', type=Path, default=TINY / '.venv/bin/python')
    p.add_argument('--toolchain-observation', type=Path, required=True)
    args = p.parse_args()
    for path in [args.output, args.tiny_root, args.model, args.python, args.toolchain_observation]:
        if not path.is_absolute():
            p.error('all paths must be absolute')
    if args.output.exists():
        p.error('output already exists')
    if args.tiny_root != TINY or args.model != MODEL or args.python != TINY / '.venv/bin/python':
        p.error('CM1 binds the existing fixed tiny-llm/model/venv paths')
    model_config = json.loads((args.model / 'config.json').read_text())
    if (model_config.get('num_hidden_layers'), model_config.get('vocab_size')) != (28, 151936):
        raise ValueError('unexpected model geometry')
    weights = sorted(args.model.glob('*.safetensors'))
    if not weights:
        raise FileNotFoundError('cached weights absent; no download')
    files = set((args.tiny_root / 'src/tiny_llm_ref').rglob('*.py'))
    ext = args.tiny_root / 'src/extensions_ref/tiny_llm_ext_ref'
    native = [f for f in ext.iterdir() if f.is_file() and
              f.suffix in {'.py', '.pyi', '.so', '.dylib', '.metallib'}]
    if not all(any(f.suffix == s for f in native) for s in ['.so', '.dylib', '.metallib']):
        raise FileNotFoundError('prebuilt reference extension incomplete; no build')
    files.update(native)
    parent_init = ext.parent / '__init__.py'
    if parent_init.exists():
        files.add(parent_init)
    # load() constructs a tokenizer too; include merges.txt and every normal
    # cached model file, not only weights/JSON. Symlink targets are pinned.
    files.update(f for f in args.model.iterdir() if f.is_file())
    files.add(args.python)
    venv = args.tiny_root / '.venv'
    if args.python.parent != venv / 'bin' or not (venv / 'pyvenv.cfg').is_file():
        raise ValueError('worker Python must belong to this exact existing venv')
    files.add(venv / 'pyvenv.cfg')
    sites = sorted((args.tiny_root / '.venv/lib').glob('python*/site-packages'))
    if len(sites) != 1:
        raise ValueError('expected one existing venv site-packages')
    site = sites[0]
    versions = {}
    wanted = {'mlx', 'mlx-lm', 'numpy', 'transformers'}
    for metadata in site.glob('*.dist-info/METADATA'):
        fields = {}
        for line in metadata.read_text().splitlines():
            if ': ' in line:
                key, value = line.split(': ', 1)
                fields.setdefault(key, value)
        name = fields.get('Name', '').lower().replace('_', '-')
        if name in wanted:
            versions[name] = fields.get('Version')
            files.add(metadata)
    if set(versions) != wanted:
        raise ValueError('required installed package metadata missing')
    # Pin MLX and mlx-lm code/native files as well as the entire reference closure.
    # Other dependencies are version observations, not a full environment lockfile.
    for package in ['mlx', 'mlx_lm']:
        for f in (site / package).rglob('*'):
            if f.is_file() and '__pycache__' not in f.parts and f.suffix != '.pyc':
                files.add(f)
    # Explicit dependency: relocation never guesses a repository from parents.
    tool_obs = args.toolchain_observation
    files.add(tool_obs)
    files.add(Path('/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc'))
    inputs = [pin(f) for f in sorted(files) if '__pycache__' not in f.parts]
    data = {'schema': 'cm1-inputs-v1', 'tiny_root': str(args.tiny_root),
            'model': str(args.model), 'python': str(args.python), 'site_packages': str(site),
            'model_snapshot': args.model.name, 'layers': 28, 'page_size': 128,
            'vocab_size': 151936, 'seed': 7, 'first_run_evaluations': 2,
            'a32': A32, 'b_prefix7': B_PREFIX7,
            'b_last_token_rule': 'this-session-actual-A-result',
            'fixed_prefix_origin': {
                'path': '/Users/fbsh/response-lab/results/swift-mlx-wait-owner-smwo1/preparation/inputs.json',
                'bytes': 229671, 'sha256': '6a3acfd73a705a690fa2d7ab5c04ad3a8fd0763ff2cfb637403f318ee360935a',
                'selection': 'ref-A token_ids and first seven ref-B token_ids; no old B eighth token'},
            'external_inputs': inputs, 'package_versions': versions,
            'git': {'head': git(args.tiny_root, 'rev-parse', 'HEAD').strip(),
                    'status_porcelain': git(args.tiny_root, 'status', '--porcelain')},
            'toolchain_observation': pin(tool_obs)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'output': pin(args.output), 'fixed_a_tokens': len(A32), 'fixed_b_prefix_tokens': len(B_PREFIX7),
                      'external_inputs': len(inputs)}))


if __name__ == '__main__':
    main()
