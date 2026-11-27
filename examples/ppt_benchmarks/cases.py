"""Fetch the two pinned benchmark cases and prepare a native LazyMind run."""

import argparse
from hashlib import sha256
import os
from pathlib import Path
from urllib.request import urlopen

from examples.presentbench.run import prepare_files


DATASET_CASES = {'presentbench': 'alphabet', 'slidesgen': 'steps'}
PRESENTBENCH = ('https://huggingface.co/datasets/PresentBench/PresentBench/resolve/'
                '3c6e01f563165d3a364a459f9777cd7286f42cd8/'
                'economics/Alphabet_Investor_Relations/2024q1/')
SLIDESGEN = ('https://raw.githubusercontent.com/YunqiaoYang/SlidesGen-Bench/'
             'b202e60c39e19c5f9da57029fe53ff189e6642b0/'
             'dataset/brand_promote/50-Million-Steps-Campaign-Toolkit-2025/')
CASES = {
    'alphabet': (
        ('instructions.md', PRESENTBENCH + 'generation_task/instructions.md',
         '0eab765461acc702f533473c8315a4ae093c9affa71c8e879b92b97546190536'),
        ('material.pdf', PRESENTBENCH + 'material.pdf',
         '314881a72319fadddcf44a57ba4b186d2d3570daa5e837b1116a6dc7b0bad4ed'),
    ),
    'steps': (
        ('paragraph.txt', SLIDESGEN + 'paragraph.txt',
         'f70c03ba5434d30092c56fb80ec905b682f10e912aba2ead245d0f94ee22e628'),
        ('50-Million-Steps-Campaign-Toolkit-2025.pdf',
         SLIDESGEN + '50-Million-Steps-Campaign-Toolkit-2025.pdf',
         '0872fc7f609a87277d4f92c908d1e6f536e31bccd43b2b897add73fd0bc8c275'),
    ),
}


def case_files(name, root, *, fetch=False):
    folder = root.resolve() / name
    if fetch:
        folder.mkdir(parents=True, exist_ok=True)
    files = []
    for filename, url, expected in CASES[name]:
        path = folder / filename
        if fetch and not path.exists():
            with urlopen(url, timeout=90) as response:
                data = response.read()
            if sha256(data).hexdigest() != expected:
                raise ValueError(f'Download checksum differs from the pinned input: {filename}')
            path.write_bytes(data)
        if sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Local checksum differs from the pinned input: {path}')
        files.append(path)
    return tuple(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('fetch').add_argument('root', type=Path)
    prep = commands.add_parser('prepare')
    prep.add_argument('case', choices=CASES)
    prep.add_argument('root', type=Path)
    prep.add_argument('run', type=Path)
    prep.add_argument('--delivery', default='')
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'fetch':
        for name in CASES:
            print(*(str(path) for path in case_files(name, args.root, fetch=True)), sep='\n')
    else:
        prompt, material = case_files(args.case, args.root)
        prepare_files(prompt.parent, prompt, [material], args.run, args.delivery)


if __name__ == '__main__':
    main()
