"""Local artifacts, scoring work groups and resumable results."""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from .codex import EFFORT, MODEL


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


@contextmanager
def lock(directory):
    with (directory / '.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def slide_count(pptx):
    with ZipFile(pptx) as file:
        pages = sum(bool(re.fullmatch(r'ppt/slides/slide\d+\.xml', name)) for name in file.namelist())
        if not pages or file.testzip() is not None:
            raise ValueError(f'Corrupt or empty PowerPoint deck: {pptx}')
    return pages


def render_pdf(pptx, output):
    with TemporaryDirectory(prefix='office-', dir=output) as profile:
        subprocess.run(['soffice', f'-env:UserInstallation={Path(profile).as_uri()}', '--headless',
                        '--convert-to', 'pdf', '--outdir', str(output), str(pptx)], check=True, timeout=180)
    pdf = output / f'{pptx.stem}.pdf'
    if not pdf.is_file():
        raise RuntimeError('LibreOffice did not export the presentation to PDF')
    return pdf


def pages(pdf, directory, *, dpi, ext, renderer, quality=90):
    from PIL import Image
    import pymupdf

    directory.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf) as document:
        count = len(document)
        files = [directory / f'slide_{index:04d}.{ext}' for index in range(1, count + 1)]
        if not all(path.is_file() for path in files):
            if renderer == 'poppler':
                from pdf2image import convert_from_path

                for path, rendered in zip(files, convert_from_path(pdf, dpi=dpi, fmt=ext), strict=True):
                    temporary = path.with_suffix('.tmp.' + ext)
                    rendered.save(temporary)
                    temporary.replace(path)
            else:
                for path, page in zip(files, document, strict=True):
                    pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
                    with Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples) as rendered:
                        temporary = path.with_suffix('.tmp.' + ext)
                        rendered.save(temporary, quality=quality)
                        temporary.replace(path)
    return files


@dataclass(frozen=True)
class Check:
    name: str
    inputs: list[dict]
    schema: dict | None = None
    validate: Callable[[str], object] | None = None


def totals(usages):
    result = {key: sum(usage[key] for usage in usages)
              for key in ('inputTokens', 'cachedInputTokens', 'outputTokens')}
    result['cacheHitRate'] = (result['cachedInputTokens'] / result['inputTokens']
                              if result['inputTokens'] else None)
    return result


class Progress:
    def __init__(self, output, fingerprints):
        self.path = output / 'checkpoint.json'
        self.fingerprint = {'model': MODEL, 'effort': EFFORT, 'inputs': fingerprints}
        self.data = read(self.path) if self.path.is_file() else {
            'fingerprint': self.fingerprint, 'auth': 'chatgpt', 'warmups': [], 'checks': {},
        }
        if self.data['fingerprint'] != self.fingerprint:
            raise ValueError('The checkpoint belongs to different scoring inputs')

    def pending(self, checks):
        return [check for check in checks if check.name not in self.data['checks']]

    def answer(self, name):
        return self.data['checks'][name]['answer']

    def flush(self):
        self.data['usage'] = totals([item['usage'] for item in self.data['checks'].values()])
        save(self.path, self.data)

    def check(self, codex, thread, request):
        _, answer, usage = codex.turn(thread, request.inputs, request.schema)
        if request.validate:
            request.validate(answer)
        self.data['checks'][request.name] = {'answer': answer, 'usage': usage}
        self.flush()
        print(f"{request.name} cached={usage['cachedInputTokens']}/{usage['inputTokens']}", flush=True)

    def group(self, codex, name, evidence, checks):
        pending = self.pending(checks)
        if not pending:
            return
        with codex.evidence(evidence) as (thread, warm, usage):
            self.data['warmups'].append({'group': name, 'usage': usage})
            self.flush()
            for request in pending:
                with codex.branch(thread, warm) as branch:
                    self.check(codex, branch, request)
