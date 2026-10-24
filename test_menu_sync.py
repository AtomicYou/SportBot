import unittest
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SetChatMenuButton
from menu_sync import MiniAppMenu


class MenuSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_sets_global_url_and_removes_private_overrides(self):
        bot = AsyncMock()
        menu = MiniAppMenu(bot, 'https://current.example.com')
        await menu.publish(['2', '1', '2'])
        calls = bot.set_chat_menu_button.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertNotIn('chat_id', calls[0].kwargs)
        self.assertEqual(calls[0].kwargs['menu_button'].web_app.url, menu.url)
        self.assertEqual([call.kwargs['chat_id'] for call in calls[1:]], [1, 2])
        self.assertTrue(all(call.kwargs['menu_button'].type == 'web_app' for call in calls[1:]))
        await menu.sync_chat(1)
        self.assertEqual(bot.set_chat_menu_button.await_count, 3)

    async def test_blocked_chat_does_not_stop_others_and_can_retry(self):
        bot = AsyncMock()
        bot.set_chat_menu_button.side_effect = [True, TelegramForbiddenError(
            method=SetChatMenuButton(), message='blocked'), True, True]
        menu = MiniAppMenu(bot, 'https://current.example.com')
        await menu.publish([1, 2])
        self.assertEqual(menu.synced, {2})
        await menu.sync_chat(1)
        self.assertEqual(menu.synced, {1, 2})

