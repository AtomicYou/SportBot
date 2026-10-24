from contextlib import closing
import hashlib
import json
import sqlite3
import unittest
from types import SimpleNamespace

from request_journal import RequestJournal
from server_operations import ServerOperations
from moderator_auth import ModeratorAuth
import test_miniapp


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_miniapp.ModerationTests.asyncSetUp
    asyncTearDown = test_miniapp.ModerationTests.asyncTearDown

    async def test_all_new_endpoints_reject_non_owner(self):
        for path in ('/api/admin/system', '/api/admin/devices', '/api/admin/backups'):
            self.assertEqual((await self.client.get(path)).status, 401)
            self.assertEqual((await self.client.get(path, headers=self.member)).status, 403)
            self.assertEqual((await self.client.get(path, headers=self.owner)).status, 200)
        self.assertEqual((await self.client.post('/api/admin/backups', headers=self.member)).status, 403)
        self.assertEqual((await self.client.post('/api/admin/devices/revoke-others', headers=self.member)).status, 403)
        self.assertEqual((await self.client.delete('/api/admin/devices/anything', headers=self.member)).status, 403)

    async def test_device_listing_activity_and_individual_revocation(self):
        auth = ModeratorAuth(self.store)
        code = auth.issue('123')
        login = await self.client.post('/api/moderator/login', json={'code': code, 'device': 'My second phone'})
        second_token = (await login.json())['token']
        second = {'Authorization': 'Bearer '+second_token}
        data = await (await self.client.get('/api/admin/devices', headers=self.owner)).json()
        self.assertEqual(len(data['devices']), 2)
        self.assertEqual(sum(d['current'] for d in data['devices']), 1)
        self.assertNotIn(second_token, json.dumps(data))
        self.assertNotIn('hash', json.dumps(data))
        target = next(d for d in data['devices'] if d['device'] == 'My second phone')
        self.assertIsNotNone(target['last_seen'])
        self.assertEqual((await self.client.delete('/api/admin/devices/'+target['id'], headers=self.owner)).status, 200)
        self.assertEqual((await self.client.get('/api/admin/overview', headers=second)).status, 401)
        self.assertEqual((await self.client.get('/api/admin/overview', headers=self.owner)).status, 200)

    async def test_revoke_others_keeps_current_but_revokes_pending_pairing(self):
        auth = ModeratorAuth(self.store)
        second = auth.exchange(auth.issue('123'), 'Second phone')
        pending = auth.issue('123')
        self.assertEqual((await self.client.post('/api/admin/devices/revoke-others', headers=self.owner)).status, 200)
        self.assertEqual((await self.client.get('/api/admin/devices', headers={'Authorization': 'Bearer '+second})).status, 401)
        self.assertEqual((await self.client.post('/api/moderator/login', json={'code': pending})).status, 401)
        self.assertEqual((await self.client.get('/api/admin/devices', headers=self.owner)).status, 200)

    async def test_live_wal_backups_are_readable_match_data_and_include_key(self):
        journal = RequestJournal(self.store.directory)
        try:
            journal.record(100, SimpleNamespace(id=123, username='owner'), 'message', 'command', '/start', {})
            await self.store.save()
            response = await self.client.post('/api/admin/backups', headers=self.owner)
            self.assertEqual(response.status, 200)
            name = (await response.json())['id']
            path = self.store.directory/'backups'/'automatic'/name
            manifest = json.loads((path/'manifest.json').read_text())
            for entry in manifest['files']:
                with (path/entry['name']).open('rb') as stream:
                    self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), entry['sha256'])
            with closing(sqlite3.connect(path/'bot.sqlite3')) as copy:
                self.assertEqual(copy.execute('SELECT id,data FROM users ORDER BY id').fetchall(),
                                 self.store.db.execute('SELECT id,data FROM users ORDER BY id').fetchall())
                self.assertEqual(copy.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            with closing(sqlite3.connect(path/'journal.sqlite3')) as copy:
                self.assertEqual(copy.execute('SELECT * FROM events').fetchall(), journal.db.execute('SELECT * FROM events').fetchall())
            self.assertEqual((path/'secret.key').read_bytes(), (self.store.directory/'secret.key').read_bytes())
        finally:
            journal.close()

    async def test_daily_dedup_retention_and_failed_copy_not_published(self):
        journal = RequestJournal(self.store.directory)
        ops = ServerOperations(self.backend)
        try:
            await ops.daily()
            await ops.daily()
            self.assertEqual(len(ops.catalogue()), 1)
            for _ in range(11):
                await ops.backup()
            self.assertEqual(len([b for b in ops.catalogue() if b['kind'] == 'manual']), 10)
            self.assertEqual(len([b for b in ops.catalogue() if b['kind'] == 'daily']), 1)
        finally:
            journal.close()
        (self.store.directory/'journal.sqlite3').unlink()
        before = ops.catalogue()
        with self.assertRaises(sqlite3.OperationalError):
            await ops.backup()
        self.assertEqual(before, ops.catalogue())
        self.assertIsNotNone(ops.last_backup_error)
        self.assertFalse(any(p.name.startswith('.pending-') for p in ops.root.iterdir()))
