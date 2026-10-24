import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import core


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(core, 'ROOT', Path(self.temp.name))
        self.patch.start()
        self.store = core.Store()
        self.dt = core.now() + timedelta(days=8)
        self.wish = {'id': 'stable', 'sport': 'Volleyball', 'weekdays': list(range(7)), 'time_from': 0, 'time_to': 24}
        self.store.users['1'] = {'auth': {'csrf': 'c', 'session': 's'}, 'wishlist': [self.wish], 'cancelled': []}
        self.portal = SimpleNamespace(book=AsyncMock(return_value=200), schedule=AsyncMock(return_value=[]), close=AsyncMock())
        self.app = SimpleNamespace(get_decrypted_auth=lambda u: u['auth'], bot=SimpleNamespace(send_message=AsyncMock()))
        self.engine = core.Engine(self.app, self.store, self.portal)

    async def asyncTearDown(self):
        await self.engine.close()
        self.store.db.close()
        self.patch.stop()
        self.temp.cleanup()

    async def test_concurrent_booking_only_once(self):
        await asyncio.gather(*(self.engine.attempt('1', 99, 'Volleyball', self.dt) for _ in range(5)))
        self.assertEqual(self.portal.book.await_count, 1)
        self.assertTrue(self.store.booked('1', 99))

    async def test_names_and_username_survive_cancel_and_rebooking(self):
        self.store.remember_telegram_user('1','sports_user')
        self.store.mark_booked('1',99,title='Volleyball')
        self.assertEqual(self.store.db.execute('SELECT training_title,telegram_username FROM approved_bookings').fetchone(),('Volleyball','@sports_user'))
        self.store.mark_cancelled('1',99)
        self.assertEqual(self.store.db.execute('SELECT training_title,telegram_username FROM cancelled_bookings').fetchone(),('Volleyball','@sports_user'))
        self.store.remember_telegram_user('1','renamed_user')
        self.store.mark_booked('1',99)
        self.assertEqual(self.store.db.execute('SELECT training_title,telegram_username FROM approved_bookings').fetchone(),('Volleyball','@renamed_user'))

    async def test_fill_historical_titles_does_not_change_booking_state(self):
        self.store.mark_cancelled('1',99)
        self.store.update_booking_titles('1',[{'title':'Yoga','extendedProps':{'id':99}}])
        self.assertEqual(self.store.db.execute('SELECT training_title FROM cancelled_bookings').fetchone()[0],'Yoga')
        self.assertFalse(self.store.booked('1',99))

    async def test_cancel_and_rebook_move_between_tables_and_survive_restart(self):
        await self.store.save()
        self.store.mark_booked('1',99)
        self.store.mark_cancelled('1',99)
        self.assertFalse(self.store.booked('1',99))
        self.assertEqual(self.store.db.execute('SELECT user,training FROM cancelled_bookings').fetchall(),[('1','99')])
        other=core.Store()
        try:
            self.assertEqual(other.users['1']['cancelled'],['99'])
            other.mark_booked('1',99)
            self.assertTrue(other.booked('1',99))
            self.assertEqual(other.db.execute('SELECT count(*) FROM cancelled_bookings').fetchone()[0],0)
            self.assertEqual(other.users['1']['cancelled'],[])
        finally:
            other.db.close()

    async def test_legacy_migration_keeps_approvals_and_moves_cancellations(self):
        import json
        self.store.db.execute('DROP TABLE approved_bookings')
        self.store.db.execute('CREATE TABLE bookings (user TEXT,training TEXT,PRIMARY KEY(user,training))')
        self.store.db.executemany('INSERT INTO bookings VALUES (?,?)',[('1','99'),('1','100')])
        legacy={**self.store.users['1'],'cancelled':['99','101']}
        self.store.db.execute('INSERT INTO users VALUES (?,?)',('1',json.dumps(legacy)))
        self.store.db.commit()
        other=core.Store()
        try:
            self.assertFalse(other.booked('1','99'))
            self.assertTrue(other.booked('1','100'))
            self.assertEqual(set(other.users['1']['cancelled']),{'99','101'})
            self.assertIsNone(other.db.execute("SELECT name FROM sqlite_master WHERE name='bookings'").fetchone())
            self.assertNotIn('cancelled',json.loads(other.db.execute('SELECT data FROM users').fetchone()[0]))
            again=core.Store()
            try:
                self.assertEqual(again.db.execute('SELECT count(*) FROM cancelled_bookings').fetchone()[0],2)
            finally:
                again.db.close()
        finally:
            other.db.close()

    async def test_moderator_disable_blocks_scanner_and_sniper(self):
        self.app.store=self.store
        self.store.features.update({**self.store.features.flags,'auto_booking':False})
        await self.engine.scan('1')
        await self.engine.attempt('1',99,'Volleyball',self.dt)
        self.portal.schedule.assert_not_awaited()
        self.portal.book.assert_not_awaited()

    async def test_moderator_disable_during_auth_prevents_post(self):
        self.app.store=self.store
        async def disable(*args,**kwargs):
            self.store.features.update({**self.store.features.flags,'auto_booking':False})
            return {'session':'s','csrf':'c'}
        self.engine.auth=AsyncMock(side_effect=disable)
        await self.engine.attempt('1',99,'Volleyball',self.dt)
        self.portal.book.assert_not_awaited()

    async def test_cancel_pause_and_deleted_goal_block_booking(self):
        user = self.store.users['1']
        user['cancelled'] = ['99']
        await self.engine.attempt('1', 99, 'Volleyball', self.dt)
        user['cancelled'] = []
        user['paused'] = True
        await self.engine.attempt('1', 99, 'Volleyball', self.dt)
        user['paused'] = False
        user['wishlist'] = []
        await self.engine.attempt('1', 99, 'Volleyball', self.dt)
        self.portal.book.assert_not_awaited()

    async def test_empty_weeks_are_not_expired_sessions(self):
        self.app.auto_login = AsyncMock()
        await self.engine.scan('1')
        self.assertEqual(self.portal.schedule.await_count, 2)
        self.app.auto_login.assert_not_awaited()
        calls = self.portal.schedule.call_args_list
        self.assertEqual(calls[0].args[2], calls[1].args[1])

    async def test_only_requested_account_scanned(self):
        self.store.users['2'] = dict(self.store.users['1'])
        await self.engine.scan('1')
        self.assertEqual(self.portal.schedule.await_count, 2)

    async def test_sniper_starts_one_minute_before_seven_days(self):
        opening = self.dt - timedelta(days=7)
        clock = [opening - timedelta(minutes=2)]
        attempts = []

        async def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)

        async def book(auth, tid):
            attempts.append(clock[0])
            return 409 if len(attempts) == 1 else 200

        self.portal.book.side_effect = book
        with patch.object(core, 'now', side_effect=lambda: clock[0]), patch.object(core.asyncio, 'sleep', side_effect=sleep):
            await self.engine.snipe('1', 99, 'Volleyball', self.dt)
        self.assertEqual(attempts, [opening - timedelta(minutes=1), opening - timedelta(seconds=59)])

    async def test_restart_scan_rebuilds_future_task(self):
        event = {'title': 'Volleyball', 'start': self.dt.isoformat(),
                 'extendedProps': {'id': 99, 'can_check_in': False}}
        self.portal.schedule.return_value = [event]
        await self.engine.scan('1')
        self.assertEqual(len(self.engine.tasks), 1)
        self.portal.book.assert_not_awaited()

    async def test_persistence_and_encryption(self):
        encrypted = self.store.cipher.encrypt(b'password').decode()
        self.store.users['1']['credentials'] = {'password': encrypted}
        await self.store.save()
        self.store.mark_booked('1', 99)
        other = core.Store()
        try:
            self.assertTrue(other.booked('1', 99))
            self.assertEqual(other.cipher.decrypt(other.users['1']['credentials']['password'].encode()), b'password')
        finally:
            other.db.close()

    async def test_time_window_and_timezone(self):
        self.wish.update(time_from=18, time_to=22)
        self.assertTrue(core.matches(self.wish, 'Volleyball', self.dt.replace(hour=21, minute=59)))
        self.assertFalse(core.matches(self.wish, 'Volleyball', self.dt.replace(hour=22, minute=0)))
        self.assertEqual(core.parse_date('2026-10-24T07:30:00+00:00').hour, 10)
        self.wish['sport'] = 'Tennis'
        self.assertFalse(core.matches(self.wish, 'Table tennis - Beginners', self.dt.replace(hour=20)))


if __name__ == '__main__':
    unittest.main()
