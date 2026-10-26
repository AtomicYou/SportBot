import asyncio
import json
import logging
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
import core
from core import Store, Engine, now
from miniapp import create_app
from owner_alerts import OwnerAlerts
from test_miniapp import TOKEN, signed


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = patch.object(core, 'ROOT', Path(self.temp.name))
        self.root.start()
        self.store = Store()
        self.store.users['123'] = {'auth': {'csrf': 'c', 'session': 's'}, 'wishlist': [], 'cancelled': []}
        self.start = now() + timedelta(days=1)
        self.event = {'title': 'Volleyball', 'start': self.start.isoformat(),
                      'end': (self.start+timedelta(hours=1)).isoformat(),
                      'extendedProps': {'id': 42, 'checked_in': False, 'can_check_in': True}}
        self.portal = SimpleNamespace(schedule=AsyncMock(return_value=[self.event]),
            book=AsyncMock(return_value=200), cancel=AsyncMock(return_value=200), close=AsyncMock())
        self.app = SimpleNamespace(store=self.store, USERS=self.store.users, BOT_TOKEN=TOKEN,
            portal=self.portal, save_db_async=self.store.save,
            get_decrypted_auth=lambda user: user['auth'], bot=SimpleNamespace(send_message=AsyncMock()))
        self.engine = self.app.engine = Engine(self.app, self.store, self.portal)
        self.client = TestClient(TestServer(create_app(self.app)))
        await self.client.start_server()
        self.headers = {'Authorization': 'tma '+signed()}

    async def asyncTearDown(self):
        await self.client.close()
        await self.engine.close()
        self.store.db.close()
        self.root.stop()
        self.temp.cleanup()

    async def test_waiting_specific_event_books_without_recurring_goal(self):
        self.event['extendedProps']['can_check_in'] = False
        response = await self.client.post('/api/waitlist/42', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertTrue(self.store.is_waiting('123', '42'))
        await self.store.save()
        reopened = Store()
        self.assertEqual(reopened.waiting('123')[0]['title'], 'Volleyball')
        reopened.db.close()
        self.event['extendedProps']['can_check_in'] = True
        await self.engine.scan()
        self.portal.book.assert_awaited_once()
        self.assertFalse(self.store.is_waiting('123', '42'))
        self.assertTrue(self.store.booked('123', '42'))

    async def test_waiting_delete_ownership_pause_and_reminders_setting(self):
        await self.client.post('/api/waitlist/42', headers=self.headers)
        self.assertEqual((await self.client.delete('/api/waitlist/42', headers={'Authorization':'tma '+signed(999)})).status, 403)
        self.store.users['123']['paused'] = True
        await self.engine.scan()
        self.portal.book.assert_not_awaited()
        response = await self.client.put('/api/reminders', headers=self.headers, json={'enabled': False})
        self.assertEqual(response.status, 200)
        self.assertFalse(self.store.users['123']['reminders'])
        self.assertEqual((await self.client.put('/api/reminders', headers=self.headers, json={'enabled': 'false'})).status, 400)
        await self.client.delete('/api/waitlist/42', headers=self.headers)
        self.assertEqual(self.store.waiting('123'), [])

    async def test_existing_portal_booking_does_not_block_manual_overlap(self):
        other = {**self.event, 'extendedProps': {'id': 88, 'checked_in': True}}
        self.portal.schedule.return_value = [self.event, other]
        response = await self.client.post('/api/bookings/42/book', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.portal.book.assert_awaited_once()

    async def test_two_different_simultaneous_overlapping_bookings_are_allowed(self):
        second = {**self.event, 'extendedProps': {'id': 43, 'can_check_in': True}}
        self.portal.schedule.return_value = [self.event, second]
        results = await asyncio.gather(*(self.client.post('/api/bookings/'+tid+'/book', headers=self.headers) for tid in ('42','43')))
        self.assertEqual(sorted(response.status for response in results), [200, 200])
        self.assertEqual(self.portal.book.await_count, 2)

    async def test_automatic_and_manual_overlapping_bookings_are_allowed(self):
        second = {**self.event, 'extendedProps': {'id': 43, 'can_check_in': True}}
        self.portal.schedule.return_value = [self.event, second]
        self.store.add_waiting('123', self.event)
        await asyncio.gather(self.engine.attempt('123',42,'Volleyball',self.start),
                             self.client.post('/api/bookings/43/book', headers=self.headers))
        self.assertEqual(self.portal.book.await_count, 2)

    async def test_stored_intervals_do_not_block_booking(self):
        self.store.remember_interval('123',100,self.start,self.start+timedelta(hours=1))
        response = await self.client.post('/api/bookings/42/book',headers=self.headers)
        self.assertEqual(response.status,200)
        self.portal.book.assert_awaited_once()

    async def test_reminder_persistence_cancellation_opt_out_and_late_window(self):
        self.event['start'] = (now()+timedelta(hours=1)).isoformat()
        self.event['extendedProps']['checked_in'] = True
        await self.engine.features.remind('123', self.event)
        self.app.bot.send_message.assert_awaited_once()
        other = Store()
        try:
            other.users = self.store.users
            old = self.engine.features.store
            self.engine.features.store = other
            await self.engine.features.remind('123', self.event)
            self.engine.features.store = old
        finally:
            other.db.close()
        self.app.bot.send_message.assert_awaited_once()
        for tid, reminder_on, cancelled, hours in [(50,False,[],1), (51,True,['51'],1), (52,True,[],2)]:
            self.event['extendedProps']['id'] = tid
            self.event['start'] = (now()+timedelta(hours=hours)).isoformat()
            self.store.users['123'].update(reminders=reminder_on, cancelled=cancelled)
            await self.engine.features.remind('123', self.event)
        self.app.bot.send_message.assert_awaited_once()

    async def test_waiting_again_keeps_cancel_history_until_success(self):
        self.store.mark_cancelled('123',42,title='Volleyball')
        self.store.add_waiting('123',self.event)
        self.assertTrue(self.engine.active('123',42,'Volleyball',self.start))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM cancelled_bookings').fetchone()[0],1)
        await self.engine.attempt('123',42,'Volleyball',self.start)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM cancelled_bookings').fetchone()[0],0)

    async def test_owner_errors_are_private_and_rate_limited_across_restart(self):
        identity = self.store.directory/'moderator-identity.json'
        identity.write_text(json.dumps({'telegram_id':777}),encoding='utf-8')
        alerts = OwnerAlerts(self.app.bot,self.store)
        try:
            record = logging.LogRecord('core',logging.ERROR,'',1,'password=supersecret email=other@university',(),None)
            alerts.emit(record)
            alerts.emit(record)
            await asyncio.sleep(0)
            await alerts.queue.join()
            self.app.bot.send_message.assert_awaited_once()
            args = self.app.bot.send_message.call_args.args
            self.assertEqual(args[0],777)
            self.assertNotIn('supersecret',args[1])
            self.assertNotIn('other@',args[1])
        finally:
            await alerts.shutdown()
        alerts = OwnerAlerts(self.app.bot,self.store)
        try:
            alerts.enqueue('portal')
            self.assertTrue(alerts.queue.empty())
        finally:
            await alerts.shutdown()

    async def test_calendar_reports_failure_and_success_to_same_health_component(self):
        observer = Mock()
        self.app.owner_alerts = SimpleNamespace(observe=observer)
        self.portal.schedule.side_effect = core.PortalError('temporary')
        with self.assertRaises(core.PortalError) as failure:
            await self.engine.features.calendar('123')
        self.assertTrue(failure.exception.owner_alert_reported)
        observer.assert_called_once_with('portal','123',False)
        self.portal.schedule.side_effect = None
        await self.engine.features.calendar('123')
        self.assertEqual(observer.call_args.args,('portal','123',True))

    async def test_unknown_owner_never_falls_back_to_another_user(self):
        alerts = OwnerAlerts(self.app.bot,self.store)
        try:
            alerts.enqueue('app')
            self.assertTrue(alerts.queue.empty())
            self.app.bot.send_message.assert_not_awaited()
        finally:
            await alerts.shutdown()

    async def test_reminder_button_uses_clicking_person_not_message_author(self):
        import bot as module
        from aiogram import Bot
        client = Bot('123456:ABCdefGHIjklMNOpqrSTUvwxYZ012345678')
        callback = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=123),
                                   message=SimpleNamespace(chat=SimpleNamespace(id=999)))
        try:
            with patch.object(module, 'bot', client), patch.object(module, 'cmd_my_trainings', new_callable=AsyncMock) as show:
                await module.show_bookings(callback)
                self.assertEqual(show.call_args.args[0].chat.id, 123)
        finally:
            await client.session.close()

    async def test_both_reminder_thresholds_and_retry_after_failure(self):
        self.event['extendedProps']['checked_in'] = True
        self.event['start'] = (now()+timedelta(days=1)).isoformat()
        self.app.bot.send_message.side_effect = RuntimeError('temporary network failure')
        await self.engine.features.remind('123',self.event)
        await self.engine.features.remind('123',self.event)
        self.app.bot.send_message.assert_awaited_once()
        self.app.bot.send_message.side_effect = None
        clock = now()+timedelta(minutes=5,seconds=1)
        with patch.object(core,'now',return_value=clock):
            await self.engine.features.remind('123',self.event)
        self.assertEqual(self.app.bot.send_message.await_count,2)
        self.event['start'] = (now()+timedelta(hours=1)).isoformat()
        await self.engine.features.remind('123',self.event)
        self.assertEqual(self.app.bot.send_message.await_count,3)
