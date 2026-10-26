import unittest
from datetime import timedelta
from unittest.mock import patch
import core
import test_core


class PortalSyncTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_core.CoreTests.setUp
    asyncTearDown = test_core.CoreTests.asyncTearDown

    def event(self, checked=False, available=False):
        return {'title': 'Volleyball', 'start': self.dt.isoformat(),
                'extendedProps': {'id': 99, 'checked_in': checked, 'can_check_in': available}}

    async def test_external_cancellation_confirmed_and_never_auto_rebooked(self):
        base = core.now()
        with patch.object(core, 'now', return_value=base):
            self.store.mark_booked('1', 99)
        with patch.object(core, 'now', return_value=base+timedelta(seconds=121)):
            await self.engine.reconcile_portal('1', [self.event()])
            self.assertTrue(self.store.booked('1', 99))
        with patch.object(core, 'now', return_value=base+timedelta(seconds=152)):
            await self.engine.reconcile_portal('1', [self.event()])
        self.assertFalse(self.store.booked('1', 99))
        self.assertIn('99', self.store.users['1']['cancelled'])
        await self.engine.attempt('1',99,'Volleyball',self.dt)
        self.portal.book.assert_not_awaited()
        self.app.bot.send_message.assert_awaited_once()
        # Explicit request for this event restores eligibility, not all cancelled events.
        self.store.add_waiting('1', self.event())
        self.assertTrue(self.engine.active('1',99,'Volleyball',self.dt))

    async def test_fresh_success_missing_field_and_empty_calendar_are_not_cancellation(self):
        base = core.now()
        with patch.object(core, 'now', return_value=base):
            self.store.mark_booked('1', 99)
        with patch.object(core, 'now', return_value=base+timedelta(seconds=1)):
            await self.engine.reconcile_portal('1', [self.event()])
        with patch.object(core, 'now', return_value=base+timedelta(minutes=5)):
            event=self.event();event['extendedProps'].pop('checked_in')
            await self.engine.reconcile_portal('1', [event])
            await self.engine.reconcile_portal('1', [])
        self.assertTrue(self.store.booked('1',99))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM portal_missing_booking').fetchone()[0],0)

    async def test_positive_snapshot_resets_pending_negative_and_manual_site_booking_is_imported(self):
        await self.engine.reconcile_portal('1', [self.event(checked=True)])
        self.assertTrue(self.store.booked('1',99))
        self.store.db.execute("UPDATE portal_booking_receipts SET confirmed_at='2020-01-01T00:00:00+00:00'")
        self.store.db.commit()
        await self.engine.reconcile_portal('1', [self.event()])
        await self.engine.reconcile_portal('1', [self.event(checked=True)])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM portal_missing_booking').fetchone()[0],0)
        self.assertNotIn('99',self.store.users['1']['cancelled'])

    async def test_changes_are_persisted_and_available_place_triggers_booking(self):
        first=self.event()
        await self.engine.reconcile_portal('1',[first])
        opened=self.event(available=True)
        opened['start']=(self.dt+timedelta(hours=1)).isoformat()
        self.assertTrue(await self.engine.reconcile_portal('1',[opened]))
        kinds={r[0] for r in self.store.db.execute('SELECT kind FROM portal_changes')}
        self.assertTrue({'new_training','availability_changed','training_changed'}<=kinds)
        await self.engine.scan('1',events_snapshot=[opened])
        self.portal.book.assert_awaited_once()

    async def test_polling_continues_when_reminders_and_auto_booking_are_disabled(self):
        self.store.users['1']['reminders']=False
        self.app.store=self.store
        self.store.features.update({**self.store.features.flags,'auto_booking':False})
        self.portal.schedule.return_value=[self.event(checked=True)]
        await self.engine.features.reminders()
        self.assertTrue(self.store.booked('1',99))
        self.portal.book.assert_not_awaited()

    async def test_disappearance_requires_complete_calendar_and_restoration_reenables_event(self):
        await self.engine.reconcile_portal('1', [self.event()])
        another = self.event(); another['extendedProps'] = {**another['extendedProps'], 'id':100}
        await self.engine.reconcile_portal('1', [another])
        self.assertTrue(self.store.portal_sync.in_schedule('1',99))
        await self.engine.reconcile_portal('1', [], complete=True)
        self.assertTrue(self.store.portal_sync.in_schedule('1',99))
        await self.engine.reconcile_portal('1', [another], complete=True)
        self.assertFalse(self.store.portal_sync.in_schedule('1',99))
        self.assertFalse(self.engine.active('1',99,'Volleyball',self.dt))
        await self.engine.reconcile_portal('1', [self.event(),another], complete=True)
        self.assertTrue(self.store.portal_sync.in_schedule('1',99))

    async def test_pending_cancellation_and_confirmed_cancellation_survive_restart(self):
        await self.store.save()
        self.store.mark_booked('1',99)
        self.store.db.execute("UPDATE portal_booking_receipts SET confirmed_at='2020-01-01T00:00:00+00:00'")
        self.store.db.commit()
        first=core.now()
        with patch.object(core,'now',return_value=first):
            await self.engine.reconcile_portal('1',[self.event()])
        reopened=core.Store()
        try:
            with patch.object(core,'now',return_value=first+timedelta(seconds=40)):
                reopened.portal_sync.observe('1',[self.event()])
            self.assertIn('99',reopened.users['1']['cancelled'])
            self.assertFalse(reopened.booked('1',99))
        finally:
            reopened.db.close()
