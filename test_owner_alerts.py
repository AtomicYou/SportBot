import asyncio
import json
import logging
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from owner_alerts import OwnerAlerts


class AlertNoiseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        (root/'moderator-identity.json').write_text(json.dumps({'telegram_id':777}))
        self.db = sqlite3.connect(':memory:')
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.alerts = OwnerAlerts(self.bot, SimpleNamespace(directory=root, db=self.db))
        self.clock = 0
        self.alerts.clock = lambda:self.clock

    async def asyncTearDown(self):
        await self.alerts.shutdown()
        self.db.close()
        self.directory.cleanup()

    def signal(self, second, key='a', ok=False):
        self.clock = second
        self.alerts.observe('portal', key, ok)

    async def test_transient_failure_and_scheduler_delay_are_silent(self):
        self.signal(0);self.signal(60);self.signal(61,ok=True)
        self.alerts.emit(logging.LogRecord('apscheduler.executors.default',logging.WARNING,'',0,'Job missed',(),None))
        await asyncio.sleep(0)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(list(self.alerts.recent),[])

    async def test_persistent_failure_sends_once_then_one_recovery(self):
        self.signal(0);self.signal(60);self.signal(120)
        await self.alerts.queue.join()
        for second in [180,240,3600]:self.signal(second)
        await self.alerts.queue.join()
        self.bot.send_message.assert_awaited_once()
        self.signal(3601,ok=True)
        await self.alerts.queue.join()
        self.assertEqual(self.bot.send_message.await_count,2)
        self.assertIn('восстановлена',self.bot.send_message.call_args.args[1])

    async def test_other_account_success_cannot_reset_failed_account(self):
        self.signal(0);self.signal(30,'b',True);self.signal(60);self.signal(90,'b',True);self.signal(120)
        await self.alerts.queue.join()
        self.bot.send_message.assert_awaited_once()

    async def test_recovery_waits_for_every_reported_account(self):
        for second in [0,60,120]:self.signal(second)
        await self.alerts.queue.join()
        for second in [130,190,250]:self.signal(second,'b')
        self.signal(251,ok=True)
        await self.alerts.queue.join()
        self.bot.send_message.assert_awaited_once()
        self.signal(252,'b',True)
        await self.alerts.queue.join()
        self.assertEqual(self.bot.send_message.await_count,2)

    async def test_resolved_problem_is_not_delivered_from_queue(self):
        self.signal(0);self.signal(60);self.signal(120);self.signal(121,ok=True)
        await self.alerts.queue.join()
        self.bot.send_message.assert_not_awaited()

    async def test_persistent_outage_is_reconsidered_after_cooldown(self):
        self.db.execute('INSERT INTO operational_alerts VALUES (?,?)',('portal',datetime.now(timezone.utc).isoformat()))
        self.db.commit()
        self.signal(0);self.signal(60);self.signal(120)
        self.assertTrue(self.alerts.queue.empty())
        self.db.execute("UPDATE operational_alerts SET last_sent='2000-01-01T00:00:00+00:00'")
        self.db.commit()
        self.signal(180)
        await self.alerts.queue.join()
        self.bot.send_message.assert_awaited_once()

    async def test_real_error_is_immediate_private_and_deduplicated(self):
        record=logging.LogRecord('app',logging.ERROR,'',0,'private-password',(),None)
        self.alerts.emit(record);self.alerts.emit(record)
        await asyncio.sleep(0)
        await self.alerts.queue.join()
        self.bot.send_message.assert_awaited_once()
        self.assertNotIn('private-password',self.bot.send_message.call_args.args[1])
