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
        self.failures = {}
        self.active = {}
        self.notified = set()
        self.clock = time.monotonic
        self.worker = asyncio.create_task(self.run())

    def observe(self, category, key, ok):
        """Track each account separately so another account cannot hide an outage."""
        identity = (category, str(key))
        if ok:
            self.failures.pop(identity, None)
            keys = self.active.get(category)
            if keys is not None:
                keys.discard(str(key))
                if not keys:
                    self.active.pop(category, None)
                    if category in self.notified:
                        self.enqueue(category, recovery=True, health=True)
            return
        clock = self.clock()
        count, first = self.failures.get(identity, (0, clock))
        self.failures[identity] = (count+1, first)
        if count+1 >= 3 and clock-first >= 120:
            opened = category in self.active
            self.active.setdefault(category, set()).add(str(key))
            if not opened or category not in self.notified:
                self.enqueue(category, health=True)

    def emit(self, record):
        if not self.owner or record.name == __name__ or getattr(record, 'owner_alert', True) is False:
            return
        category = category_for(record)
        if category == 'scheduler':
            return  # Missed or overlapping runs remain warnings in the local log.
        # Neither exception text, log messages, nor account data leave the process.
        if not self.loop.is_closed():
            if record.levelno >= logging.ERROR:
                self.loop.call_soon_threadsafe(self.enqueue, category)
            elif record.levelno >= logging.WARNING:
                self.loop.call_soon_threadsafe(self.observe, category, record.name, False)

    def enqueue(self, category, recovery=False, health=False):
        pending_key = (category, recovery)
        if not self.owner or category not in LABELS or pending_key in self.pending:
            return
        row = self.db.execute('SELECT last_sent FROM operational_alerts WHERE category=?', (category,)).fetchone()
        if not recovery and row and datetime.now(timezone.utc)-datetime.fromisoformat(row[0]) < timedelta(minutes=30):
            return
        try:
            self.queue.put_nowait((category, recovery, health))
        except asyncio.QueueFull:
            return
        self.pending.add(pending_key)

    async def run(self):
        while True:
            category, recovery, health = await self.queue.get()
            try:
                while True:
                    if health and ((recovery and category in self.active) or (not recovery and category not in self.active)):
                        break  # Do not deliver an obsolete outage after the connection returns.
                    try:
                        if recovery:
                            message = 'Inno Sport: связь со спортивным порталом восстановлена.' if category == 'portal' else 'Inno Sport: работа восстановлена.'
                        elif health:
                            message = ('Inno Sport: повторяются сбои загрузки расписания.\n'
                                       'Не менее трёх неудачных проверок за две минуты. Бот продолжает попытки; сообщу о восстановлении.') if category == 'portal' else ('Inno Sport: устойчивая проблема.\n'+LABELS[category])
                        elif category == 'scheduler':
                            message = ('Задержка фоновой задачи Inno Sport\n'+LABELS[category]+
                                       '\nПри повторении проверь нагрузку и режим сна ноутбука.')
                        else:
                            message = ('Сбой Inno Sport\n'+LABELS[category]+
                                       '\nПроверь журнал и соединение сервера. Личные данные пользователей в уведомление не включены.')
                        await self.bot.send_message(self.owner, message)
                        if not recovery:
                            self.recent.append({'time': time.time(), 'category': category})
                        if health:
                            if recovery:
                                self.notified.discard(category)
                            else:
                                self.notified.add(category)
                                if category not in self.active:
                                    self.enqueue(category, recovery=True, health=True)
                        if not recovery:
                            with self.db:
                                self.db.execute('INSERT OR REPLACE INTO operational_alerts VALUES (?,?)',
                                                (category, datetime.now(timezone.utc).isoformat()))
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Keep one pending alert until Telegram is reachable again.
                        await asyncio.sleep(60)
            finally:
                self.pending.discard((category, recovery))
                self.queue.task_done()

    async def shutdown(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
