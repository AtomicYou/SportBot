"""Local Telegram activity journal. Authentication input is never retained."""
import json
import re
import sqlite3
from datetime import datetime, timezone

from aiogram import BaseMiddleware

BUTTONS = {'🔐 Авторизация', '🎯 Добавить цель', '📋 Мой вишлист', '📅 Мои записи',
           '🚀 Искать сейчас', '⏸ Пауза / Продолжить', '❌ Отмена', '🏟 Открыть Mini App'}


def scrub(text):
    if not text:
        return text
    text = re.sub(r'\b(?:bot)?\d{6,}:[A-Za-z0-9_-]{20,}\b', '[токен скрыт]', text)
    return re.sub(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', '[email скрыт]', text)


class RequestJournal:
    def __init__(self, directory):
        self.db = sqlite3.connect(directory / 'journal.sqlite3')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS telegram_users (
                telegram_id INTEGER PRIMARY KEY, username TEXT, last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, update_id INTEGER UNIQUE NOT NULL,
                time TEXT NOT NULL, telegram_id INTEGER, username TEXT,
                event_type TEXT NOT NULL, action TEXT NOT NULL, content TEXT,
                details TEXT NOT NULL, result TEXT NOT NULL, error_type TEXT
            );
            CREATE INDEX IF NOT EXISTS events_by_user ON events(telegram_id,id);
            CREATE VIEW IF NOT EXISTS user_requests AS
                SELECT username,telegram_id,time,event_type,action,content,result,error_type
                FROM events ORDER BY id DESC;
        ''')

    def record(self, update_id, user, event_type, action, content, details):
        stamp = datetime.now(timezone.utc).isoformat()
        uid = user.id if user else None
        username = user.username if user else None
        with self.db:
            if uid is not None:
                self.db.execute('INSERT INTO telegram_users VALUES (?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,last_seen=excluded.last_seen',
                                (uid, username, stamp))
            self.db.execute('INSERT OR IGNORE INTO events(update_id,time,telegram_id,username,event_type,action,content,details,result) VALUES (?,?,?,?,?,?,?,?,?)',
                            (update_id,stamp,uid,username,event_type,action,content,json.dumps(details,ensure_ascii=False),'received'))

    def finish(self, update_id, result, error=None):
        with self.db:
            self.db.execute('UPDATE events SET result=?,error_type=? WHERE update_id=?', (result,error,update_id))

    def close(self):
        self.db.close()


class JournalMiddleware(BaseMiddleware):
    def __init__(self, get_journal):
        self.get_journal = get_journal

    async def __call__(self, handler, event, data):
        journal = self.get_journal()
        if journal is None:
            return await handler(event, data)
        kind = event.event_type
        payload = event.event
        user = getattr(payload, 'from_user', None) or data.get('event_from_user')
        message = getattr(event, 'message', None) or getattr(event, 'edited_message', None)
        state = data.get('state')
        state_name = await state.get_state() if state else None
        content, action, details = None, kind, {}
        if message:
            text = message.text or message.caption
            details = {'message_id':message.message_id,'content_type':str(message.content_type)}
            if message.chat.type != 'private':
                content, action = '[содержимое группового сообщения скрыто]', 'group_message'
            elif state_name in ('AuthState:username','AuthState:password'):
                action = 'auth_email_input' if state_name.endswith(':username') else 'auth_password_input'
                content = '[ввод данных авторизации скрыт]'
            else:
                content = scrub(text)
                action = 'command' if text and text.startswith('/') else 'button' if text in BUTTONS else 'text_input' if text else 'attachment'
        elif kind == 'callback_query':
            content, action = scrub(payload.data), 'inline_button'
        journal.record(event.update_id,user,kind,action,content,details)
        data['journal_update_id'] = event.update_id
        try:
            result = await handler(event,data)
        except BaseException as exc:
            journal.finish(event.update_id,'error',type(exc).__name__)
            raise
        else:
            # aiogram's error handler may already have recorded a handled error.
            journal.db.execute("UPDATE events SET result='processed' WHERE update_id=? AND result='received'", (event.update_id,))
            journal.db.commit()
            return result
