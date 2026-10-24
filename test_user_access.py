import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import test_miniapp
from core import Store, Engine, now
from datetime import timedelta


class AudienceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_miniapp.ModerationTests.asyncSetUp
    asyncTearDown = test_miniapp.ModerationTests.asyncTearDown

    async def test_verified_users_and_multiple_installations_are_visible(self):
        first = {**self.member, 'X-Miniapp-Device': 'a'*32, 'X-Telegram-Platform': 'android'}
        second = {**self.member, 'X-Miniapp-Device': 'b'*32, 'X-Telegram-Platform': 'tdesktop'}
        self.assertEqual((await self.client.get('/api/profile', headers=first)).status, 200)
        self.assertEqual((await self.client.get('/api/profile', headers=second)).status, 200)
        self.assertEqual((await self.client.get('/api/admin/audience', headers=self.member)).status, 403)
        data = await (await self.client.get('/api/admin/audience', headers=self.owner)).json()
        devices = [d for d in data['devices'] if d['uid'] == '999']
        self.assertEqual({d['platform'] for d in devices}, {'android', 'tdesktop'})
        self.assertEqual(next(u for u in data['users'] if u['uid'] == '999')['username'], 'Other')
        self.assertEqual(len(devices), 2)
        self.assertNotIn('private@example.com', str(data))
        self.assertEqual((await self.client.get('/api/profile', headers={'Authorization': 'tma forged', 'X-Miniapp-Device': 'c'*32})).status, 401)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM miniapp_devices').fetchone()[0], 2)

    async def test_account_ban_persists_and_blocks_all_installations_and_bookings(self):
        path = '/api/admin/audience/users/999'
        self.assertEqual((await self.client.put(path, headers=self.member, json={'blocked': True})).status, 403)
        self.assertEqual((await self.client.put(path, headers=self.owner, json={'blocked': True})).status, 200)
        for client in ('a'*32, 'b'*32, ''):
            headers = {**self.member, 'X-Miniapp-Device': client}
            self.assertEqual((await self.client.get('/api/profile', headers=headers)).status, 403)
            self.assertEqual((await self.client.post('/api/bookings/42/book', headers=headers)).status, 403)
        self.backend.portal.book.assert_not_awaited()
        with patch('core.ROOT', self.store.directory.parent):
            reopened = Store()
        try:
            self.assertTrue(reopened.access.blocked('999'))
        finally:
            reopened.db.close()
        engine = Engine(self.backend, self.store, self.backend.portal)
        self.store.users['999']['wishlist'] = [{'id': 'x', 'sport': 'Yoga', 'weekdays': list(range(7)), 'time_from': 0, 'time_to': 24}]
        self.assertFalse(engine.active('999', 42, 'Yoga', now()+timedelta(days=1)))
        self.backend.bot = SimpleNamespace(send_message=AsyncMock())
        await engine.features.remind('999', self.event)
        self.backend.bot.send_message.assert_not_awaited()
        self.assertEqual((await self.client.put(path, headers=self.owner, json={'blocked': False})).status, 200)
        self.assertEqual((await self.client.get('/api/profile', headers=self.member)).status, 200)

    async def test_device_ban_only_affects_selected_installation(self):
        first = {**self.member, 'X-Miniapp-Device': 'a'*32}
        second = {**self.member, 'X-Miniapp-Device': 'b'*32}
        await self.client.get('/api/profile', headers=first)
        await self.client.get('/api/profile', headers=second)
        data = await (await self.client.get('/api/admin/audience', headers=self.owner)).json()
        target = next(d['id'] for d in data['devices'] if d['uid'] == '999' and d['id'] == __import__('hashlib').sha256(('999|'+'a'*32).encode()).hexdigest())
        path = '/api/admin/audience/devices/'+target
        self.assertEqual((await self.client.put(path, headers=self.owner, json={'blocked': True})).status, 200)
        self.assertEqual((await self.client.get('/api/profile', headers=first)).status, 403)
        self.assertEqual((await self.client.get('/api/profile', headers=second)).status, 200)
        self.assertEqual((await self.client.put(path, headers=self.owner, json={'blocked': False})).status, 200)
        self.assertEqual((await self.client.get('/api/profile', headers=first)).status, 200)

    async def test_owner_protection_and_invalid_targets(self):
        path = '/api/admin/audience/users/'
        self.assertEqual((await self.client.put(path+'123', headers=self.owner, json={'blocked': True})).status, 400)
        self.assertEqual((await self.client.put(path+'999', headers=self.owner, json={'blocked': 'true'})).status, 400)
        self.assertEqual((await self.client.put(path+'888', headers=self.owner, json={'blocked': True})).status, 404)
        self.assertEqual((await self.client.put('/api/admin/audience/devices/unknown', headers=self.owner, json={'blocked': True})).status, 404)

    async def test_blocked_telegram_updates_do_not_reach_bot_handlers(self):
        import bot
        self.store.access.touch('999', 'Other')
        self.store.db.execute("UPDATE bot_access SET blocked=1 WHERE uid='999'")
        self.store.db.commit()
        handler = AsyncMock()
        data = {'event_from_user': SimpleNamespace(id=999, username='Other'), 'event_chat': SimpleNamespace(type='private')}
        with patch.object(bot, 'store', self.store), patch.object(bot, 'menu_sync', None):
            await bot.PrivateAccess()(handler, SimpleNamespace(), data)
        handler.assert_not_awaited()
