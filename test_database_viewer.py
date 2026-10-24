import json
import unittest
from types import SimpleNamespace
from urllib.parse import quote

import test_miniapp
from request_journal import RequestJournal


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_miniapp.ModerationTests.asyncSetUp
    asyncTearDown = test_miniapp.ModerationTests.asyncTearDown

    async def test_owner_only_and_no_arbitrary_paths_or_sql(self):
        url='/api/admin/databases/main'
        self.assertEqual((await self.client.get(url)).status,401)
        self.assertEqual((await self.client.get(url,headers=self.member)).status,403)
        self.assertEqual((await self.client.get(url,headers=self.owner)).status,200)
        self.assertEqual((await self.client.get('/api/admin/databases/unknown',headers=self.owner)).status,404)
        self.assertEqual((await self.client.get(url+'/users;DROP TABLE users',headers=self.owner)).status,404)
        self.assertEqual((await self.client.post(url+'/users',headers=self.owner)).status,405)

    async def test_user_rows_match_sqlite_without_json_rewriting_or_redaction(self):
        await self.store.save()
        expected=[dict(zip(('id','data'),row)) for row in self.store.db.execute('SELECT id,data FROM users ORDER BY id DESC')]
        response=await self.client.get('/api/admin/databases/main/users',headers=self.owner)
        self.assertEqual((await response.json())['rows'],expected)
        result=await self.client.get('/api/admin/databases/main/users',headers=self.owner,params={'q':'private@example.com'})
        data=await result.json()
        self.assertEqual(data['total'],1)
        self.assertEqual(data['rows'][0]['id'],'999')

    async def test_journal_matches_stored_text_metadata_ids_and_usernames(self):
        journal=RequestJournal(self.store.directory)
        try:
            for index in range(65):
                journal.record(index,SimpleNamespace(id=999,username='PRIVATE_USERNAME'),
                               'message','text_input','Private name and phone 123456',{'message_id':index})
            result=await self.client.get('/api/admin/databases/journal/events',headers=self.owner)
            data=await result.json()
            self.assertEqual(len(data['rows']),50)
            self.assertEqual(data['total'],65)
            row=data['rows'][0]
            self.assertEqual(row['username'],'PRIVATE_USERNAME')
            self.assertEqual(row['telegram_id'],999)
            self.assertEqual(row['content'],'Private name and phone 123456')
            self.assertEqual(json.loads(row['details']),{'message_id':64})
            result=await self.client.get('/api/admin/databases/journal/events?page=2',headers=self.owner)
            self.assertEqual(len((await result.json())['rows']),15)
        finally:
            journal.close()

    async def test_all_columns_settings_and_new_tables_are_available(self):
        self.store.db.execute("INSERT INTO settings VALUES ('api_key','secret-value')")
        self.store.db.execute('ALTER TABLE approved_bookings ADD COLUMN private_field TEXT')
        self.store.db.execute("INSERT INTO approved_bookings(user,training,private_field) VALUES ('999','42','stored-value')")
        self.store.db.execute('CREATE TABLE "odd""name" (value TEXT)')
        self.store.db.execute('INSERT INTO "odd""name" VALUES (?)',('<script>literal text</script>',))
        self.store.db.commit()
        result=await self.client.get('/api/admin/databases/main/settings?q=secret-value',headers=self.owner)
        self.assertEqual((await result.json())['rows'][0]['value'],'secret-value')
        result=await self.client.get('/api/admin/databases/main/approved_bookings',headers=self.owner)
        self.assertEqual((await result.json())['rows'][0]['private_field'],'stored-value')
        result=await self.client.get('/api/admin/databases/main/'+quote('odd"name',safe=''),headers=self.owner)
        self.assertEqual((await result.json())['rows'][0]['value'],'<script>literal text</script>')

    async def test_more_than_ten_thousand_rows_and_precise_binary_and_integers(self):
        self.store.db.execute('CREATE TABLE numbers (id INTEGER, data BLOB)')
        self.store.db.executemany('INSERT INTO numbers VALUES (?,?)',((i,b'\x00\xff') for i in range(10051)))
        self.store.db.commit()
        result=await self.client.get('/api/admin/databases/main/numbers?page=202',headers=self.owner)
        data=await result.json()
        self.assertEqual(data['total'],10051)
        self.assertEqual(len(data['rows']),1)
        self.assertFalse(data['limited'])
        self.assertEqual(data['rows'][0]['data'],{'sqlite_type':'blob','hex':'00ff'})
        self.store.db.execute('INSERT INTO numbers VALUES (?,NULL)',(9223372036854775807,))
        self.store.db.commit()
        result=await self.client.get('/api/admin/databases/main/numbers',headers=self.owner)
        self.assertEqual((await result.json())['rows'][0]['id']['value'],'9223372036854775807')
