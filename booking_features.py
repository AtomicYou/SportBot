"""Persistent waiting lists, booking intervals and reminders."""
import asyncio
from user_access import blocked
import logging
from datetime import timedelta

log = logging.getLogger(__name__)


def window(event):
    from core import parse_date
    start = parse_date(event['start'])
    end = parse_date(event['end']) if event.get('end') else start + timedelta(hours=1)
    if end <= start:
        end = start + timedelta(hours=1)
    return start, end


class BookingStorage:
    def init_booking_features(self):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS waitlist (
                user TEXT, training TEXT, title TEXT, start TEXT, end TEXT,
                PRIMARY KEY(user,training));
            CREATE TABLE IF NOT EXISTS booking_times (
                user TEXT, training TEXT, start TEXT, end TEXT, confirmed_at TEXT,
                PRIMARY KEY(user,training));
            CREATE TABLE IF NOT EXISTS reminder_deliveries (
                user TEXT, training TEXT, start TEXT, lead INTEGER, sent_at TEXT,
                attempted_at TEXT, PRIMARY KEY(user,training,start,lead));
        ''')

    def waiting(self, uid):
        from core import now
        return [dict(id=row[0], title=row[1], start=row[2], end=row[3]) for row in self.db.execute(
            'SELECT training,title,start,end FROM waitlist WHERE user=? AND start>? ORDER BY start', (str(uid), now().isoformat()))]

    def is_waiting(self, uid, tid):
        return bool(self.db.execute('SELECT 1 FROM waitlist WHERE user=? AND training=?',
                                    (str(uid), str(tid))).fetchone())

    def add_waiting(self, uid, event):
        start, end = window(event)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO waitlist VALUES (?,?,?,?,?)',
                            (str(uid), str(event['extendedProps']['id']), str(event.get('title', 'Занятие')),
                             start.isoformat(), end.isoformat()))

    def remove_waiting(self, uid, tid):
        with self.db:
            self.db.execute('DELETE FROM waitlist WHERE user=? AND training=?', (str(uid), str(tid)))

    def remember_interval(self, uid, tid, start, end):
        from core import now
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO booking_times VALUES (?,?,?,?,?)',
                            (str(uid), str(tid), start.isoformat(), end.isoformat(), now().isoformat()))


class BookingFeatures:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def health(self, category, uid, ok):
        alerts = getattr(self.engine.app, 'owner_alerts', None)
        if alerts is not None:
            alerts.observe(category, str(uid), ok)

    async def calendar(self, uid):
        from core import AuthExpired, PortalError, now
        monday = now().date() - timedelta(days=now().weekday())
        try:
            auth = await self.engine.auth(uid)
            events = []
            for offset in (0, 7):
                start = monday + timedelta(days=offset)
                try:
                    week = await self.engine.portal.schedule(auth, str(start), str(start + timedelta(days=7)))
                except AuthExpired:
                    auth = await self.engine.auth(uid, refresh=True)
                    week = await self.engine.portal.schedule(auth, str(start), str(start + timedelta(days=7)))
                events.extend(week)
        except PortalError as error:
            self.health('portal', uid, False)
            error.owner_alert_reported = True
            raise
        self.health('portal', uid, True)
        return events

    async def reminders(self):
        from core import now, parse_date
        for uid, user in list(self.store.users.items()):
            if blocked(self.engine.app, uid) or not user.get('auth'):
                continue
            try:
                events = await self.calendar(uid)
                changed = await self.engine.reconcile_portal(uid, events, complete=True)
                if changed and not user.get("paused"):
                    await self.engine.scan(uid, events_snapshot=events)
                for event in events:
                    await self.remind(uid, event)
                with self.store.db:
                    self.store.db.execute('DELETE FROM waitlist WHERE user=? AND start<=?', (uid, now().isoformat()))
                    # Compare parsed times below when deciding notifications; these ISO values are written in MSK.
                    self.store.db.execute('DELETE FROM reminder_deliveries WHERE start<?', ((now()-timedelta(days=30)).isoformat(),))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.log(logging.WARNING if getattr(error, 'owner_alert_reported', False) else logging.ERROR,
                        'Reminder calendar unavailable: %s', type(error).__name__,
                        extra={'owner_alert': not getattr(error, 'owner_alert_reported', False)})

    async def remind(self, uid, event):
        from core import now, parse_date
        try:
            extra = event.get('extendedProps', {})
            tid = str(extra['id'])
            start, _ = window(event)
        except (ValueError, KeyError, TypeError):
            return
        if not extra.get('checked_in') or start <= now():
            return
        async with self.engine.slot_locks.setdefault((str(uid), tid), asyncio.Lock()):
            user = self.store.users.get(str(uid), {})
            if blocked(self.engine.app, uid) or user.get('reminders', True) is False or tid in user.get('cancelled', []):
                return
            for lead, label in ((1440, 'завтра'), (60, 'через час')):
                due = start - timedelta(minutes=lead)
                clock = now()
                # Do not flood the user with missed notifications after a long outage.
                if not due <= clock < due + timedelta(minutes=15):
                    continue
                key = (str(uid), tid, start.isoformat(), lead)
                row = self.store.db.execute('SELECT sent_at,attempted_at FROM reminder_deliveries WHERE user=? AND training=? AND start=? AND lead=?', key).fetchone()
                if row and (row[0] or (row[1] and clock-parse_date(row[1]) < timedelta(minutes=5))):
                    continue
                with self.store.db:
                    self.store.db.execute('INSERT INTO reminder_deliveries VALUES (?,?,?,?,NULL,?) ON CONFLICT(user,training,start,lead) DO UPDATE SET attempted_at=excluded.attempted_at', (*key, clock.isoformat()))
                from aiogram import types
                keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
                    types.InlineKeyboardButton(text='Мои записи', callback_data='show_bookings')]])
                try:
                    await self.engine.app.bot.send_message(uid,
                        f'Напоминание: занятие {label}\n{event.get("title", "Занятие")}\n{start:%d.%m в %H:%M} · МСК',
                        reply_markup=keyboard)
                except Exception as error:
                    self.health('reminders', uid, False)
                    log.warning('Reminder delivery failed: %s', type(error).__name__, extra={'owner_alert': False})
                else:
                    self.health('reminders', uid, True)
                    with self.store.db:
                        self.store.db.execute('UPDATE reminder_deliveries SET sent_at=? WHERE user=? AND training=? AND start=? AND lead=?', (clock.isoformat(), *key))
