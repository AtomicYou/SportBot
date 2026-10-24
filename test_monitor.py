import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from aiohttp.test_utils import TestClient, TestServer
from monitor import Monitor, create_app, safe_command
from request_journal import RequestJournal


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def test_sensitive_text_and_arguments_do_not_reach_panel(self):
        self.assertEqual(safe_command('command', '/login secret@example.com password'), '/login')
        self.assertEqual(safe_command('text_input', 'private-password'), 'Текстовое сообщение')
        self.assertEqual(safe_command('auth_password_input', 'private-password'), 'Ввод пароля · скрыт')
        self.assertEqual(safe_command('inline_button', 'sensitive-token'), 'Нажатие inline-кнопки')

    async def test_loopback_host_and_origin_guard_and_static_allowlist(self):
        client=TestClient(TestServer(create_app()))
        await client.start_server()
        try:
            self.assertEqual((await client.get('/api/stats',headers={'Host':'evil.example'})).status,403)
            headers={'Host':'127.0.0.1:8085'}
            self.assertEqual((await client.get('/api/stats',headers={**headers,'Origin':'https://evil.example'})).status,403)
            response=await client.get('/api/stats',headers=headers)
            self.assertEqual(response.status,200)
            self.assertNotIn('Access-Control-Allow-Origin',response.headers)
            self.assertEqual((await client.get('/.env',headers=headers)).status,404)
        finally:
            await client.close()

    def test_journal_stats_and_safe_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            journal=RequestJournal(root/'data')
            try:
                user=SimpleNamespace(id=123,username='tester')
                journal.record(1,user,'message','command','/start private-secret',{})
                journal.finish(1,'processed')
                journal.record(2,user,'message','auth_password_input','private-password',{})
                snapshot=Monitor(root).collect()
                self.assertEqual(snapshot['counts']['minute'],2)
                self.assertNotIn('private-secret',str(snapshot))
                self.assertNotIn('private-password',str(snapshot))
                self.assertFalse(snapshot['log_ok'])
            finally:
                journal.close()
