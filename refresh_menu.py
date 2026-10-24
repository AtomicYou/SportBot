"""Refresh and verify Telegram menu buttons after deploying any bot/UI edits."""
import asyncio
import sqlite3
from pathlib import Path

from aiogram import Bot
from dotenv import dotenv_values

from menu_sync import MiniAppMenu

ROOT = Path(__file__).resolve().parent


async def main():
    url = (ROOT / 'data' / 'miniapp-url.txt').read_text(encoding='utf-8').strip()
    token = dotenv_values(ROOT / '.env')['BOT_TOKEN']
    chats = set()
    for name, query in [
        ('journal.sqlite3', 'SELECT telegram_id FROM telegram_users'),
        ('bot.sqlite3', 'SELECT user FROM telegram_profiles'),
    ]:
        path = ROOT / 'data' / name
        if path.exists():
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                chats.update(int(row[0]) for row in db.execute(query))
    async with Bot(token).context() as bot:
        sync = MiniAppMenu(bot, url)
        await sync.publish(chats)
        menu = await bot.get_chat_menu_button()
        if menu.type != 'web_app' or menu.web_app.url.rstrip('/') != url.rstrip('/'):
            print('Global menu still returns an older value; verifying explicit chat buttons.')
        for chat_id in sorted(sync.synced):
            menu = await bot.get_chat_menu_button(chat_id=chat_id)
            if menu.type != 'web_app' or menu.web_app.url.rstrip('/') != url.rstrip('/'):
                raise RuntimeError('Private chat menu verification failed.')
        print(f'Mini App menu updated and verified; chats: {len(sync.synced)}/{len(chats)}.')
        if sync.synced != chats:
            raise RuntimeError('Some chats could not be updated; see warning classes above.')


if __name__ == '__main__':
    asyncio.run(main())
