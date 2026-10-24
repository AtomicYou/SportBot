import hashlib
import asyncio
import hmac
import json
import os
import time
import tempfile
from pathlib import Path
import unittest
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from core import now, AuthExpired, Store
from miniapp import create_app, validate_init_data, validate_wish

TOKEN = 'test-token-not-a-real-credential'


def signed(uid=123, timestamp=None, username=None):
    user = {'id': uid, 'first_name': 'Тест'}
    if username is not None:
        user['username'] = username
    data = {'auth_date': str(int(time.time()) if timestamp is None else timestamp),
            'user': json.dumps(user)}
    check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    data['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class AuthTests(unittest.TestCase):
    def test_valid_signed_user(self):
        self.assertEqual(validate_init_data(signed(), TOKEN), '123')

    def test_reject_tampering_expiry_future_duplicate_wrong_token(self):
        for value, token in [(signed().replace('123', '999'), TOKEN),
                             (signed(timestamp=int(time.time())-3700), TOKEN),
                             (signed(timestamp=int(time.time())+100), TOKEN),
                             (signed()+'&user=x', TOKEN), (signed(), 'wrong')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_init_data(value, token)

    def test_wish_validation(self):
        valid = {'sport': 'Volleyball', 'weekdays': [0, 2], 'time_from': 18, 'time_to': 22}
        self.assertEqual(validate_wish(valid)['weekdays'], [0, 2])
        for data in [[], {**valid, 'weekdays': [7]}, {**valid, 'weekdays': [True]},
                     {**valid, 'time_from': 23}, {**valid, 'sport': ''}]:
            with self.assertRaises(ValueError):
                validate_wish(data)


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {'ALLOWED_USER_IDS': ''})
        self.env.start()
        self.event = {'title': 'Volleyball', 'start': (now()+timedelta(days=1)).isoformat(),
                      'extendedProps': {'id': 42, 'checked_in': True, 'can_check_in': False},
                      'private_cookie': 'should-not-leak'}
        self.backend = SimpleNamespace(
            BOT_TOKEN=TOKEN,
            USERS={'123': {'auth': {'secret': 'hidden'}, 'wishlist': []},
                   '999': {'auth': {'secret': 'other'}, 'wishlist': [{'id': 'other'}]}},
            save_db_async=AsyncMock(),
            store=SimpleNamespace(mark_booked=Mock()),
            engine=SimpleNamespace(auth=AsyncMock(return_value={'session': 'secret'}), scan=AsyncMock(), slot_locks={}),
            portal=SimpleNamespace(schedule=AsyncMock(return_value=[self.event]), cancel=AsyncMock(return_value=200), book=AsyncMock(return_value=200)))
        self.client=TestClient(TestServer(create_app(self.backend)))
        self.backend.store.mark_cancelled = Mock(side_effect=lambda uid,tid,**kwargs:self.backend.USERS[uid].setdefault('cancelled',[]).append(tid))
        await self.client.start_server()
        self.headers={'Authorization': 'tma '+signed()}

    async def asyncTearDown(self):
        await self.client.close()
        self.env.stop()

    async def test_reject_unauthenticated_and_unknown_user(self):
        self.assertEqual((await self.client.get('/api/profile')).status, 401)
        self.assertEqual((await self.client.get('/api/profile', headers={'Authorization':'tma '+signed(456)})).status, 403)

    async def test_profile_excludes_secrets_and_schedule_is_sanitized(self):
        response = await self.client.get('/api/profile', headers=self.headers)
        self.assertNotIn('auth', await response.json())
        response = await self.client.get('/api/schedule?week=1', headers=self.headers)
        payload = await response.json()
        self.assertEqual(payload['events'][0]['id'], '42')
        self.assertNotIn('private_cookie', payload['events'][0])
        self.assertNotIn('secret', json.dumps(payload))
        self.assertEqual((await self.client.get('/api/schedule?week=2', headers=self.headers)).status,400)

    async def test_goal_and_pause_only_affect_authenticated_user(self):
        response=await self.client.post('/api/wishes',headers=self.headers,json={'sport':'Yoga','weekdays':[1],'time_from':9,'time_to':12})
        self.assertEqual(response.status,201)
        wish=await response.json()
        self.backend.engine.scan.assert_not_awaited()
        await self.client.delete('/api/wishes/'+wish['id'],headers=self.headers)
        self.assertEqual(self.backend.USERS['123']['wishlist'],[])
        self.assertEqual(self.backend.USERS['999']['wishlist'],[{'id':'other'}])
        response=await self.client.put('/api/pause',headers=self.headers,json={'paused':True})
        self.assertEqual(response.status,200)
        self.assertTrue(self.backend.USERS['123']['paused'])

    async def test_cancellation_verifies_ownership_and_blocks_sniper(self):
        response=await self.client.post('/api/bookings/666/cancel',headers=self.headers)
        self.assertEqual(response.status,404)
        self.backend.portal.cancel.assert_not_awaited()
        response=await self.client.post('/api/bookings/42/cancel',headers=self.headers)
        self.assertEqual(response.status,200)
        self.assertEqual(self.backend.USERS['123']['cancelled'],['42'])

    async def test_static_ui_and_demo_have_no_api_bypass(self):
        self.assertEqual((await self.client.get('/?demo=1')).status,200)
        self.assertEqual((await self.client.get('/api/profile?demo=1')).status,401)
        response=await self.client.post('/api/wishes',headers=self.headers,json=[])
        self.assertEqual(response.status,400)

    async def test_manual_booking_works_without_wishes_while_paused(self):
        self.event['extendedProps'].update(checked_in=False, can_check_in=True)
        self.backend.USERS['123'].update(paused=True, cancelled=['42'])
        response=await self.client.post('/api/bookings/42/book',headers=self.headers)
        self.assertEqual(response.status,200)
        self.assertFalse((await response.json())['already_booked'])
        self.backend.portal.book.assert_awaited_once_with({'session':'secret'},'42')
        self.backend.store.mark_booked.assert_called_once_with('123','42',title='Volleyball')
        self.assertEqual(self.backend.USERS['123']['cancelled'],[])
        self.assertEqual(self.backend.USERS['123']['wishlist'],[])

    async def test_manual_booking_rejects_unknown_past_and_closed(self):
        response=await self.client.post('/api/bookings/9999/book',headers=self.headers)
        self.assertEqual(response.status,404)
        self.event['start']=(now()-timedelta(hours=1)).isoformat()
        response=await self.client.post('/api/bookings/42/book',headers=self.headers)
        self.assertEqual(response.status,404)
        self.event['start']=(now()+timedelta(days=1)).isoformat()
        self.event['extendedProps'].update(checked_in=False, can_check_in=False)
        response=await self.client.post('/api/bookings/42/book',headers=self.headers)
        self.assertEqual(response.status,409)
        self.backend.portal.book.assert_not_awaited()

    async def test_already_booked_and_double_click_do_not_repeat_post(self):
        response=await self.client.post('/api/bookings/42/book',headers=self.headers)
        self.assertTrue((await response.json())['already_booked'])
        self.backend.portal.book.assert_not_awaited()
        self.event['extendedProps'].update(checked_in=False, can_check_in=True)
        responses=await asyncio.gather(*(self.client.post('/api/bookings/42/book',headers=self.headers) for _ in range(3)))
        self.assertEqual([r.status for r in responses],[200,200,200])
        self.backend.portal.book.assert_awaited_once()

    async def test_rejection_and_rate_limit_are_not_success(self):
        self.event['extendedProps'].update(checked_in=False, can_check_in=True)
        for status,expected in [(409,409),(500,409),(429,429)]:
            self.backend.portal.book.return_value=status
            response=await self.client.post('/api/bookings/42/book',headers=self.headers)
            self.assertEqual(response.status,expected)
        self.backend.store.mark_booked.assert_not_called()

    async def test_manual_booking_revalidates_after_auth_refresh(self):
        self.event['extendedProps'].update(checked_in=False, can_check_in=True)
        self.backend.portal.book.side_effect=[AuthExpired('expired'),200]
        response=await self.client.post('/api/bookings/42/book',headers=self.headers)
        self.assertEqual(response.status,200)
        self.assertEqual(self.backend.portal.schedule.await_count,2)
        self.assertIn(unittest.mock.call('123',refresh=True),self.backend.engine.auth.call_args_list)

    async def test_manual_booking_requires_signed_telegram_user(self):
        self.assertEqual((await self.client.post('/api/bookings/42/book')).status,401)
        self.backend.portal.book.assert_not_awaited()


class ModerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch('core.ROOT', Path(self.temp.name)):
            self.store = Store()
        self.store.users.update({'123': {'auth': {'csrf':'c','session':'s'}, 'wishlist': []},
            '999': {'auth': {'session':'private-session'}, 'credentials': {'username':'private@example.com','password':'encrypted-password'},
                    'wishlist':[{'id':'wish-1','sport':'Yoga','weekdays':[1], 'time_from':9,'time_to':12}]}})
        self.event={'title':'Yoga','start':(now()+timedelta(days=1)).isoformat(), 'extendedProps':{'id':42,'checked_in':True}}
        self.backend=SimpleNamespace(BOT_TOKEN=TOKEN, USERS=self.store.users, store=self.store,
            save_db_async=self.store.save,
            engine=SimpleNamespace(auth=AsyncMock(return_value={'session':'private-session'}),scan=AsyncMock(),slot_locks={}),
            portal=SimpleNamespace(schedule=AsyncMock(return_value=[self.event]),cancel=AsyncMock(return_value=200),book=AsyncMock(return_value=200)))
        self.env=patch.dict(os.environ,{'ALLOWED_USER_IDS':''});self.env.start()
        (self.store.directory/'moderator-identity.json').write_text(json.dumps({'telegram_id':'123'}),encoding='utf-8')
        from moderator_auth import ModeratorAuth
        auth=ModeratorAuth(self.store)
        token=auth.exchange(auth.issue('123'))
        self.client=TestClient(TestServer(create_app(self.backend)));await self.client.start_server()
        self.owner={'Authorization':'Bearer '+token}
        self.member={'Authorization':'tma '+signed(uid=999,username='Other')}

    async def asyncTearDown(self):
        await self.client.close();self.store.db.close();self.env.stop();self.temp.cleanup()

    async def overview(self):
        response=await self.client.get('/api/admin/overview',headers=self.owner)
        self.assertEqual(response.status,200)
        return await response.json()

    async def test_native_login_logout_and_removed_web_page(self):
        from moderator_auth import ModeratorAuth
        auth = ModeratorAuth(self.store)
        code = auth.issue('123')
        response = await self.client.post('/api/moderator/login', json={'code': code})
        self.assertEqual(response.status, 200)
        headers = {'Authorization': 'Bearer '+(await response.json())['token']}
        self.assertEqual((await self.client.get('/api/admin/overview', headers=headers)).status, 200)
        self.assertEqual((await self.client.post('/api/moderator/login', json={'code': code})).status, 401)
        self.assertEqual((await self.client.post('/api/admin/logout', headers=headers)).status, 200)
        self.assertEqual((await self.client.get('/api/admin/overview', headers=headers)).status, 401)
        for path in ('/moderation', '/moderation.html', '/moderation.js'):
            self.assertEqual((await self.client.get(path)).status, 404)

    async def test_only_owner_and_pinned_id_even_after_username_change(self):
        self.assertEqual((await self.client.get('/api/admin/overview')).status,401)
        self.assertEqual((await self.client.get('/api/admin/overview',headers=self.member)).status,403)
        await self.overview()
        stolen={'Authorization':'tma '+signed(uid=999,username='KarAlexandr')}
        renamed={'Authorization':'tma '+signed(username='RenamedOwner')}
        self.assertEqual((await self.client.get('/api/admin/overview',headers=stolen)).status,403)
        self.assertEqual((await self.client.get('/api/admin/overview',headers=renamed)).status,403)
        self.assertTrue((self.store.directory/'moderator-identity.json').exists())
        # Restarting the HTTP app retains the original numeric account pin.
        from moderation import Moderator
        self.assertFalse(Moderator(self.backend).is_admin('999',signed(uid=999,username='KarAlexandr')))

    async def test_moderator_outputs_only_whitelisted_anonymous_data(self):
        data=await self.overview()
        self.assertEqual(set(data['users'][0]),{'code','paused','wish_count'})
        for user in data['users']:
            response=await self.client.get('/api/admin/users/'+user['code'],headers=self.owner)
            payload=await response.json()
            self.assertEqual(set(payload),{'code','paused','wishlist','bookings','portal_available'})
            for secret in ('private@example.com','encrypted-password','private-session','credentials','telegram_id'):
                self.assertNotIn(secret,json.dumps(payload))
        profile=await (await self.client.get('/api/profile',headers=self.member)).json()
        self.assertFalse(profile['moderator']);self.assertTrue(profile['support_code'].startswith('U-'))

    async def test_delete_and_cancel_require_user_request_and_are_audited(self):
        await self.overview()
        member=await (await self.client.get('/api/profile',headers=self.member)).json();code=member['support_code']
        path=f'/api/admin/users/{code}/wishes/wish-1'
        self.assertEqual((await self.client.delete(path,headers=self.owner,json={})).status,400)
        self.assertEqual((await self.client.delete(path,headers=self.member,json={'requested_by_user':True})).status,403)
        self.assertEqual((await self.client.delete(path,headers=self.owner,json={'requested_by_user':True})).status,200)
        path=f'/api/admin/users/{code}/bookings/42/cancel'
        self.assertEqual((await self.client.post(path,headers=self.owner,json={'requested_by_user':True})).status,200)
        self.assertEqual(self.store.users['999']['cancelled'],['42'])
        self.assertEqual(self.store.users['999']['wishlist'],[])
        self.assertEqual(len((await self.overview())['audit']),2)

    async def test_feature_switches_persist_and_cannot_be_bypassed_via_api(self):
        flags=(await self.overview())['features'];flags.update(manual_booking=False,new_wishes=False)
        self.assertEqual((await self.client.put('/api/admin/features',headers=self.owner,json=flags)).status,200)
        self.assertEqual((await self.client.post('/api/bookings/42/book',headers=self.member)).status,403)
        wish={'sport':'Yoga','weekdays':[1],'time_from':9,'time_to':12}
        self.assertEqual((await self.client.post('/api/wishes',headers=self.member,json=wish)).status,403)
        self.backend.portal.book.assert_not_awaited()
        from controls import FeaturePolicy
        self.assertFalse(FeaturePolicy(self.store.db).flags['manual_booking'])
        self.assertEqual((await self.client.put('/api/admin/features',headers=self.member,json=flags)).status,403)


if __name__ == '__main__':
    unittest.main()
