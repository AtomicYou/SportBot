"""Read-only desktop monitor, on a separate loopback port outside the tunnel."""
import asyncio
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
from aiohttp import web

ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
ASSETS = Path(getattr(sys, '_MEIPASS', ROOT))/'monitor-web'
MENU_BUTTONS = {'🔐 Авторизация','🎯 Добавить цель','📋 Мой вишлист','📅 Мои записи','🚀 Искать сейчас','⏸ Пауза / Продолжить','❌ Отмена','🏟 Открыть Mini App','🛟 Помощь при ошибках'}
JOBS = {'wishlist_scanner': 'Поиск занятий', 'BookingFeatures.reminders': 'Напоминания',
        'ServerOperations.daily': 'Проверка резервных копий'}


def safe_command(action, content):
    if action == 'command':
        match = re.match(r'^/[A-Za-z0-9_]{1,32}(?:@[A-Za-z0-9_]+)?(?=\s|$)', content or '')
        return match[0] if match else 'Команда'
    if action == 'button':
        return content if content in MENU_BUTTONS else 'Кнопка меню'
    return {'inline_button': 'Нажатие inline-кнопки', 'auth_email_input': 'Ввод логина · скрыт',
            'auth_password_input': 'Ввод пароля · скрыт', 'text_input': 'Текстовое сообщение',
            'attachment': 'Вложение'}.get(action, 'Событие Telegram')


class Monitor:
    def __init__(self, root=ROOT):
        self.root = root
        self.process = None
        self.history = []
        self.snapshot = {'available': False, 'error': 'Подключаемся к журналам…'}
        psutil.cpu_percent()

    def collect(self):
        if self.process is None or not self.process.is_running():
            self.process = None
            for proc in psutil.process_iter(['name', 'cmdline', 'cwd']):
                try:
                    args = proc.info['cmdline'] or []
                    if 'python' in (proc.info['name'] or '').lower() and any(Path(a).name == 'bot.py' for a in args[1:]) and Path(proc.info['cwd'] or '').resolve() == self.root:
                        # The venv launcher may own a child running the real bot.
                        candidates = [c for c in proc.children() if 'python' in c.name().lower()]
                        self.process = candidates[0] if candidates else proc
                        self.process.cpu_percent()
                        break
                except (psutil.Error, OSError):
                    continue
        process = {'running': False, 'cpu': None, 'memory_mb': None, 'uptime': None, 'threads': None}
        try:
            if self.process:
                process = {'running': True, 'cpu': self.process.cpu_percent(),
                           'memory_mb': round(self.process.memory_info().rss/1048576, 1),
                           'uptime': max(0, int(time.time()-self.process.create_time())),
                           'threads': self.process.num_threads()}
        except psutil.Error:
            self.process = None
        minute = (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        requests, counts = [], {'minute': 0, 'users_today': 0, 'errors_today': 0, 'pending': 0}
        today = datetime.now(timezone(timedelta(hours=3))).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        try:
            db = sqlite3.connect((self.root/'data/journal.sqlite3').as_uri()+'?mode=ro', uri=True, timeout=1)
            try:
                counts['minute'] = db.execute('SELECT count(*) FROM events WHERE time>=?', (minute,)).fetchone()[0]
                counts['users_today'] = db.execute('SELECT count(DISTINCT telegram_id) FROM events WHERE time>=?', (today,)).fetchone()[0]
                counts['errors_today'] = db.execute("SELECT count(*) FROM events WHERE time>=? AND result='error'", (today,)).fetchone()[0]
                counts['pending'] = db.execute("SELECT count(*) FROM events WHERE time>=? AND result='received'", (minute,)).fetchone()[0]
                for stamp, uid, username, action, content, result in db.execute('SELECT time,telegram_id,username,action,content,result FROM events ORDER BY id DESC LIMIT 35'):
                    requests.append(dict(time=stamp, user='@'+username.lstrip('@') if username else str(uid or '—'),
                                         command=safe_command(action, content), result=result))
            finally:
                db.close()
            journal_ok = True
        except sqlite3.Error:
            journal_ok = False
        jobs, actions, latencies = {}, [], []
        try:
            with (self.root/'data/bot-error.log').open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell()-131072))
                lines = stream.read().decode('utf-8', errors='replace').splitlines()
            for line in lines:
                if not re.match(r'^\d{4}-\d\d-\d\d ', line):
                    continue
                stamp = line[:23].replace(',', '.')+'+03:00'
                for key, title in JOBS.items():
                    if '"'+key+' ' not in line:
                        continue
                    state = ('выполняется' if 'Running job ' in line else 'завершено' if 'executed successfully' in line
                             else 'пропущено' if 'was missed' in line or 'skipped' in line else 'ошибка' if 'ERROR' in line else None)
                    if state:
                        jobs[key] = dict(title=title, state=state, time=stamp)
                        actions.append(dict(time=stamp, title=title, state=state))
                match = re.search(r'Update id=\d+ is handled\. Duration (\d+) ms', line)
                if match:
                    latency = int(match[1]); latencies.append(latency)
                    actions.append(dict(time=stamp, title='Обработано обновление Telegram', state=str(latency)+' мс'))
                elif 'ERROR' in line and not any(key in line for key in JOBS):
                    actions.append(dict(time=stamp, title='Ошибка в журнале бота', state='ошибка'))
            log_ok = True
        except OSError:
            log_ok = False
        self.history.append(dict(time=time.time(), cpu=process['cpu'], requests=counts['minute']))
        self.history = self.history[-150:]
        return dict(available=True, updated=time.time(), process=process, host_cpu=psutil.cpu_percent(),
                    host_ram=psutil.virtual_memory().percent, counts=counts, requests=requests,
                    jobs=list(jobs.values()), actions=list(reversed(actions[-35:])), history=self.history,
                    latency_ms=round(sum(latencies[-30:])/len(latencies[-30:])) if latencies else None,
                    journal_ok=journal_ok, log_ok=log_ok)


def create_app(root=ROOT, port=8085):
    monitor = Monitor(root)
    @web.middleware
    async def local_only(request, handler):
        if request.host not in (f'127.0.0.1:{port}', f'localhost:{port}'):
            raise web.HTTPForbidden()
        origin = request.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            raise web.HTTPForbidden()
        response = await handler(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"})
        return response

    app = web.Application(middlewares=[local_only])
    async def sampling(app):
        async def run():
            while True:
                try:
                    monitor.snapshot = await asyncio.to_thread(monitor.collect)
                except Exception:
                    monitor.snapshot = {'available': False, 'error': 'Не удалось прочитать показатели. Повторяем…'}
                await asyncio.sleep(2)
        task = asyncio.create_task(run())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    app.cleanup_ctx.append(sampling)

    async def data(request):
        return web.json_response(monitor.snapshot)
    async def asset(request):
        name = request.match_info.get('name', 'index.html')
        if name not in ('index.html', 'monitor.js', 'monitor.css'):
            raise web.HTTPNotFound()
        return web.FileResponse(ASSETS/name)
    app.router.add_get('/api/stats', data)
    app.router.add_get('/', asset)
    app.router.add_get('/{name}', asset)
    return app


if __name__ == '__main__':
    web.run_app(create_app(), host='127.0.0.1', port=8085, access_log=None, print=None)
