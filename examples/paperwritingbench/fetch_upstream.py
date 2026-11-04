"""Download the pinned official raters and verify their recorded SHA-256 hashes."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parent


def settings(suite):
    name = 'upstream-manifest.json' if suite == 'paperwritingbench' else 'autosurvey-manifest.json'
    directory = ROOT / 'upstream'
    if suite == 'autosurvey':
        directory /= 'autosurvey'
    return json.loads((ROOT / name).read_text()), directory


def verify(suite='paperwritingbench'):
    manifest, directory = settings(suite)
    for item in manifest['files']:
        path = directory / item['path']
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('Restore pinned raters: python -m examples.paperwritingbench.fetch_upstream '
                             '--suite ' + suite)


def fetch(suite='paperwritingbench'):
    manifest, directory = settings(suite)
    repository = manifest['repository'].removeprefix('https://github.com/')
    base = f'https://raw.githubusercontent.com/{repository}/{manifest["commit"]}/'
    for item in manifest['files']:
        path = directory / item['path']
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']:
            continue
        with urllib.request.urlopen(base + item['path'], timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Pinned upstream hash mismatch: ' + item['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print('Verified pinned sources: ' + manifest['repository'] + '@' + manifest['commit'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=['paperwritingbench', 'autosurvey'], default='paperwritingbench')
    fetch(parser.parse_args().suite)
