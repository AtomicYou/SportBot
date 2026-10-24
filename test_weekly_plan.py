import unittest
from datetime import timedelta
from core import now
import test_miniapp

class WeeklyPlanAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_miniapp.APITests.asyncSetUp
    asyncTearDown=test_miniapp.APITests.asyncTearDown

    async def test_past_bookings_are_separate_and_do_not_expose_extra_fields(self):
        old={**self.event,'start':(now()-timedelta(days=1)).isoformat(),
             'end':(now()-timedelta(days=1)+timedelta(hours=1)).isoformat(),
             'extendedProps':{'id':43,'checked_in':True}}
        not_booked={**old,'extendedProps':{'id':44,'checked_in':False}}
        self.backend.portal.schedule.return_value=[self.event,old,not_booked]
        response=await self.client.get('/api/schedule?week=0',headers=self.headers)
        data=await response.json()
        self.assertEqual([e['id'] for e in data['events']],['42'])
        self.assertEqual([e['id'] for e in data['past_bookings']],['43'])
        self.assertEqual(data['past_bookings'][0]['end'],old['end'])
        self.assertNotIn('private_cookie',data['past_bookings'][0])

    async def test_cancelled_ids_only_for_current_account(self):
        self.backend.USERS['123']['cancelled']=['42']
        self.backend.USERS['999']['cancelled']=['other-private-id']
        response=await self.client.get('/api/profile',headers=self.headers)
        data=await response.json()
        self.assertEqual(data['cancelled_ids'],['42'])
        self.assertNotIn('other-private-id',str(data))
