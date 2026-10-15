"""Cooperating process locks and recoverable publication of file/manifest pairs.

Readers and writers must hold the same lock. A durable journal rolls an
interrupted publication back, or finishes cleanup after a committed publication.
Two filesystem renames are not claimed to be a single atomic operation.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import errno
import json
import os
from pathlib import Path
import tempfile
import time


async def run_sync(function, *args, **kwargs):
    """Keep an enclosing lock alive until a cancelled worker has actually stopped."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A second cancellation must not release the enclosing file lock early.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


def _open_lock(target):
    handle = target.with_name('.'+target.name+'.lock').open('a+b')
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b'\0')
        handle.flush()
    handle.seek(0)
    return handle


def _try_lock(handle):
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(exc, 'winerror', None) == 33:
            return False
        raise


@contextmanager
def file_lock(target: Path, timeout=60):
    # Lock files stay in place: unlinking them would allow locking different inodes.
    with _open_lock(target) as handle:
        deadline = time.monotonic()+timeout
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise TimeoutError('another process is using this artifact')
            time.sleep(0.05)
        yield


@asynccontextmanager
async def async_file_lock(target: Path, timeout=60):
    with _open_lock(target) as handle:
        deadline = time.monotonic()+timeout
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise TimeoutError('another process is using this artifact')
            await asyncio.sleep(0.05)
        yield


def stage_bytes(target: Path, body: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix='.'+target.name+'.', suffix='.partial', dir=target.parent)
    path = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _journal_path(data):
    return data.with_name('.'+data.name+'.transaction.json')


def _backup_path(target):
    return target.with_name('.'+target.name+'.rollback')


def _write_journal(path, value):
    temporary = stage_bytes(path, json.dumps(value, sort_keys=True).encode())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recover_pair(data: Path, manifest: Path):
    """Caller holds the artifact lock; leave journal intact if recovery fails."""
    journal = _journal_path(data)
    if not journal.exists():
        return
    try:
        value = json.loads(journal.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise OSError('invalid artifact recovery journal; manual inspection required') from exc
    if not isinstance(value, dict):
        raise OSError('invalid artifact recovery journal')
    if type(value.get('committed')) is not bool:
        raise OSError('invalid artifact recovery journal')
    staged = []
    for key, target in [('data', data), ('manifest', manifest)]:
        name = value.get(key+'_stage')
        if (not isinstance(name, str) or Path(name).name != name or
                not name.startswith('.'+target.name+'.') or not name.endswith('.partial') or
                type(value.get(key+'_existed')) is not bool):
            raise OSError('invalid artifact recovery path')
        staged.append(target.parent/name)
    for key, target in [('data', data), ('manifest', manifest)]:
        backup = _backup_path(target)
        if not value['committed']:
            if backup.exists():
                os.replace(backup, target)
            elif not value[key+'_existed']:
                target.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
    for path in staged:
        path.unlink(missing_ok=True)
    journal.unlink()


def publish_pair(data_stage: Path, manifest_stage: Path, data: Path, manifest: Path):
    """Publish under the caller's lock, restoring the previous pair on failure."""
    if any(path.parent != data.parent for path in [manifest, data_stage, manifest_stage]):
        raise ValueError('artifact stages must be in the destination directory')
    recover_pair(data, manifest)
    for target in [data, manifest]:
        if target.is_symlink() or _backup_path(target).exists():
            raise OSError('unsafe artifact target or unresolved backup')
    value = {'committed': False, 'data_stage': data_stage.name, 'manifest_stage': manifest_stage.name,
             'data_existed': data.exists(), 'manifest_existed': manifest.exists()}
    journal = _journal_path(data)
    _write_journal(journal, value)
    try:
        for target in [data, manifest]:
            if target.exists():
                os.replace(target, _backup_path(target))
        os.replace(data_stage, data)
        os.replace(manifest_stage, manifest)
        _write_journal(journal, value | {'committed': True})
    except BaseException:
        recover_pair(data, manifest)
        raise
    recover_pair(data, manifest)
