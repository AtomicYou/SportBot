"""One-time Telegram pairing and revocable, short-lived Android sessions."""
import hashlib
import json
import secrets
import time
from collections import deque

from aiohttp import web


class ModeratorAuth:
    def __init__(self, store):
        self.store = store
        self.attempts = deque()
        with store.db:
            store.db.execute('CREATE TABLE IF NOT EXISTS moderator_pairing (hash TEXT PRIMARY KEY, expires REAL)')
            store.db.execute('CREATE TABLE IF NOT EXISTS moderator_sessions (hash TEXT PRIMARY KEY, expires REAL, telegram_id TEXT)')
            columns = {row[1] for row in store.db.execute('PRAGMA table_info(moderator_sessions)')}
            for name, kind in (('id', 'TEXT'), ('device', 'TEXT'), ('created', 'REAL'), ('last_seen', 'REAL')):
                if name not in columns:
                    store.db.execute(f'ALTER TABLE moderator_sessions ADD COLUMN {name} {kind}')
            for row in store.db.execute('SELECT hash FROM moderator_sessions WHERE id IS NULL').fetchall():
                store.db.execute('UPDATE moderator_sessions SET id=?,device=? WHERE hash=?',
                                 (secrets.token_hex(16), 'Старая версия приложения', row[0]))

    def owner(self):
        path = self.store.directory / 'moderator-identity.json'
        if not path.exists():
            return None
        return str(json.loads(path.read_text(encoding='utf-8'))['telegram_id'])

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def issue(self, uid):
        if self.owner() != str(uid):
            raise PermissionError('Only the pinned Telegram owner can pair a device')
        code = secrets.token_urlsafe(24)
        with self.store.db:
            self.store.db.execute('DELETE FROM moderator_pairing')
            self.store.db.execute('INSERT INTO moderator_pairing VALUES (?,?)', (self.digest(code), time.time()+300))
        return code

    def exchange(self, code, device='Android'):
        clock = time.time()
        while self.attempts and self.attempts[0] < clock-60:
            self.attempts.popleft()
        if len(self.attempts) >= 30:
            raise web.HTTPTooManyRequests(text='Слишком много попыток. Подожди минуту.')
        self.attempts.append(clock)
        if not isinstance(device, str) or not 1 <= len(device.strip()) <= 100 or any(ord(c) < 32 for c in device):
            raise web.HTTPBadRequest(text='Некорректное название устройства.')
        if not isinstance(code, str) or not 20 <= len(code) <= 100 or not self.owner():
            raise web.HTTPUnauthorized(text='Код недействителен. Получи новый: /moderation')
        token = secrets.token_urlsafe(32)
        with self.store.db:
            self.store.db.execute('DELETE FROM moderator_pairing WHERE expires <= ?', (clock,))
            self.store.db.execute('DELETE FROM moderator_sessions WHERE expires <= ?', (clock,))
            changed = self.store.db.execute('DELETE FROM moderator_pairing WHERE hash = ? AND expires > ?',
                                           (self.digest(code), clock)).rowcount
            if changed != 1:
                raise web.HTTPUnauthorized(text='Код использован или устарел. Получи новый: /moderation')
            self.store.db.execute('INSERT INTO moderator_sessions(hash,expires,telegram_id,id,device,created,last_seen) VALUES (?,?,?,?,?,?,?)',
                                 (self.digest(token), clock+1800, self.owner(), secrets.token_hex(16), device.strip(), clock, clock))
        return token

    def validate(self, raw):
        if not raw.startswith('Bearer ') or len(raw) > 200:
            raise web.HTTPForbidden(text='Открой приложение Inno Sport Admin и войди через /moderation.')
        row = self.store.db.execute('SELECT telegram_id FROM moderator_sessions WHERE hash=? AND expires>?',
                                   (self.digest(raw[7:]), time.time())).fetchone()
        if not row or row[0] != self.owner():
            raise web.HTTPUnauthorized(text='Сессия истекла. Получи новый код: /moderation')
        with self.store.db:
            self.store.db.execute('UPDATE moderator_sessions SET last_seen=? WHERE hash=?', (time.time(), self.digest(raw[7:])))
        return row[0]

    def revoke(self, raw=None):
        with self.store.db:
            if raw:
                self.store.db.execute('DELETE FROM moderator_sessions WHERE hash=?', (self.digest(raw[7:]),))
            else:
                self.store.db.execute('DELETE FROM moderator_sessions')
                self.store.db.execute('DELETE FROM moderator_pairing')

    def mount(self, app):
        async def login(request):
            data = await request.json()
            token = self.exchange(data.get('code') if isinstance(data, dict) else None,
                                  data.get('device', 'Android') if isinstance(data, dict) else 'Android')
            return web.json_response({'token': token, 'expires_in': 1800})

        async def logout(request):
            self.revoke(request.headers.get('Authorization', ''))
            return web.json_response({'ok': True})

        async def devices(request):
            current = self.digest(request.headers['Authorization'][7:])
            rows = self.store.db.execute('SELECT id,device,created,last_seen,expires,hash FROM moderator_sessions WHERE expires>? AND telegram_id=? ORDER BY created DESC',
                                         (time.time(), self.owner())).fetchall()
            return web.json_response({'devices': [dict(id=r[0] or 'legacy', device=r[1] or 'Старая версия приложения',
                created=r[2], last_seen=r[3], expires=r[4], current=r[5] == current) for r in rows]})

        async def remove_device(request):
            session_id = request.match_info['id']
            with self.store.db:
                if session_id == 'legacy':
                    count = self.store.db.execute('DELETE FROM moderator_sessions WHERE id IS NULL').rowcount
                else:
                    count = self.store.db.execute('DELETE FROM moderator_sessions WHERE id=?', (session_id,)).rowcount
            if not count:
                raise web.HTTPNotFound(text='Сессия уже завершена.')
            return web.json_response({'ok': True})

        async def revoke_others(request):
            current = self.digest(request.headers['Authorization'][7:])
            with self.store.db:
                self.store.db.execute('DELETE FROM moderator_sessions WHERE hash != ?', (current,))
                self.store.db.execute('DELETE FROM moderator_pairing')
            return web.json_response({'ok': True})

        app.router.add_post('/api/moderator/login', login)
        app.router.add_post('/api/admin/logout', logout)
        app.router.add_get('/api/admin/devices', devices)
        app.router.add_delete('/api/admin/devices/{id}', remove_device)
        app.router.add_post('/api/admin/devices/revoke-others', revoke_others)
