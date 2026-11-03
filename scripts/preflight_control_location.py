#!/usr/bin/env python3
"""Bind an immutable P6 build to two independently recomputed CPU references."""
import argparse
import hashlib
import json
from pathlib import Path
from control_math import require, verify_reference_document


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--attempt', default='attempt-1')
    args = parser.parse_args()
    root = args.experiment.resolve()
    attempt = root / '.build' / args.attempt
    build = json.loads((attempt / 'build.json').read_text())
    require(build['returncode'] == 0, 'build failed')
    require(digest(attempt / 'metal-control-location') == build['binary_sha256'], 'binary changed')
    for name, expected in build['source_sha256'].items():
        require(digest(attempt / 'source' / name) == expected, 'snapshot changed: ' + name)
    references = []
    for rounds in (16, 64):
        directory = root / 'verification' / f'reference-{rounds}'
        run = json.loads((directory / 'run.json').read_text())
        require(run['returncode'] == 0 and not run['timed_out'], 'reference execution failed')
        for name, info in run['files'].items():
            require(digest(directory / name) == info['sha256'], 'reference log changed')
            require((directory / name).stat().st_size == info['bytes'], 'reference log size changed')
        references.append(dict(verify_reference_document(json.loads((directory / 'stdout.json').read_text())),
                               path=str(directory / 'stdout.json'), file_sha256=digest(directory / 'stdout.json')))
    print(json.dumps({'passed': True, 'scope': 'immutable source/binary and independent full CPU reference agreement; no GPU measurement',
                      'build_manifest_sha256': digest(attempt / 'build.json'),
                      'binary_sha256': build['binary_sha256'], 'source_sha256': build['source_sha256'],
                      'checker_sha256': digest(Path(__file__)),
                      'math_sha256': digest(Path(__file__).with_name('control_math.py')),
                      'references': references}, indent=2))


if __name__ == '__main__':
    main()
