"""Moderator access pinned to a verified Telegram account; whitelist-only output."""
import hashlib
import hmac
import json
from urllib.parse import parse_qsl

from aiohttp import web
from core import now, parse_date, PortalError


class Moderator:
    def __init__(self, backend):
        self.backend = backend
        self.identity_path = backend.store.directory / 'moderator-identity.json'
        self.identity = json.loads(self.identity_path.read_text(encoding='utf-8')) if self.identity_path.exists() else None
        self.code_key = hashlib.sha256((backend.store.directory / 'secret.key').read_bytes()).digest()
        backend.store.db.execute('CREATE TABLE IF NOT EXISTS moderation_audit (time TEXT, action TEXT, subject TEXT, details TEXT)')
        backend.store.db.commit()

    def is_admin(self, uid, verified_raw):
        return bool(self.identity) and str(self.identity['telegram_id']) == str(uid)

    def code(self, uid):
        return 'U-' + hmac.new(self.code_key, str(uid).encode(), hashlib.sha256).hexdigest()[:12].upper()

    def uid(self, code):
        for uid, user in self.backend.USERS.items():
            if user.get('auth') and hmac.compare_digest(self.code(uid), code):
                return uid
        raise web.HTTPNotFound(text='Пользователь не найден.')

    def audit(self, action, code='', details=''):
        with self.backend.store.db:
            self.backend.store.db.execute('INSERT INTO moderation_audit VALUES (?,?,?,?)',
                (now().isoformat(), action, code, details))

    @staticmethod
    def wishes(user):
        return [{key: wish[key] for key in ('id', 'sport', 'weekdays', 'time_from', 'time_to')}
                for wish in user.get('wishlist', [])]

    def mount(self, app, load_week, cancel_training):
        from database_viewer import mount_database_viewer
        mount_database_viewer(app, self)
        from user_access import mount_audience
        mount_audience(app, self)
        async def overview(request):
            users = [{'code': self.code(uid), 'paused': bool(user.get('paused')), 'wish_count': len(user.get('wishlist', []))}
                     for uid, user in self.backend.USERS.items() if user.get('auth')]
            rows = self.backend.store.db.execute('SELECT time,action,subject,details FROM moderation_audit ORDER BY rowid DESC LIMIT 30').fetchall()
            return web.json_response({'features': self.backend.store.features.flags, 'users': users,
                'audit': [dict(zip(('time', 'action', 'subject', 'details'), row)) for row in rows]})

        async def user_info(request):
            code = request.match_info['code']
            uid = self.uid(code)
            user = self.backend.USERS[uid]
            bookings = {}
            portal_available = True
            try:
                for week in (0, 1):
                    _, events = await load_week(uid, week)
                    for event in events:
                        extra = event.get('extendedProps', {})
                        try:
                            start = parse_date(event['start'])
                            if extra.get('checked_in') and extra.get('id') is not None and start > now():
                                tid = str(extra['id'])
                                bookings[tid] = {'id': tid, 'title': str(event.get('title', 'Занятие')), 'start': start.isoformat()}
                        except (ValueError, TypeError, KeyError):
                            continue
            except PortalError:
                portal_available = False
            return web.json_response({'code': code, 'paused': bool(user.get('paused')), 'wishlist': self.wishes(user),
                'bookings': sorted(bookings.values(), key=lambda b: b['start']), 'portal_available': portal_available})

        async def require_request(request):
            data = await request.json()
            if not isinstance(data, dict) or data.get('requested_by_user') is not True:
                raise web.HTTPBadRequest(text='Подтверди, что пользователь попросил выполнить действие.')

        async def remove_wish(request):
            await require_request(request)
            code, wish_id = request.match_info['code'], request.match_info['id']
            uid = self.uid(code)
            user = self.backend.USERS[uid]
            if not any(w['id'] == wish_id for w in user.get('wishlist', [])):
                raise web.HTTPNotFound(text='Цель уже удалена.')
            user['wishlist'] = [w for w in user['wishlist'] if w['id'] != wish_id]
            await self.backend.save_db_async()
            self.audit('remove_wish', code, wish_id)
            return web.json_response({'ok': True})

        async def pause_user(request):
            await require_request(request)
            code = request.match_info['code']
            uid = self.uid(code)
            self.backend.USERS[uid]['paused'] = True
            await self.backend.save_db_async()
            self.audit('pause_user', code)
            return web.json_response({'ok': True})

        async def cancel(request):
            await require_request(request)
            code, tid = request.match_info['code'], request.match_info['id']
            uid = self.uid(code)
            await cancel_training(uid, tid)
            self.audit('cancel_booking', code, tid)
            return web.json_response({'ok': True})

        async def features(request):
            data = await request.json()
            self.backend.store.features.update(data)
            self.audit('features', details=json.dumps(data, sort_keys=True))
            return web.json_response({'ok': True, 'features': self.backend.store.features.flags})

        app.router.add_get('/api/admin/overview', overview)
        app.router.add_get('/api/admin/users/{code}', user_info)
        app.router.add_delete('/api/admin/users/{code}/wishes/{id}', remove_wish)
        app.router.add_post('/api/admin/users/{code}/pause', pause_user)
        app.router.add_post('/api/admin/users/{code}/bookings/{id}/cancel', cancel)
        app.router.add_put('/api/admin/features', features)
