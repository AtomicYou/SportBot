import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiogram import types
from request_journal import RequestJournal, JournalMiddleware


class JournalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.journal=RequestJournal(Path(self.temp.name))
        self.middleware=JournalMiddleware(lambda:self.journal)

    async def asyncTearDown(self):
        self.journal.close();self.temp.cleanup()

    async def send(self, text, uid=123, username='test_user', state=None, update_id=1):
        event=types.Update.model_validate({'update_id':update_id,'message':{'message_id':1,'date':1792770000,
            'chat':{'id':uid,'type':'private'},'from':{'id':uid,'is_bot':False,'first_name':'Ignored','username':username},'text':text}})
        context=SimpleNamespace(get_state=AsyncMock(return_value=state))
        await self.middleware(AsyncMock(),event,{'state':context})

    async def test_buttons_text_and_grouping(self):
        await self.send('🔐 Авторизация')
        await self.send('Volleyball',uid=999,username='other',update_id=2)
        rows=self.journal.db.execute('SELECT telegram_id,username,action,content,result FROM events ORDER BY id').fetchall()
        self.assertEqual(rows,[(123,'test_user','button','🔐 Авторизация','processed'),(999,'other','text_input','Volleyball','processed')])

    async def test_authentication_values_never_stored(self):
        await self.send('person@innopolis.university',state='AuthState:username')
        await self.send('very-private-password',state='AuthState:password',update_id=2)
        dump='\n'.join(self.journal.db.iterdump())
        self.assertNotIn('person@',dump);self.assertNotIn('very-private-password',dump)
        self.assertIn('auth_password_input',dump)

    async def test_username_changes_keep_same_user_and_history(self):
        await self.send('/start')
        await self.send('/app',username='renamed',update_id=2)
        self.assertEqual(self.journal.db.execute('SELECT count(*) FROM telegram_users').fetchone()[0],1)
        self.assertEqual(self.journal.db.execute('SELECT username FROM telegram_users').fetchone()[0],'renamed')
        self.assertEqual(self.journal.db.execute('SELECT count(*) FROM events WHERE telegram_id=123').fetchone()[0],2)


if __name__=='__main__':
    unittest.main()
