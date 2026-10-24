"""Verify published binaries without application sources or a GPU on the runner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import traceback


def run(*command, **kwargs):
    print('+', ' '.join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def checked_version(value):
    if not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise argparse.ArgumentTypeError('Expected a three-part release version')
    return value


def verify_file(path, expected):
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if path.stat().st_size != expected['size'] or digest != expected['sha256']:
        raise RuntimeError(f'Release asset differs from the build manifest: {path.name}')
    print(f'Verified SHA256: {path.name} {digest}', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True, type=checked_version)
    parser.add_argument('--from-version', required=True, type=checked_version)
    parser.add_argument('--edition', required=True, choices=('CPU', 'GPU'))
    parser.add_argument('--mode', required=True, choices=('delta', 'full'))
    args = parser.parse_args()
    repository = os.environ['GITHUB_REPOSITORY']
    temp = Path(os.environ['RUNNER_TEMP']).resolve()
    evidence = temp / 'fungiq-ci-output'
    evidence.mkdir(exist_ok=False)
    # Select a local disk with enough space for the CUDA runtime and delta scratch.
    locations = [temp, Path(os.environ['SYSTEMDRIVE'] + '\\')]
    location = max(locations, key=lambda p: shutil.disk_usage(p).free)
    name = f'fungiq-qa-{os.environ["GITHUB_RUN_ID"]}-{args.edition.lower()}-{args.mode}'
    work = location / name
    if work.exists():
        raise RuntimeError('Verification directory already exists')
    work.mkdir()
    feed = work / 'feed'
    feed.mkdir()
    output = work / f'{args.edition.lower()}-{args.mode}-verified'
    manifest = json.loads((Path(__file__).parent / 'manifests' / f'{args.version}.json').read_text())
    if manifest['version'] != args.version or manifest['from_version'] != args.from_version:
        raise RuntimeError('Build manifest has the wrong release versions')
    channel = args.edition.lower()
    base_name = f'FungiQ.{args.edition}-{args.from_version}-{channel}-full.nupkg'
    target_names = [f'FungiQ.{args.edition}-{args.version}-{channel}-{kind}.nupkg'
                    for kind in ('full', 'delta')]
    success = False
    try:
        print(json.dumps({'runner_os': os.environ['RUNNER_OS'], 'work': str(work),
                          'free_gib': round(shutil.disk_usage(work).free / 2**30, 2)}), flush=True)
        run('gh', 'release', 'download', f'v{args.from_version}', '--repo', repository,
            '--pattern', base_name, '--dir', feed)
        run('gh', 'release', 'download', f'v{args.version}', '--repo', repository,
            '--pattern', target_names[0], '--pattern', target_names[1],
            '--pattern', f'releases.{channel}.json', '--dir', feed)
        verify_file(feed / base_name, manifest['base_assets'][base_name])
        for filename in target_names:
            verify_file(feed / filename, manifest['assets'][filename])
        command = [sys.executable, str(Path(__file__).with_name('verify_velopack_upgrade.py')),
                   '--edition', args.edition, '--from-version', args.from_version,
                   '--to-version', args.version, '--feed', str(feed), '--output', str(output),
                   '--trim-test-cache']
        if args.mode == 'full':
            command.append('--full')
        with (evidence / 'verification.log').open('w', encoding='utf-8') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1200)
        print((evidence / 'verification.log').read_text(encoding='utf-8', errors='replace'), flush=True)
        if result.returncode:
            raise RuntimeError(f'Real update verification exited with {result.returncode}')
        report = json.loads((output / 'reports/upgrade.json').read_text())
        if not report.get('success') or report['startup'].get('cuda_available'):
            raise RuntimeError('The clean Windows CPU-fallback verification did not pass')
        if len(report['startup']['windows']) != 4 or len(report['startup']['segmentation']) != 3:
            raise RuntimeError('Not all workspaces and segmentation models were tested')
        report['independent_windows_ci'] = {
            'run_id': os.environ['GITHUB_RUN_ID'], 'commit': os.environ['GITHUB_SHA'],
            'runner_os': os.environ['RUNNER_OS'], 'cpu_fallback': True,
            'verified_github_actions': os.environ.get('GITHUB_ACTIONS') == 'true',
        }
        (output / 'reports/upgrade.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        success = True
    except Exception:
        (evidence / 'failure.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
    finally:
        reports = output / 'reports'
        if reports.exists():
            shutil.copytree(reports, evidence / 'reports', dirs_exist_ok=True)
        native = Path(os.environ['LOCALAPPDATA']) / 'velopack' / f'velopack_FungiQ.{args.edition}.log'
        if native.exists():
            shutil.copy2(native, evidence / 'native-update.log')
            print(native.read_text(encoding='utf-8', errors='replace')[-14000:], flush=True)
        summary = Path(os.environ['GITHUB_STEP_SUMMARY'])
        with summary.open('a', encoding='utf-8') as stream:
            stream.write(f'## {args.edition} {args.mode}: {"PASS" if success else "FAIL"}\n')
            stream.write(f'{args.from_version} -> {args.version}; actual native updater and frozen application.\n')


if __name__ == '__main__':
    main()
