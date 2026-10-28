"""W9: sandboxed personal file workflow (inventory, content-hash dedupe, undo).

The worker may only touch one user-approved sandbox directory. Every path is
resolved with realpath and refused if it leaves the sandbox; symlinks are never
followed (not during the walk, not as path components, not when opening files).
Scans and reads are bounded. Moves are planned as a dry run, re-checked against
the plan-time hashes before execution (temporal guard), journalled in SQLite
before each side effect, and independently verified by re-hashing afterwards.
The journal lives in ``db_path`` (WAL) so undo survives a restart.

This is a real filesystem tool, but it is a portable (POSIX/Windows) Python
worker, not native Windows Explorer automation.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUARANTINE = '.resolve_quarantine'
EXPORTS = 'exports'
MAX_FILES = 5000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_NOTE_BYTES = 1024 * 1024
CHUNK = 1024 * 1024
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_BINARY = getattr(os, 'O_BINARY', 0)


class FileWorkerError(ValueError):
    def __init__(self, code: str, detail: str = ''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code = code
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class FileWorker:
    def __init__(self, sandbox: Path, db_path: Path, max_files: int = MAX_FILES,
                 max_file_bytes: int = MAX_FILE_BYTES):
        root = Path(os.path.realpath(sandbox))
        if not root.is_dir():
            raise FileWorkerError('SANDBOX_MISSING', str(sandbox))
        self.root = root
        self.db_path = Path(db_path)
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self._init_db()

    # ------------------------------------------------------------------ db
    def _db(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as con, con:
            con.execute('PRAGMA journal_mode=WAL')
            con.executescript('''
                CREATE TABLE IF NOT EXISTS fw_plans(
                    plan_id TEXT PRIMARY KEY, sandbox TEXT NOT NULL, created_at TEXT NOT NULL,
                    moves_json TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fw_ops(
                    op_id TEXT PRIMARY KEY, kind TEXT NOT NULL, plan_id TEXT, sandbox TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    verification_json TEXT);
                CREATE TABLE IF NOT EXISTS fw_journal(
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT, op_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    src TEXT NOT NULL, dst TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                    state TEXT NOT NULL, updated_at TEXT NOT NULL);
            ''')

    def _set_op(self, op_id: str, status: str, verification: dict | None = None) -> None:
        with closing(self._db()) as con, con:
            con.execute('UPDATE fw_ops SET status=?, updated_at=?, verification_json=COALESCE(?, verification_json) '
                        'WHERE op_id=?', (status, _now(), json.dumps(verification) if verification else None, op_id))

    def _set_entry(self, entry_id: int, state: str) -> None:
        with closing(self._db()) as con, con:
            con.execute('UPDATE fw_journal SET state=?, updated_at=? WHERE entry_id=?', (state, _now(), entry_id))

    # --------------------------------------------------------------- paths
    def _excluded(self, abs_path: Path) -> bool:
        db = os.path.realpath(self.db_path)
        return str(abs_path) in (db, db + '-wal', db + '-shm', db + '-journal')

    def _resolve(self, rel: str | os.PathLike, allow_internal: bool = False) -> Path:
        """Map a sandbox-relative path to an absolute path, refusing escapes and symlinks."""
        text = os.fspath(rel)
        if not text or '\x00' in text:
            raise FileWorkerError('INVALID_PATH', repr(text))
        if os.path.isabs(text) or Path(text).drive or text.startswith(('/', '\\')):
            raise FileWorkerError('ABSOLUTE_PATH', text)
        parts = Path(text.replace('\\', '/')).parts
        if any(p == '..' for p in parts):
            raise FileWorkerError('PATH_TRAVERSAL', text)
        if not allow_internal and parts and parts[0] == QUARANTINE:
            raise FileWorkerError('INTERNAL_PATH', text)
        candidate = self.root.joinpath(*parts)
        # never follow a symlink as any component of the path
        walk = self.root
        for part in parts:
            walk = walk / part
            if os.path.islink(walk):
                raise FileWorkerError('SYMLINK_REFUSED', text)
        real = Path(os.path.realpath(candidate))
        if os.path.commonpath([str(real), str(self.root)]) != str(self.root):
            raise FileWorkerError('OUTSIDE_SANDBOX', text)
        return real

    def _rel(self, abs_path: Path) -> str:
        return Path(os.path.relpath(abs_path, self.root)).as_posix()

    @staticmethod
    def _plain_name(name: str) -> str:
        if not name or name in ('.', '..') or '/' in name or '\\' in name or '\x00' in name or os.path.isabs(name):
            raise FileWorkerError('INVALID_NAME', repr(name))
        return name

    def _hash(self, abs_path: Path) -> tuple[str, int]:
        """Chunked sha256 of a regular, non-symlink file inside the sandbox."""
        fd = os.open(str(abs_path), os.O_RDONLY | _NOFOLLOW | _BINARY)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise FileWorkerError('NOT_REGULAR_FILE', self._rel(abs_path))
            if st.st_size > self.max_file_bytes:
                raise FileWorkerError('FILE_TOO_LARGE', self._rel(abs_path))
            h = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_file_bytes:
                    raise FileWorkerError('FILE_TOO_LARGE', self._rel(abs_path))
                h.update(chunk)
            return h.hexdigest(), total
        finally:
            os.close(fd)

    def _hash_or_none(self, abs_path: Path) -> str | None:
        try:
            if os.path.islink(abs_path) or not abs_path.is_file():
                return None
            return self._hash(abs_path)[0]
        except (OSError, FileWorkerError):
            return None

    # ----------------------------------------------------------- inventory
    def inventory(self) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            here = Path(dirpath)
            keep = []
            for d in sorted(dirnames):
                p = here / d
                if here == self.root and d in (QUARANTINE, EXPORTS):
                    continue
                if os.path.islink(p):
                    skipped.append({'path': self._rel(p), 'reason': 'symlink_dir_not_followed'})
                    continue
                keep.append(d)
            dirnames[:] = keep
            for name in sorted(filenames):
                p = here / name
                if self._excluded(p):
                    continue
                rel = self._rel(p)
                if truncated or len(files) >= self.max_files:
                    truncated = True
                    skipped.append({'path': rel, 'reason': f'file_limit_{self.max_files}'})
                    continue
                if os.path.islink(p):
                    skipped.append({'path': rel, 'reason': 'symlink_not_followed'})
                    continue
                try:
                    st = os.lstat(p)
                    if not stat.S_ISREG(st.st_mode):
                        skipped.append({'path': rel, 'reason': 'not_regular_file'})
                        continue
                    if st.st_size > self.max_file_bytes:
                        skipped.append({'path': rel, 'reason': 'too_large'})
                        continue
                    digest, size = self._hash(p)
                except FileWorkerError as exc:
                    skipped.append({'path': rel, 'reason': exc.code.lower()})
                    continue
                except OSError as exc:
                    skipped.append({'path': rel, 'reason': f'os_error:{exc.errno}'})
                    continue
                files.append({'path': rel, 'size': size, 'sha256': digest,
                              'modified': _iso(st.st_mtime), '_mtime_ns': st.st_mtime_ns})
        files.sort(key=lambda f: f['path'])
        out = [{k: v for k, v in f.items() if not k.startswith('_')} for f in files]
        self._last_mtimes = {f['path']: f['_mtime_ns'] for f in files}
        return {'root': str(self.root), 'files': out, 'total_files': len(out),
                'total_bytes': sum(f['size'] for f in out), 'skipped': skipped, 'truncated': truncated}

    def duplicates(self) -> dict[str, Any]:
        inv = self.inventory()
        mt = self._last_mtimes
        by_hash: dict[str, list[dict]] = {}
        for f in inv['files']:
            by_hash.setdefault(f['sha256'], []).append(f)
        groups = []
        for digest, members in sorted(by_hash.items()):
            if len(members) < 2:
                continue
            members.sort(key=lambda f: (mt[f['path']], f['path']))  # oldest first, tie -> lexicographic
            groups.append({'sha256': digest, 'size': members[0]['size'], 'paths': [f['path'] for f in members]})
        return {'groups': groups}

    # ---------------------------------------------------------------- plan
    def plan_dedupe(self) -> dict[str, Any]:
        plan_id = 'plan_' + uuid.uuid4().hex[:16]
        moves = []
        for g in self.duplicates()['groups']:
            keep, *rest = g['paths']
            for src in rest:
                moves.append({'src': src, 'dst': f'{QUARANTINE}/{plan_id}/{src}', 'sha256': g['sha256'],
                              'size': g['size'], 'kept': keep})
        with closing(self._db()) as con, con:
            con.execute('INSERT INTO fw_plans VALUES (?,?,?,?,?)',
                        (plan_id, str(self.root), _now(), json.dumps(moves), 'PLANNED'))
        return {'plan_id': plan_id, 'dry_run': True, 'moves': moves}

    def _load_plan(self, plan_id: str) -> sqlite3.Row:
        with closing(self._db()) as con:
            row = con.execute('SELECT * FROM fw_plans WHERE plan_id=?', (plan_id,)).fetchone()
        if row is None or row['sandbox'] != str(self.root):
            raise FileWorkerError('UNKNOWN_PLAN', plan_id)
        return row

    @staticmethod
    def _no_clobber_move(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(dst):
            raise FileWorkerError('DESTINATION_OCCUPIED', str(dst))
        try:
            os.link(src, dst, follow_symlinks=False)  # fails if dst exists: no clobber
            os.unlink(src)
        except (OSError, NotImplementedError):
            if os.path.lexists(dst):
                raise
            os.rename(src, dst)

    def apply(self, plan_id: str) -> dict[str, Any]:
        row = self._load_plan(plan_id)
        if row['status'] != 'PLANNED':
            raise FileWorkerError('PLAN_NOT_APPLICABLE', f'{plan_id} is {row["status"]}')
        moves = json.loads(row['moves_json'])
        # temporal guard: every source must still match its plan-time hash
        checks = []
        for m in moves:
            try:
                src = self._resolve(m['src'])
                dst = self._resolve(m['dst'], allow_internal=True)
            except FileWorkerError as exc:  # e.g. source replaced by a symlink since planning
                checks.append({'src': m['src'], 'planned_sha256': m['sha256'], 'current_sha256': None,
                               'destination_free': None, 'error': exc.code, 'ok': False})
                continue
            now = self._hash_or_none(src)
            ok = now == m['sha256'] and not os.path.lexists(dst)
            checks.append({'src': m['src'], 'planned_sha256': m['sha256'], 'current_sha256': now,
                           'destination_free': not os.path.lexists(dst), 'ok': ok})
        if not all(c['ok'] for c in checks):
            with closing(self._db()) as con, con:
                con.execute("UPDATE fw_plans SET status='STALE' WHERE plan_id=?", (plan_id,))
            return {'op_id': None, 'moved': [], 'verification': {'status': 'STALE_PLAN', 'checks': checks}}

        op_id = 'op_' + uuid.uuid4().hex[:16]
        entries = []
        with closing(self._db()) as con, con:
            con.execute('INSERT INTO fw_ops VALUES (?,?,?,?,?,?,?,?)',
                        (op_id, 'dedupe_move', plan_id, str(self.root), 'DISPATCHING', _now(), _now(), None))
            con.execute("UPDATE fw_plans SET status='APPLYING' WHERE plan_id=?", (plan_id,))
            for i, m in enumerate(moves):
                cur = con.execute('INSERT INTO fw_journal(op_id,seq,src,dst,sha256,size,state,updated_at) '
                                  'VALUES (?,?,?,?,?,?,?,?)',
                                  (op_id, i, m['src'], m['dst'], m['sha256'], m['size'], 'INTENT', _now()))
                entries.append((cur.lastrowid, m))
        moved = []
        for entry_id, m in entries:  # intent was persisted above, before any move
            self._no_clobber_move(self._resolve(m['src']), self._resolve(m['dst'], allow_internal=True))
            self._set_entry(entry_id, 'MOVED')
            moved.append({'src': m['src'], 'dst': m['dst']})
        verification = self._verify_moves([m for _, m in entries], forward=True)
        self._set_op(op_id, verification['status'], verification)
        with closing(self._db()) as con, con:
            con.execute('UPDATE fw_plans SET status=? WHERE plan_id=?', ('APPLIED', plan_id))
        return {'op_id': op_id, 'moved': moved, 'verification': verification}

    def _verify_moves(self, moves: list[dict], forward: bool) -> dict[str, Any]:
        """Independent check from disk: target bytes hash to the recorded value, origin is gone."""
        checks = []
        for m in moves:
            present, absent = (m['dst'], m['src']) if forward else (m['src'], m['dst'])
            got = self._hash_or_none(self._resolve(present, allow_internal=True))
            gone = not os.path.lexists(self._resolve(absent, allow_internal=True))
            checks.append({'path': present, 'expected_sha256': m['sha256'], 'observed_sha256': got,
                           'origin_absent': gone, 'ok': got == m['sha256'] and gone})
        return {'status': 'CONFIRMED' if all(c['ok'] for c in checks) else 'FAILED', 'checks': checks}

    # ---------------------------------------------------------------- undo
    def undo(self, op_id: str) -> dict[str, Any]:
        with closing(self._db()) as con:
            op = con.execute('SELECT * FROM fw_ops WHERE op_id=?', (op_id,)).fetchone()
            rows = con.execute('SELECT * FROM fw_journal WHERE op_id=? ORDER BY seq', (op_id,)).fetchall()
        if op is None or op['sandbox'] != str(self.root) or op['kind'] != 'dedupe_move':
            raise FileWorkerError('UNKNOWN_OP', op_id)
        if op['status'] in ('UNDONE', 'UNDOING'):
            raise FileWorkerError('ALREADY_UNDONE', op_id)
        # restore from observed disk state, not from the journal's last state (crash-safe)
        todo, checks = [], []
        for r in rows:
            src = self._resolve(r['src'])
            dst = self._resolve(r['dst'], allow_internal=True)
            if not os.path.lexists(dst) and self._hash_or_none(src) == r['sha256']:
                continue  # never moved (crash between INTENT and move)
            got = self._hash_or_none(dst)
            occupied = os.path.lexists(src)
            ok = got == r['sha256'] and not occupied
            checks.append({'path': r['src'], 'quarantine_sha256': got, 'expected_sha256': r['sha256'],
                           'original_occupied': occupied, 'ok': ok})
            todo.append(dict(r))
        bad = [c for c in checks if not c['ok']]
        if bad:
            reason = 'ORIGINAL_OCCUPIED' if any(c['original_occupied'] for c in bad) else 'DESTINATION_CHANGED'
            return {'op_id': op_id, 'restored': [],
                    'verification': {'status': 'REFUSED', 'reason': reason, 'checks': checks}}
        self._set_op(op_id, 'UNDOING')
        restored = []
        for r in todo:
            self._set_entry(r['entry_id'], 'UNDO_INTENT')
            self._no_clobber_move(self._resolve(r['dst'], allow_internal=True), self._resolve(r['src']))
            self._set_entry(r['entry_id'], 'RESTORED')
            restored.append(r['src'])
        verification = self._verify_moves(todo, forward=False)
        self._set_op(op_id, 'UNDONE' if verification['status'] == 'CONFIRMED' else 'UNDO_FAILED', verification)
        return {'op_id': op_id, 'restored': restored, 'verification': verification}

    # --------------------------------------------------------- new content
    def _write_exclusive(self, dst: Path, chunks) -> None:
        fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o644)
        try:
            for chunk in chunks:
                os.write(fd, chunk)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _record_simple(self, kind: str, rel: str, digest: str, size: int, verification: dict) -> str:
        op_id = 'op_' + uuid.uuid4().hex[:16]
        with closing(self._db()) as con, con:
            con.execute('INSERT INTO fw_ops VALUES (?,?,?,?,?,?,?,?)',
                        (op_id, kind, None, str(self.root), verification['status'], _now(), _now(),
                         json.dumps(verification)))
            con.execute('INSERT INTO fw_journal(op_id,seq,src,dst,sha256,size,state,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                        (op_id, 0, '', rel, digest, size, 'CREATED', _now()))
        return op_id

    def write_note(self, name: str, text: str) -> dict[str, Any]:
        name = self._plain_name(name)
        if not name.lower().endswith('.txt'):
            raise FileWorkerError('NOT_TXT', name)
        data = text.encode('utf-8')
        if len(data) > MAX_NOTE_BYTES:
            raise FileWorkerError('NOTE_TOO_LARGE', name)
        dst = self._resolve(name)
        if os.path.lexists(dst):
            raise FileWorkerError('OVERWRITE_REFUSED', name)
        expected = hashlib.sha256(data).hexdigest()
        try:
            self._write_exclusive(dst, [data])
        except FileExistsError as exc:
            raise FileWorkerError('OVERWRITE_REFUSED', name) from exc
        got = self._hash_or_none(dst)
        verification = {'status': 'CONFIRMED' if got == expected else 'FAILED',
                        'expected_sha256': expected, 'observed_sha256': got}
        self._record_simple('write_note', name, expected, len(data), verification)
        if got != expected:
            raise FileWorkerError('VERIFY_FAILED', name)
        return {'path': name, 'sha256': got}

    def export_report(self, rel_path: str, dest_name: str) -> dict[str, Any]:
        src = self._resolve(rel_path)
        if os.path.islink(src) or not src.is_file():
            raise FileWorkerError('NOT_REGULAR_FILE', rel_path)
        dest_name = self._plain_name(dest_name)
        exports = self._resolve(EXPORTS)
        if os.path.lexists(exports) and not exports.is_dir():
            raise FileWorkerError('EXPORTS_NOT_DIR', EXPORTS)
        exports.mkdir(exist_ok=True)
        dst = self._resolve(f'{EXPORTS}/{dest_name}')
        if os.path.lexists(dst):
            raise FileWorkerError('OVERWRITE_REFUSED', dest_name)
        expected, size = self._hash(src)

        def chunks():
            fd = os.open(str(src), os.O_RDONLY | _NOFOLLOW | _BINARY)
            try:
                while True:
                    c = os.read(fd, CHUNK)
                    if not c:
                        return
                    yield c
            finally:
                os.close(fd)
        try:
            self._write_exclusive(dst, chunks())
        except FileExistsError as exc:
            raise FileWorkerError('OVERWRITE_REFUSED', dest_name) from exc
        got = self._hash_or_none(dst)
        rel = f'{EXPORTS}/{dest_name}'
        verification = {'status': 'CONFIRMED' if got == expected else 'FAILED',
                        'expected_sha256': expected, 'observed_sha256': got}
        self._record_simple('export_report', rel, expected, size, verification)
        if got != expected:
            raise FileWorkerError('VERIFY_FAILED', rel)
        return {'path': rel, 'sha256': got}
