import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from core import Store
from moderator_auth import ModeratorAuth


class NativeAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch('core.ROOT', Path(self.temp.name)):
            self.store = Store()
        self.identity = self.store.directory / 'moderator-identity.json'
        self.identity.write_text(json.dumps({'telegram_id': '123'}), encoding='utf-8')
        self.auth = ModeratorAuth(self.store)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_pairing_single_use_hashed_and_owner_only(self):
        with self.assertRaises(PermissionError):
            self.auth.issue('999')
        code = self.auth.issue('123')
        token = self.auth.exchange(code)
        self.assertEqual(self.auth.validate('Bearer '+token), '123')
        with self.assertRaises(web.HTTPUnauthorized):
            self.auth.exchange(code)
        self.assertNotEqual(self.store.db.execute('SELECT hash FROM moderator_sessions').fetchone()[0], token)
        with self.assertRaises(web.HTTPForbidden):
            self.auth.validate('tma anything')

    def test_expiry_revocation_and_missing_owner_fail_closed(self):
        with patch('moderator_auth.time.time', return_value=1000):
            code = self.auth.issue('123')
        with patch('moderator_auth.time.time', return_value=1301):
            with self.assertRaises(web.HTTPUnauthorized):
                self.auth.exchange(code)
        token = self.auth.exchange(self.auth.issue('123'))
        self.auth.revoke('Bearer '+token)
        with self.assertRaises(web.HTTPUnauthorized):
            self.auth.validate('Bearer '+token)
        token = self.auth.exchange(self.auth.issue('123'))
        self.store.db.execute('UPDATE moderator_sessions SET expires=0')
        self.store.db.commit()
        with self.assertRaises(web.HTTPUnauthorized):
            self.auth.validate('Bearer '+token)
        self.identity.unlink()
        with self.assertRaises(PermissionError):
            self.auth.issue('123')

    def test_global_rate_limit_and_revoke_pending_codes(self):
        for _ in range(30):
            with self.assertRaises(web.HTTPUnauthorized):
                self.auth.exchange('wrong-code-with-enough-length')
        with self.assertRaises(web.HTTPTooManyRequests):
            self.auth.exchange('wrong-code-with-enough-length')
        self.auth = ModeratorAuth(self.store)
        code = self.auth.issue('123')
        self.auth.revoke()
        with self.assertRaises(web.HTTPUnauthorized):
            self.auth.exchange(code)

    def test_upgrade_preserves_valid_old_session_and_assigns_safe_id(self):
        import time
        with self.store.db:
            self.store.db.execute('DROP TABLE moderator_sessions')
            self.store.db.execute('CREATE TABLE moderator_sessions(hash TEXT PRIMARY KEY, expires REAL, telegram_id TEXT)')
            self.store.db.execute('INSERT INTO moderator_sessions VALUES (?,?,?)',
                                 (self.auth.digest('existing-token'), time.time()+1800, '123'))
        upgraded = ModeratorAuth(self.store)
        self.assertEqual(upgraded.validate('Bearer existing-token'), '123')
        session_id, device = self.store.db.execute('SELECT id,device FROM moderator_sessions').fetchone()
        self.assertEqual(len(session_id), 32)
        self.assertEqual(device, 'Старая версия приложения')
