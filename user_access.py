"""Telegram account bans and observed Mini App installations (not hardware IDs)."""
import hashlib
import json
import re
import time

from aiohttp import web


class UserAccess:
    def __init__(self, store):
        self.store = store
        with store.db:
            store.db.execute('CREATE TABLE IF NOT EXISTS bot_access (uid TEXT PRIMARY KEY,username TEXT,blocked INTEGER NOT NULL DEFAULT 0,last_seen REAL)')
            store.db.execute('CREATE TABLE IF NOT EXISTS miniapp_devices (id TEXT PRIMARY KEY,uid TEXT,platform TEXT,agent TEXT,first_seen REAL,last_seen REAL,blocked INTEGER NOT NULL DEFAULT 0)')

    def touch(self, uid, username=None):
        with self.store.db:
            self.store.db.execute('INSERT INTO bot_access(uid,username,last_seen) VALUES (?,?,?) ON CONFLICT(uid) DO UPDATE SET username=excluded.username,last_seen=excluded.last_seen',
                                  (str(uid), username, time.time()))

    def blocked(self, uid):
        row = self.store.db.execute('SELECT blocked FROM bot_access WHERE uid=?', (str(uid),)).fetchone()
        return bool(row and row[0])

    def observe(self, uid, headers):
        client = headers.get('X-Miniapp-Device', '')
        agent = headers.get('User-Agent', '')[:300]
        platform = headers.get('X-Telegram-Platform', 'unknown')[:40]
        if not re.fullmatch('[a-f0-9]{32}', client):
            client = 'legacy:'+agent
            platform = 'unknown'
        device_id = hashlib.sha256((str(uid)+'|'+client).encode()).hexdigest()
        clock = time.time()
        with self.store.db:
            # Bound client-supplied metadata without deleting blocked installations.
            known = self.store.db.execute('SELECT blocked FROM miniapp_devices WHERE id=?', (device_id,)).fetchone()
            count = self.store.db.execute('SELECT count(*) FROM miniapp_devices WHERE uid=?', (str(uid),)).fetchone()[0]
            if known is not None or count < 50:
                self.store.db.execute('INSERT INTO miniapp_devices(id,uid,platform,agent,first_seen,last_seen) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET platform=excluded.platform,agent=excluded.agent,last_seen=excluded.last_seen',
                                      (device_id, str(uid), platform, agent, clock, clock))
        return bool(known and known[0])


def blocked(backend, uid):
    access = getattr(getattr(backend, 'store', None), 'access', None)
    return bool(access and access.blocked(uid))


def mount_audience(app, moderator):
    backend = moderator.backend
    access = backend.store.access

    def owner_uid():
        path = backend.store.directory/'moderator-identity.json'
        return str(json.loads(path.read_text(encoding='utf-8'))['telegram_id']) if path.exists() else None

    async def audience(request):
        known = {uid: dict(uid=uid, username=None, blocked=access.blocked(uid), last_seen=None,
                           code=moderator.code(uid), connected=bool(user.get('auth')))
                 for uid, user in backend.USERS.items()}
        for uid, username in backend.store.db.execute('SELECT user,username FROM telegram_profiles'):
            known.setdefault(uid, dict(uid=uid, username=username, blocked=access.blocked(uid), last_seen=None,
                                      code=moderator.code(uid), connected=False))
            known[uid]['username'] = username
        for uid, username, ban, seen in backend.store.db.execute('SELECT uid,username,blocked,last_seen FROM bot_access'):
            entry = known.setdefault(uid, dict(uid=uid, code=moderator.code(uid), connected=False))
            entry.update(username=username, blocked=bool(ban), last_seen=seen)
        devices = [dict(id=r[0], uid=r[1], code=moderator.code(r[1]), platform=r[2], agent=r[3], first_seen=r[4], last_seen=r[5], blocked=bool(r[6]))
                   for r in backend.store.db.execute('SELECT id,uid,platform,agent,first_seen,last_seen,blocked FROM miniapp_devices ORDER BY last_seen DESC')]
        for entry in known.values():
            entry['owner'] = entry['uid'] == owner_uid()
        return web.json_response({'users': list(known.values()), 'devices': devices})

    async def change(request):
        data = await request.json()
        if not isinstance(data, dict) or type(data.get('blocked')) is not bool:
            raise web.HTTPBadRequest(text='Укажи состояние блокировки.')
        device_id = request.match_info.get('device')
        uid = request.match_info.get('uid')
        if device_id:
            row = backend.store.db.execute('SELECT uid FROM miniapp_devices WHERE id=?', (device_id,)).fetchone()
            if not row:
                raise web.HTTPNotFound(text='Устройство не найдено.')
            uid = row[0]
        elif uid not in backend.USERS and not backend.store.db.execute('SELECT 1 FROM bot_access WHERE uid=? UNION SELECT 1 FROM telegram_profiles WHERE user=?', (uid, uid)).fetchone():
            raise web.HTTPNotFound(text='Пользователь не найден.')
        if uid == owner_uid():
            raise web.HTTPBadRequest(text='Блокировка владельца недоступна.')
        import asyncio
        locks = getattr(backend.engine, 'user_booking_locks', None)
        if locks is None:
            backend.engine.user_booking_locks = locks = {}
        async with locks.setdefault(uid, asyncio.Lock()):
            with backend.store.db:
                if device_id:
                    backend.store.db.execute('UPDATE miniapp_devices SET blocked=? WHERE id=?', (int(data['blocked']), device_id))
                else:
                    backend.store.db.execute('INSERT INTO bot_access(uid,blocked) VALUES (?,?) ON CONFLICT(uid) DO UPDATE SET blocked=excluded.blocked', (uid, int(data['blocked'])))
            if not device_id and data['blocked']:
                for (task_uid, _), task in list(getattr(backend.engine, 'tasks', {}).items()):
                    if task_uid == uid:
                        task.cancel()
            moderator.audit(('block_' if data['blocked'] else 'unblock_')+('device' if device_id else 'user'), moderator.code(uid))
        return web.json_response({'ok': True})

    app.router.add_get('/api/admin/audience', audience)
    app.router.add_put('/api/admin/audience/users/{uid}', change)
    app.router.add_put('/api/admin/audience/devices/{device}', change)
