"""Operational alerts only to the pinned owner's numeric Telegram ID."""
import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone

LABELS = {
    'portal': 'Ошибка обращения к спортивному порталу или автоматической записи.',
    'reminders': 'Не удалось проверить или отправить напоминание.',
    'telegram': 'Ошибка соединения с Telegram или обработки обновлений.',
    'app': 'Ошибка в обработчике бота или Mini App.',
    'scheduler': 'Один из фоновых запусков пропущен или задержан. Планировщик продолжит работу по расписанию.',
    'scheduler_error': 'Ошибка выполнения фоновой задачи. Проверь журнал сервера.',
}


def category_for(record):
    if record.name.startswith('apscheduler'):
        return 'scheduler_error' if record.levelno >= logging.ERROR else 'scheduler'
    return ('portal' if record.name == 'core' else 'reminders' if record.name == 'booking_features'
            else 'telegram' if record.name.startswith('aiogram') else 'app')


class OwnerAlerts(logging.Handler):
    def __init__(self, bot, store):
        super().__init__(logging.WARNING)
        self.recent = deque(maxlen=20)
        self.bot, self.db = bot, store.db
        identity = store.directory / 'moderator-identity.json'
        self.owner = int(json.loads(identity.read_text(encoding='utf-8'))['telegram_id']) if identity.exists() else None
        self.db.execute('CREATE TABLE IF NOT EXISTS operational_alerts (category TEXT PRIMARY KEY,last_sent TEXT)')
        self.db.commit()
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=8)
        self.pending = set()
        self.worker = asyncio.create_task(self.run())

    def emit(self, record):
        if not self.owner or record.name == __name__:
            return
        category = category_for(record)
        self.recent.append({"time": time.time(), "category": category})
        # Neither exception text, log messages, nor account data leave the process.
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.enqueue, category)

    def enqueue(self, category):
        if not self.owner or category not in LABELS or category in self.pending:
            return
        row = self.db.execute('SELECT last_sent FROM operational_alerts WHERE category=?', (category,)).fetchone()
        if row and datetime.now(timezone.utc)-datetime.fromisoformat(row[0]) < timedelta(minutes=30):
            return
        self.pending.add(category)
        self.queue.put_nowait(category)

    async def run(self):
        while True:
            category = await self.queue.get()
            try:
                while True:
                    try:
                        if category == 'scheduler':
                            message = ('Задержка фоновой задачи Inno Sport\n'+LABELS[category]+
                                       '\nПри повторении проверь нагрузку и режим сна ноутбука.')
                        else:
                            message = ('Сбой Inno Sport\n'+LABELS[category]+
                                       '\nПроверь журнал и соединение сервера. Личные данные пользователей в уведомление не включены.')
                        await self.bot.send_message(self.owner, message)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Keep one pending alert until Telegram is reachable again.
                        await asyncio.sleep(60)
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO operational_alerts VALUES (?,?)',
                                    (category, datetime.now(timezone.utc).isoformat()))
            finally:
                self.pending.discard(category)
                self.queue.task_done()

    async def shutdown(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
