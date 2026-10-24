"""Storage, portal client and booking coordination. No Telegram side effects on import."""
import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiohttp
from cryptography.fernet import Fernet
from controls import FeaturePolicy, enabled
from user_access import UserAccess, blocked
from booking_features import BookingStorage, BookingFeatures, window

MSK = ZoneInfo('Europe/Moscow')
ROOT = Path(__file__).resolve().parent
BASE = 'https://sport.innopolis.university'
log = logging.getLogger(__name__)


def now():
    return datetime.now(MSK)


def parse_date(value):
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=MSK) if dt.tzinfo is None else dt.astimezone(MSK)


def matches(wish, title, start):
    sport = wish['sport'].strip().casefold()
    title = title.casefold()
    return (bool(sport) and sport in title
            and not (sport == 'tennis' and 'table tennis' in title)
            and start.weekday() in wish['weekdays']
            and wish['time_from'] * 60 <= start.hour * 60 + start.minute
            < wish['time_to'] * 60)


class Store(BookingStorage):
    def __init__(self):
        directory = ROOT / 'data'
        directory.mkdir(exist_ok=True)
        self.directory = directory
        key = directory / 'secret.key'
        db = directory / 'bot.sqlite3'
        if not key.exists():
            if db.exists():
                raise RuntimeError('Missing data/secret.key; restore the original key.')
            with key.open('xb') as stream:
                stream.write(Fernet.generate_key())
        self.cipher = Fernet(key.read_bytes())
        self.db = sqlite3.connect(db)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.features = FeaturePolicy(self.db)
        self.access = UserAccess(self)
        self.users = {uid: json.loads(data) for uid, data in self.db.execute('SELECT id,data FROM users')}
        for user in self.users.values():
            for wish in user.get('wishlist', []):
                wish.setdefault('id', uuid4().hex)
        self._migrate_bookings()
        self._restore_telegram_profiles()
        self.init_booking_features()

    def _migrate_bookings(self):
        with self.db:
            old = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bookings'").fetchone()
            approved = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='approved_bookings'").fetchone()
            if old and not approved:
                self.db.execute('ALTER TABLE bookings RENAME TO approved_bookings')
            else:
                self.db.execute('CREATE TABLE IF NOT EXISTS approved_bookings (user TEXT, training TEXT, PRIMARY KEY(user, training))')
                if old:
                    self.db.execute('INSERT OR IGNORE INTO approved_bookings(user,training) SELECT user,training FROM bookings')
                    self.db.execute('DROP TABLE bookings')
            self.db.execute('CREATE TABLE IF NOT EXISTS cancelled_bookings (user TEXT, training TEXT, cancelled_at TEXT, PRIMARY KEY(user, training))')
            for table in ('approved_bookings','cancelled_bookings'):
                columns = {row[1] for row in self.db.execute(f'PRAGMA table_info({table})')}
                for column in ('training_title','telegram_username'):
                    if column not in columns:
                        self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} TEXT')
            for uid, user in self.users.items():
                for tid in user.get('cancelled', []):
                    self.db.execute('INSERT OR IGNORE INTO cancelled_bookings(user,training,cancelled_at) VALUES (?,?,?)', (str(uid),str(tid),None))
                self.db.execute('UPDATE users SET data=? WHERE id=?', (self._payload(user),uid))
            self.db.execute('DELETE FROM approved_bookings WHERE EXISTS (SELECT 1 FROM cancelled_bookings c WHERE c.user=approved_bookings.user AND c.training=approved_bookings.training)')
        for uid, user in self.users.items():
            user['cancelled'] = [row[0] for row in self.db.execute('SELECT training FROM cancelled_bookings WHERE user=?',(uid,))]

    @staticmethod
    def _payload(user):
        # The list in memory is a cache; SQLite is the persistent cancellation list.
        return json.dumps({key:value for key,value in user.items() if key != 'cancelled'})

    async def save(self):
        # No await inside transaction: handlers cannot interleave a snapshot write.
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO users VALUES (?,?)',
                                [(uid, self._payload(data)) for uid, data in self.users.items()])

    def booked(self, uid, tid):
        return self.db.execute('SELECT 1 FROM approved_bookings WHERE user=? AND training=?',
                               (str(uid), str(tid))).fetchone() is not None

    def _restore_telegram_profiles(self):
        self.db.execute('CREATE TABLE IF NOT EXISTS telegram_profiles (user TEXT PRIMARY KEY, username TEXT)')
        self.db.commit()
        journal = self.directory / 'journal.sqlite3'
        if journal.exists():
            source = sqlite3.connect(journal.as_uri()+'?mode=ro',uri=True)
            try:
                for uid, username in source.execute('SELECT telegram_id,username FROM telegram_users'):
                    self.remember_telegram_user(uid,username)
            finally:
                source.close()

    def remember_telegram_user(self, uid, username):
        uid = str(uid)
        username = '@'+username.lstrip('@') if username else None
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO telegram_profiles VALUES (?,?)',(uid,username))
            for table in ('approved_bookings','cancelled_bookings'):
                self.db.execute(f'UPDATE {table} SET telegram_username=? WHERE user=?',(username,uid))

    def _booking_metadata(self, uid, tid, title):
        row = self.db.execute('SELECT username FROM telegram_profiles WHERE user=?',(uid,)).fetchone()
        username = row[0] if row else None
        if not title:
            row = self.db.execute('SELECT training_title FROM approved_bookings WHERE user=? AND training=? UNION ALL SELECT training_title FROM cancelled_bookings WHERE user=? AND training=?',(uid,tid,uid,tid)).fetchone()
            title = row[0] if row else None
        return title, username

    def update_booking_titles(self, uid, events):
        with self.db:
            for event in events:
                tid = event.get('extendedProps',{}).get('id')
                title = event.get('title')
                if tid is not None and isinstance(title,str) and title:
                    for table in ('approved_bookings','cancelled_bookings'):
                        self.db.execute(f'UPDATE {table} SET training_title=? WHERE user=? AND training=?',(title,str(uid),str(tid)))

    def mark_booked(self, uid, tid, title=None):
        uid, tid = str(uid), str(tid)
        title, username = self._booking_metadata(uid,tid,title)
        with self.db:
            self.db.execute('INSERT INTO approved_bookings(user,training,training_title,telegram_username) VALUES (?,?,?,?) ON CONFLICT(user,training) DO UPDATE SET training_title=coalesce(excluded.training_title,training_title),telegram_username=excluded.telegram_username', (uid,tid,title,username))
            self.db.execute('DELETE FROM cancelled_bookings WHERE user=? AND training=?',(uid,tid))
            self.db.execute('DELETE FROM waitlist WHERE user=? AND training=?',(uid,tid))
        user = self.users.get(uid)
        if user is not None:
            user['cancelled'] = [item for item in user.get('cancelled',[]) if item != tid]

    def mark_cancelled(self, uid, tid, title=None):
        uid, tid = str(uid), str(tid)
        title, username = self._booking_metadata(uid,tid,title)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cancelled_bookings(user,training,cancelled_at,training_title,telegram_username) VALUES (?,?,?,?,?)',(uid,tid,now().isoformat(),title,username))
            self.db.execute('DELETE FROM approved_bookings WHERE user=? AND training=?',(uid,tid))
            self.db.execute('DELETE FROM waitlist WHERE user=? AND training=?',(uid,tid))
            self.db.execute('DELETE FROM booking_times WHERE user=? AND training=?',(uid,tid))
        user = self.users.get(uid)
        if user is not None and tid not in user.setdefault('cancelled',[]):
            user['cancelled'].append(tid)


