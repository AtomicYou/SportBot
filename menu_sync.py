"""Publish the current tunnel URL globally and explicitly for every known chat."""
import logging

from aiogram import types
from aiogram.exceptions import TelegramAPIError


class MiniAppMenu:
    def __init__(self, bot, url):
        self.bot = bot
        self.url = url
        self.synced = set()

    async def sync_chat(self, chat_id):
        chat_id = int(chat_id)
        if chat_id in self.synced:
            return
        try:
            await self.bot.set_chat_menu_button(
                chat_id=chat_id, menu_button=types.MenuButtonWebApp(text='Inno Sport', web_app=types.WebAppInfo(url=self.url)))
        except TelegramAPIError as error:
            # A blocked bot or temporary network error must not prevent polling.
            # Leave this chat eligible for another attempt on its next update.
            logging.warning('Could not reset Mini App chat menu: %s', type(error).__name__)
        else:
            self.synced.add(chat_id)

    async def publish(self, chat_ids):
        await self.bot.set_chat_menu_button(menu_button=types.MenuButtonWebApp(
            text='Inno Sport', web_app=types.WebAppInfo(url=self.url)))
        for chat_id in sorted({int(value) for value in chat_ids}):
            await self.sync_chat(chat_id)

