"""Fetch one pinned sample with HTTP ranges instead of downloading the full dataset ZIP."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.request
import uuid
import zipfile

REVISION = 'fbcfcafd042184ca954954b08dfcb92bd7942b51'
BASE = 'https://huggingface.co/datasets/yiwen-song/PaperWritingBench/resolve/'


class RemoteZip(io.RawIOBase):
    def __init__(self, url):
        self.url, self.position = url, 0
        request = urllib.request.Request(url + '?probe=' + uuid.uuid4().hex, headers={'Range': 'bytes=-65536'})
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 206:
                raise ValueError('Dataset host must support HTTP byte ranges')
            self.size = int(response.headers['Content-Range'].split('/')[-1])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        return self.position

    def read(self, count=-1):
        count = min(count if count >= 0 else self.size - self.position, self.size - self.position)
        if count <= 0:
            return b''
        start, end = self.position, self.position + count - 1
        request = urllib.request.Request(self.url + f'?range={start}-{end}',
                                         headers={'Range': f'bytes={start}-{end}'})
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 206 or not response.headers.get('Content-Range', '').startswith(f'bytes {start}-'):
                raise ValueError('Unexpected archive range response')
            data = response.read(count + 1)
        if len(data) != count:
            raise ValueError('Incomplete archive range')
        self.position += len(data)
        return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', required=True, help='For example: cvpr2025_1ca5e1b7d4')
    parser.add_argument('--venue', default='cvpr2025')
    parser.add_argument('--revision', default=REVISION)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--reference', action='store_true', help='Separately fetch reference PDF/GT for scoring only')
    args = parser.parse_args()
    if not all(re.fullmatch(r'[A-Za-z0-9_-]+', value) for value in (args.sample, args.venue)):
        parser.error('Invalid sample or venue identifier')
    if not re.fullmatch(r'[a-fA-F0-9]{40}', args.revision):
        parser.error('Use a pinned dataset commit SHA')
    if args.out.exists():
        parser.error('Use a fresh sample directory')
    prefix = f'datasets/{args.venue}/papers/{args.sample}/'
    records = []
    with zipfile.ZipFile(RemoteZip(BASE + args.revision + '/datasets.zip')) as archive:
        for item in archive.infolist():
            if not item.filename.startswith(prefix) or not item.file_size:
                continue
            relative = Path(item.filename.removeprefix(prefix))
            reference = relative.suffix == '.pdf' or relative.name.startswith('original_paper_gt_citations_')
            writing = (
                str(relative) in ('raw_materials/idea_sparse.md', 'raw_materials/idea_dense.md',
                                  'raw_materials/experimental_log.md', 'raw_materials/figures/info.json')
                or relative.parent.as_posix() == 'raw_materials/figures' and relative.suffix.lower() == '.png')
            if not writing and not (args.reference and reference):
                continue
            target = args.out / ('reference' if reference else 'writing') / relative
            if not target.resolve().is_relative_to(args.out.resolve()):
                raise ValueError('Unsafe archive path')
            data = archive.read(item)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            records.append({'path': str(target.relative_to(args.out)), 'sha256': hashlib.sha256(data).hexdigest()})
    if not records:
        raise ValueError('Sample was not found in the pinned archive')
    (args.out / 'sample-manifest.json').write_text(json.dumps({
        'dataset': 'yiwen-song/PaperWritingBench', 'revision': args.revision,
        'sample_id': args.sample, 'files': records}, indent=2) + '\n')


if __name__ == '__main__':
    main()
