import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from apscheduler.executors.base import run_job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from owner_alerts import category_for


class SchedulingTests(unittest.TestCase):
    def test_late_job_runs_with_grace_but_stale_job_is_skipped(self):
        # Exercise APScheduler's actual execution decision without waiting.
        callback = Mock()
        scheduler = AsyncIOScheduler(job_defaults={'misfire_grace_time': 60})
        job = scheduler.add_job(callback, 'interval', minutes=1, misfire_grace_time=60, coalesce=True)
        run_job(job, 'default', [datetime.now(timezone.utc)-timedelta(seconds=2)], 'test.scheduler')
        callback.assert_called_once()
        run_job(job, 'default', [datetime.now(timezone.utc)-timedelta(seconds=120)], 'test.scheduler')
        callback.assert_called_once()

    def test_production_scheduler_grace_and_alert_categories(self):
        import bot
        self.assertEqual(bot.scheduler._job_defaults['misfire_grace_time'], 60)
        warning = logging.LogRecord('apscheduler.executors.default', logging.WARNING, '', 0,
                                    'Run time of job was missed', (), None)
        failure = logging.LogRecord('apscheduler.executors.default', logging.ERROR, '', 0,
                                    'Job raised an exception', (), None)
        self.assertEqual(category_for(warning), 'scheduler')
        self.assertEqual(category_for(failure), 'scheduler_error')
        self.assertEqual(category_for(logging.LogRecord('core', logging.WARNING, '', 0, 'private', (), None)), 'portal')
