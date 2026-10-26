"""Optional, request-scoped recovery of validated stages; never cache a failed candidate."""
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from functools import lru_cache
import json
import logging
from pathlib import Path
import tempfile

from .schema import digest

LOG = logging.getLogger(__name__)
VERSION = 'paper-f4-recovery.v2'
MAX_BYTES = 32 * 1024 * 1024
_root = ContextVar('paper_f4_recovery_root', default=None)


@lru_cache(maxsize=1)
def implementation():
    """Invalidate snapshots when code or reviewed plot resources change between releases."""
    root = Path(__file__).resolve().parents[1]
    import hashlib
    files = sorted(path for folder in ('analysis', 'plotting', 'execution', 'plot_skills')
                   for path in (root / folder).rglob('*')
                   if path.suffix in ('.py', '.json', '.md'))
    return digest([(str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest()) for path in files])


@contextmanager
def workspace(path, task_id='', conversation_id=''):
    """Production workspaces are <root>/<user>/<task>; retries may receive a new task ID."""
    path = Path(path).resolve()
    root = path.parent if task_id and path.name == task_id and conversation_id else path
    token = _root.set(root / '.paper-f4-recovery' / digest(conversation_id))
    try:
        yield
    finally:
        _root.reset(token)


class Store:
    def __init__(self, inputs, configuration):
        root = _root.get()
        self.key = digest(dict(version=VERSION, implementation=implementation(), inputs=inputs,
                               configuration=configuration))
        self.root = root / self.key if root is not None else None

    def load(self, stage, identity):
        if self.root is None:
            return None
        key = digest([stage, identity])
        try:
            path = self.root / (key + '.json')
            if path.stat().st_size > MAX_BYTES:
                return None
            record = json.loads(path.read_text(encoding='utf-8'))
            if (record['version'] != VERSION or record['request'] != self.key or record['key'] != key
                    or record['sha256'] != digest(record['value'])):
                return None
            return record['value']
        except (OSError, ValueError, KeyError, TypeError):
            return None  # Missing, interrupted or incompatible snapshots are recomputed.

    def save(self, stage, identity, value):
        if self.root is None:
            return
        key = digest([stage, identity])
        temporary = None
        try:
            raw = json.dumps(dict(version=VERSION, request=self.key, key=key,
                                  sha256=digest(value), value=value), ensure_ascii=False, allow_nan=False)
            if len(raw.encode()) > MAX_BYTES:
                return
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.root, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
            temporary.replace(self.root / (key + '.json'))
        except (OSError, ValueError, TypeError):
            LOG.warning('F4 recovery snapshot unavailable for %s; execution continues', stage)
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
