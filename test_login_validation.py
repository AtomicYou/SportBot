import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from login_validation import is_university_email


class ValidationTests(unittest.TestCase):
    def test_only_exact_university_domain(self):
        for value in ('name@innopolis.university', 'Name.Surname@INNOPOLIS.UNIVERSITY'):
            self.assertTrue(is_university_email(value))
        for value in ('idi nahui', '', None, 'x@gmail.com', '@innopolis.university',
                      'x@innopolis.university.evil.com', 'x@innopolis.university@evil.com',
                      'x @innopolis.university', 'x@innopolis.university\n', 'x'*65+'@innopolis.university'):
            self.assertFalse(is_university_email(value), repr(value))


class LoginHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_login_does_not_advance_to_password(self):
        state = AsyncMock()
        message = SimpleNamespace(text='idi nahui', answer=AsyncMock())
        await bot.process_username(message, state)
        state.update_data.assert_not_awaited()
        state.set_state.assert_not_awaited()
        self.assertIn('@innopolis.university', message.answer.call_args.args[0])

    async def test_valid_login_advances_to_password(self):
        state = AsyncMock()
        message = SimpleNamespace(text=' name@innopolis.university ', answer=AsyncMock())
        await bot.process_username(message, state)
        state.update_data.assert_awaited_once_with(username='name@innopolis.university')
        state.set_state.assert_awaited_once_with(bot.AuthState.password)

    async def test_stale_invalid_username_cannot_start_browser(self):
        state = AsyncMock()
        state.get_data.return_value = {'username':'invalid'}
        message = SimpleNamespace(text='test-password', answer=AsyncMock(), delete=AsyncMock())
        with patch.object(bot, 'auto_login', new_callable=AsyncMock) as login:
            await bot.process_password(message, state)
            login.assert_not_awaited()
        state.set_state.assert_awaited_once_with(bot.AuthState.username)

    async def test_auto_login_rejects_invalid_saved_credentials(self):
        with patch.object(bot, 'async_playwright') as browser:
            self.assertIsNone(await bot.auto_login('invalid', 'password'))
            browser.assert_not_called()
