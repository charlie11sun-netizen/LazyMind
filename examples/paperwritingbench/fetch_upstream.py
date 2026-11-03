"""Download the pinned official raters and verify their recorded SHA-256 hashes."""
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parent


def verify():
    manifest = json.loads((ROOT / 'upstream-manifest.json').read_text())
    for item in manifest['files']:
        path = ROOT / 'upstream' / item['path']
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('Run python -m examples.paperwritingbench.fetch_upstream to restore pinned raters')


def fetch():
    manifest = json.loads((ROOT / 'upstream-manifest.json').read_text())
    repository = manifest['repository'].removeprefix('https://github.com/')
    base = f'https://raw.githubusercontent.com/{repository}/{manifest["commit"]}/'
    for item in manifest['files']:
        path = ROOT / 'upstream' / item['path']
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']:
            continue
        with urllib.request.urlopen(base + item['path'], timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Pinned upstream hash mismatch: ' + item['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print('Verified pinned official raters and Apache-2.0 license.')


if __name__ == '__main__':
    fetch()