class PortalError(Exception):
    pass


class AuthExpired(PortalError):
    pass


class Portal:
    def __init__(self):
        self.http = None
        self.health = {"checked": None, "ok": None, "http_status": None}

    async def request(self, auth, method, path, params=None):
        if not auth or not auth.get('csrf') or not auth.get('session'):
            raise AuthExpired('Account is not connected')
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with self.http.request(method, BASE + path, params=params,
                                         cookies={'csrftoken': auth['csrf'], 'sessionid': auth['session']},
                                         headers={'X-CSRFToken': auth['csrf'], 'Referer': BASE + '/',
                                                  'Accept': 'application/json'}, allow_redirects=False) as response:
                self.health = {"checked": time.time(), "ok": response.status < 500, "http_status": response.status}
                if response.status in (301, 302, 303, 307, 308, 401, 403):
                    raise AuthExpired('Portal rejected the session')
                if method == 'GET':
                    if response.status != 200:
                        raise PortalError(f'Portal HTTP {response.status}')
                    try:
                        data = await response.json()
                    except (ValueError, aiohttp.ContentTypeError) as exc:
                        raise PortalError('Portal returned non-JSON content') from exc
                    if not isinstance(data, list):
                        raise PortalError('Unexpected calendar response')
                    return data
                return response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self.health = {'checked': time.time(), 'ok': False, 'http_status': None}
            raise PortalError('Portal connection failed') from exc

    async def schedule(self, auth, start, end):
        return await self.request(auth, 'GET', '/api/calendar/trainings',
                                  {'start': start + 'T00:00:00', 'end': end + 'T00:00:00', 'timeZone': 'Europe/Moscow'})

    async def book(self, auth, tid):
        return await self.request(auth, 'POST', f'/api/training/{tid}/check_in')

    async def cancel(self, auth, tid):
        return await self.request(auth, 'POST', f'/api/training/{tid}/cancel_check_in')

    async def close(self):
        if self.http:
            await self.http.close()


