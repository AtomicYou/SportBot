"""Owner diagnostics and consistent, verified SQLite backups on local storage."""
import asyncio
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from aiohttp import web

log = logging.getLogger(__name__)
NAME = re.compile(r'(daily|manual)-\d{8}T\d{6}-[a-f0-9]{8}')


class ServerOperations:
    def __init__(self, backend):
        self.backend = backend
        self.directory = Path(backend.store.directory).resolve()
        self.root = self.directory / 'backups' / 'automatic'
        self.started = time.time()
        self.monotonic = time.monotonic()
        self.lock = asyncio.Lock()
        self.last_backup_error = None
        self.scan = {'running': False, 'started': None, 'finished': None, 'ok': None}
        self.telegram = {'checked': None, 'ok': None}

    def catalogue(self):
        if not self.root.exists():
            return []
        result = []
        for path in self.root.iterdir():
            if not NAME.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
                continue
            try:
                value = json.loads((path/'manifest.json').read_text(encoding='utf-8'))
                result.append({'id': path.name, 'kind': value['kind'], 'created': value['created'],
                               'bytes': sum(f['bytes'] for f in value['files']), 'verified': value['verified']})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(result, key=lambda item: item['created'], reverse=True)

    def _create(self, kind):
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise ValueError('Backup directory must be a local directory')
        name = kind+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8]
        staging = self.root / ('.pending-'+uuid4().hex)
        staging.mkdir()
        try:
            files = []
            for filename in ('bot.sqlite3', 'journal.sqlite3'):
                source = sqlite3.connect((self.directory/filename).as_uri()+'?mode=ro', uri=True, timeout=10)
                target = sqlite3.connect(staging/filename)
                try:
                    deadline = time.monotonic()+60
                    def progress(status, remaining, total):
                        if time.monotonic() > deadline:
                            raise TimeoutError('SQLite backup timeout')
                    source.backup(target, pages=256, progress=progress)
                    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('SQLite backup integrity check failed')
                finally:
                    target.close()
                    source.close()
            # Encrypted credentials cannot be restored without the original key.
            for filename in ('secret.key', 'moderator-identity.json'):
                if (self.directory/filename).is_file():
                    shutil.copy2(self.directory/filename, staging/filename)
            for path in staging.iterdir():
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                files.append({'name': path.name, 'bytes': path.stat().st_size, 'sha256': digest})
            manifest = {'kind': kind, 'created': time.time(), 'verified': True, 'files': files}
            (staging/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
            staging.rename(self.root/name)
            for group, limit in (('daily', 14), ('manual', 10)):
                entries = sorted((p for p in self.root.iterdir() if NAME.fullmatch(p.name)
                                  and p.name.startswith(group+'-') and p.is_dir() and not p.is_symlink()),
                                 key=lambda p: p.stat().st_mtime_ns, reverse=True)
                for old in entries[limit:]:
                    if old.resolve().parent != self.root:
                        raise ValueError('Unsafe backup cleanup path')
                    shutil.rmtree(old)
            return name
        finally:
            if staging.exists() and staging.resolve().parent == self.root:
                shutil.rmtree(staging)

    async def backup(self, kind='manual'):
        if kind not in ('daily', 'manual'):
            raise ValueError('Invalid backup type')
        if self.lock.locked():
            raise web.HTTPConflict(text='Копия уже создаётся. Подожди завершения.')
        async with self.lock:
            try:
                name = await asyncio.to_thread(self._create, kind)
                self.last_backup_error = None
                return name
            except Exception:
                self.last_backup_error = time.time()
                log.error('Database backup failed')
                raise

    async def daily(self):
        entries = self.catalogue()
        latest = max((b['created'] for b in entries if b['kind'] == 'daily'), default=0)
        if time.time()-latest >= 86400 and not self.lock.locked():
            try:
                await self.backup('daily')
            except Exception:
                pass  # The failure is recorded and reported through owner alerts.

    async def status(self):
        bot = getattr(self.backend, 'bot', None)
        if bot and (not self.telegram['checked'] or time.time()-self.telegram['checked'] >= 60):
            try:
                await asyncio.wait_for(bot.get_me(), timeout=8)
                self.telegram = {'checked': time.time(), 'ok': True}
            except Exception:
                self.telegram = {'checked': time.time(), 'ok': False}
        scheduler = getattr(self.backend, 'scheduler', None)
        alerts = getattr(self.backend, 'owner_alerts', None)
        return {'started': self.started, 'uptime_seconds': int(time.monotonic()-self.monotonic),
                'server': 'running', 'telegram': self.telegram,
                'portal': getattr(getattr(self.backend, 'portal', None), 'health', {'checked': None, 'ok': None}),
                'scanner': self.scan, 'scheduler_running': bool(scheduler and scheduler.running),
                'users': len(self.backend.USERS), 'backups_running': self.lock.locked(),
                'backup_error_at': self.last_backup_error,
                'recent_errors': list(getattr(alerts, 'recent', []))[-20:]}

    def mount(self, app):
        async def status(request):
            return web.json_response(await self.status())

        async def backups(request):
            return web.json_response({'backups': self.catalogue(), 'running': self.lock.locked(),
                                     'last_error': self.last_backup_error, 'daily_keep': 14, 'manual_keep': 10})

        async def create(request):
            name = await self.backup()
            return web.json_response({'ok': True, 'id': name})

        app.router.add_get('/api/admin/system', status)
        app.router.add_get('/api/admin/backups', backups)
        app.router.add_post('/api/admin/backups', create)
