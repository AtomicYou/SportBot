"""Compare authenticated calendar snapshots without guessing who changed a booking."""
import json
from datetime import timedelta


class PortalSync:
    def __init__(self, store):
        self.store = store
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS portal_snapshots(user TEXT,training TEXT,state TEXT,last_seen TEXT,PRIMARY KEY(user,training));
            CREATE TABLE IF NOT EXISTS portal_changes(id INTEGER PRIMARY KEY,time TEXT,user TEXT,training TEXT,kind TEXT,title TEXT);
            CREATE TABLE IF NOT EXISTS portal_missing_booking(user TEXT,training TEXT,first_seen TEXT,PRIMARY KEY(user,training));
            CREATE TABLE IF NOT EXISTS portal_booking_receipts(user TEXT,training TEXT,confirmed_at TEXT,PRIMARY KEY(user,training));
        ''')

    def booked(self, uid, tid):
        from core import now
        self.store.db.execute('INSERT OR REPLACE INTO portal_booking_receipts VALUES (?,?,?)', (str(uid), str(tid), now().isoformat()))
        self.store.db.execute('DELETE FROM portal_missing_booking WHERE user=? AND training=?', (str(uid), str(tid)))

    def in_schedule(self, uid, tid):
        row = self.store.db.execute('SELECT state FROM portal_snapshots WHERE user=? AND training=?', (str(uid), str(tid))).fetchone()
        return row is None or json.loads(row[0]).get('present', True)

    def observe(self, uid, events, complete=False):
        from core import now, parse_date
        uid = str(uid)
        clock = now()
        cancelled, moved = [], []
        changed = False
        seen = set()
        for event in events:
            try:
                if not isinstance(event, dict) or not isinstance(event.get('extendedProps'), dict):
                    complete = False
                    continue
                extra = event['extendedProps']
                tid = str(extra['id'])
                if extra['id'] is None or type(extra.get('checked_in')) is not bool:
                    complete = False
                    continue  # Missing fields are not proof of cancellation.
                start = parse_date(event['start'])
                if start <= clock:
                    continue
                seen.add(tid)
                state = dict(present=True, title=str(event.get('title', 'Занятие')), start=event['start'], end=event.get('end'),
                             checked_in=extra['checked_in'], available=extra.get('can_check_in'),
                             load=extra.get('load'), capacity=extra.get('capacity'))
            except (KeyError, TypeError, ValueError):
                complete = False
                continue
            row = self.store.db.execute('SELECT state FROM portal_snapshots WHERE user=? AND training=?', (uid, tid)).fetchone()
            old = json.loads(row[0]) if row else None
            kinds = []
            if old is None:
                kinds.append('new_training')
            else:
                if not old.get('present', True):
                    kinds.append('training_restored')
                if (old['start'], old['end'], old['title']) != (state['start'], state['end'], state['title']):
                    kinds.append('training_changed')
                    moved.append(tid)
                if (old.get('available'), old.get('load'), old.get('capacity')) != (state['available'], state['load'], state['capacity']):
                    kinds.append('availability_changed')
                if old['checked_in'] != state['checked_in']:
                    kinds.append('booking_state_changed')
            changed = changed or bool(kinds)
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO portal_snapshots VALUES (?,?,?,?)', (uid, tid, json.dumps(state, ensure_ascii=False), clock.isoformat()))
                for kind in kinds:
                    self.store.db.execute('INSERT INTO portal_changes(time,user,training,kind,title) VALUES (?,?,?,?,?)', (clock.isoformat(), uid, tid, kind, state['title']))
            if state['checked_in']:
                if not self.store.booked(uid, tid):
                    self.store.mark_booked(uid, tid, title=state['title'])
                with self.store.db:
                    self.store.db.execute('DELETE FROM portal_missing_booking WHERE user=? AND training=?', (uid, tid))
                continue
            if not self.store.booked(uid, tid):
                continue
            receipt = self.store.db.execute('SELECT confirmed_at FROM portal_booking_receipts WHERE user=? AND training=?', (uid, tid)).fetchone()
            if receipt and (clock-parse_date(receipt[0])).total_seconds() < 120:
                continue  # The portal may lag after accepting a booking.
            row = self.store.db.execute('SELECT first_seen FROM portal_missing_booking WHERE user=? AND training=?', (uid, tid)).fetchone()
            if row is None:
                with self.store.db:
                    self.store.db.execute('INSERT INTO portal_missing_booking VALUES (?,?,?)', (uid, tid, clock.isoformat()))
            elif (clock-parse_date(row[0])).total_seconds() >= 30:
                self.store.mark_cancelled(uid, tid, title=state['title'])
                with self.store.db:
                    self.store.db.execute('DELETE FROM portal_missing_booking WHERE user=? AND training=?', (uid, tid))
                    self.store.db.execute('INSERT INTO portal_changes(time,user,training,kind,title) VALUES (?,?,?,?,?)', (clock.isoformat(), uid, tid, 'portal_booking_removed', state['title']))
                cancelled.append(state['title'])
        # Only a complete, nonempty two-week response can establish disappearance.
        # Empty/error responses and individual weeks must not remove other events.
        if complete and seen:
            scope_end = (clock-timedelta(days=clock.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)+timedelta(days=14)
            for tid, raw in self.store.db.execute('SELECT training,state FROM portal_snapshots WHERE user=?', (uid,)).fetchall():
                old = json.loads(raw)
                if tid in seen or not old.get('present', True) or not clock < parse_date(old['start']) < scope_end:
                    continue
                old['present'] = False
                with self.store.db:
                    self.store.db.execute('UPDATE portal_snapshots SET state=? WHERE user=? AND training=?', (json.dumps(old, ensure_ascii=False), uid, tid))
                    self.store.db.execute('INSERT INTO portal_changes(time,user,training,kind,title) VALUES (?,?,?,?,?)', (clock.isoformat(), uid, tid, 'training_missing', old['title']))
                moved.append(tid)
                changed = True
        with self.store.db:
            self.store.db.execute('DELETE FROM portal_changes WHERE id NOT IN (SELECT id FROM portal_changes ORDER BY id DESC LIMIT 5000)')
            self.store.db.execute("DELETE FROM portal_snapshots WHERE last_seen < ?", ((clock-timedelta(days=30)).isoformat(),))
        return cancelled, moved, changed