class Engine:
    def __init__(self, app, store, portal):
        self.scan_failures = 0
        self.app, self.store, self.portal = app, store, portal
        self.lock = asyncio.Lock()
        self.tasks = {}
        self.slot_locks = {}
        self.auth_locks = {}
        self.user_booking_locks = {}
        self.features = BookingFeatures(self)

    def active(self, uid, tid, title, start):
        user = self.store.users.get(str(uid), {})
        return (not blocked(self.app, uid) and enabled(self.app, 'auto_booking') and not user.get('paused')
                and (str(tid) not in user.get('cancelled', []) or self.store.is_waiting(uid, tid))
                and not self.store.booked(uid, tid)
                and (self.store.is_waiting(uid, tid) or any(matches(w, title, start) for w in user.get('wishlist', []))))

    async def enrich_booking_metadata(self):
        rows = self.store.db.execute('SELECT user FROM approved_bookings WHERE training_title IS NULL UNION SELECT user FROM cancelled_bookings WHERE training_title IS NULL').fetchall()
        monday = now().date()-timedelta(days=now().weekday())
        for (uid,) in rows:
            if not self.store.users.get(uid,{}).get('auth'):
                continue
            try:
                auth = await self.auth(uid)
                for offset in (0,7):
                    start = monday+timedelta(days=offset)
                    events = await self.portal.schedule(auth,str(start),str(start+timedelta(days=7)))
                    self.store.update_booking_titles(uid,events)
            except PortalError:
                log.warning('Could not recover historical booking titles')

    async def auth(self, uid, refresh=False):
        uid = str(uid)
        async with self.auth_locks.setdefault(uid, asyncio.Lock()):
            user = self.store.users[uid]
            if refresh:
                creds = user.get('credentials')
                if not creds:
                    raise AuthExpired('Sign in again')
                tokens = await self.app.auto_login(creds['username'], self.app.decrypt_data(creds['password']))
                if not tokens:
                    raise AuthExpired('Sign in again')
                user['auth'] = {k: self.app.encrypt_data(v) for k, v in tokens.items()}
                await self.store.save()
            return self.app.get_decrypted_auth(user)

    async def notify(self, uid, text):
        if blocked(self.app, uid):
            return
        try:
            await self.app.bot.send_message(uid, text)
        except Exception:
            log.warning('Could not deliver Telegram notification')

    async def attempt(self, uid, tid, title, start, end=None):
        key = (str(uid), str(tid))
        async with self.slot_locks.setdefault(key, asyncio.Lock()), self.user_booking_locks.setdefault(str(uid), asyncio.Lock()):
            if not self.active(uid, tid, title, start):
                return True
            end = end or start + timedelta(hours=1)
            if await self.features.conflict(uid, tid, start, end):
                return False
            if not self.active(uid, tid, title, start):
                return True
            auth = await self.auth(uid)
            if not self.active(uid, tid, title, start):
                return True
            try:
                status = await self.portal.book(auth, tid)
            except AuthExpired:
                auth = await self.auth(uid, refresh=True)
                if not self.active(uid, tid, title, start):
                    return True
                status = await self.portal.book(auth, tid)
            if status in (200, 201):
                self.store.mark_booked(uid, tid, title=title)
                self.store.remember_interval(uid, tid, start, end)
                await self.notify(uid, f'✅ Записал на {title}\n{start:%d.%m %H:%M}')
                return True
            if status == 429:
                await asyncio.sleep(15)
            return False

    async def snipe(self, uid, tid, title, start, end=None):
        opening = start - timedelta(days=7)
        try:
            await asyncio.sleep(max(0, (opening - timedelta(minutes=1) - now()).total_seconds()))
            if not self.active(uid, tid, title, start):
                return
            # Validate the current session before the short booking window.
            monday = now().date() - timedelta(days=now().weekday())
            try:
                await self.portal.schedule(await self.auth(uid), str(monday), str(monday + timedelta(days=7)))
            except AuthExpired:
                await self.auth(uid, refresh=True)
            while now() < opening + timedelta(seconds=90):
                if not self.active(uid, tid, title, start):
                    return
                try:
                    if await self.attempt(uid, tid, title, start, end=end):
                        return
                except AuthExpired:
                    await self.notify(uid, '🔐 Не удалось обновить вход. Авторизуйся заново.')
                    return
                except PortalError:
                    log.warning('Booking request failed; retrying')
                await asyncio.sleep(1)
            await self.notify(uid, f'⏳ Не удалось записаться при открытии: {title}, {start:%d.%m %H:%M}. Продолжу обычный поиск.')
        except PortalError:
            log.warning('Could not prepare automatic booking')
            await self.notify(uid, '⚠️ Не удалось подготовить запись: портал недоступен или требуется повторный вход.')

    async def scan(self, only_uid=None):
        # Manual search and timer share one lock to prevent duplicate requests.
        async with self.lock:
            if not enabled(self.app, 'auto_booking'):
                return
            monday = now().date() - timedelta(days=now().weekday())
            for uid, user in list(self.store.users.items()):
                if only_uid is not None and str(only_uid) != uid:
                    continue
                if blocked(self.app, uid) or user.get('paused') or not user.get('auth') or not (user.get('wishlist') or self.store.waiting(uid)):
                    continue
                try:
                    auth = await self.auth(uid)
                    for offset in (0, 7):
                        start = monday + timedelta(days=offset)
                        try:
                            schedule = await self.portal.schedule(auth, str(start), str(start + timedelta(days=7)))
                        except AuthExpired:
                            auth = await self.auth(uid, refresh=True)
                            schedule = await self.portal.schedule(auth, str(start), str(start + timedelta(days=7)))
                        for event in schedule:
                            try:
                                extra = event.get('extendedProps', {})
                                tid, title, dt = extra.get('id'), event.get('title', ''), parse_date(event['start'])
                                if tid is None or dt <= now() or not self.active(uid, tid, title, dt):
                                    continue
                                if extra.get('checked_in'):
                                    self.store.mark_booked(uid, tid, title=title)
                                    continue
                                if extra.get('can_check_in'):
                                    await self.attempt(uid, tid, title, dt, end=window(event)[1])
                                elif now() < dt - timedelta(days=7) + timedelta(seconds=90):
                                    key = (uid, str(tid))
                                    if key not in self.tasks or self.tasks[key].done():
                                        self.tasks[key] = asyncio.create_task(self.snipe(uid, tid, title, dt, end=window(event)[1]))
                            except (KeyError, ValueError, TypeError):
                                log.warning('Skipped malformed calendar event')
                except PortalError:
                    self.scan_failures += 1
                    log.warning('Calendar scan failed for one account')
            self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.portal.close()
