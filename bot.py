# -*- coding: utf-8 -*-
import os
import time
import json
from html import escape
import asyncio
import logging
import sys
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from aiogram import BaseMiddleware
from core import Store, Portal, Engine, PortalError, AuthExpired, MSK, now, parse_date
from controls import enabled
from login_validation import is_university_email
from request_journal import RequestJournal, JournalMiddleware
from menu_sync import MiniAppMenu
from owner_alerts import OwnerAlerts
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from playwright.async_api import async_playwright

# ================= НАСТРОЙКИ =================
load_dotenv(Path(__file__).with_name('.env'))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MINIAPP_URL = os.environ.get('MINIAPP_URL', '').strip()
USERS = {}

# Глобальные переменные инициализируются в main()
bot = None
login_lock = None
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=MSK, job_defaults={'misfire_grace_time': 60})
store = None
portal = Portal()
engine = None
journal = None
menu_sync = None
owner_alerts = None

# ================= ШИФРОВАНИЕ =================
cipher_suite = None


def encrypt_data(data_str: str) -> str:
    return cipher_suite.encrypt(data_str.encode()).decode()


def decrypt_data(enc_str: str) -> str:
    try:
        return cipher_suite.decrypt(enc_str.encode()).decode()
    except Exception as e:
        print(f"⚠️ Ошибка расшифровки: {e}")
        return ""


def get_decrypted_auth(user_data: dict) -> dict:
    auth = user_data.get("auth")
    if not auth:
        return None
    try:
        return {
            "csrf": decrypt_data(auth["csrf"]),
            "session": decrypt_data(auth["session"])
        }
    except Exception:
        return None


# ================= КЛАВИАТУРЫ =================
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔐 Авторизация"), KeyboardButton(text="🎯 Добавить цель")],
        [KeyboardButton(text="📋 Мой вишлист"), KeyboardButton(text="📅 Мои записи")],
        [KeyboardButton(text="🚀 Искать сейчас"), KeyboardButton(text="⏸ Пауза / Продолжить")],
        [KeyboardButton(text="🛟 Помощь при ошибках")]
    ],
    resize_keyboard=True,
    is_persistent=True
)

if MINIAPP_URL:
    if not MINIAPP_URL.startswith('https://'):
        raise ValueError('MINIAPP_URL должен начинаться с https://')
    main_menu.keyboard.insert(0, [KeyboardButton(text='🏟 Открыть Mini App', web_app=types.WebAppInfo(url=MINIAPP_URL))])

cancel_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True,
    is_persistent=True
)


# ================= БАЗА ДАННЫХ =================
def init_db():
    global USERS, store, cipher_suite
    store = Store()
    USERS = store.users
    cipher_suite = store.cipher


async def save_db_async():
    await store.save()


# ================= АВТОМАТИЧЕСКИЙ ВХОД (PLAYWRIGHT) =================
async def auto_login(username, password):
    if not password or not is_university_email(username):
        return None

    async with login_lock:
        print(f"[{now().strftime('%H:%M')}] Запускаю браузер для авторизации (прямое соединение)...")
        async with async_playwright() as p:
            # headless=True значит браузер не будет мигать на экране
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            try:
                await page.goto("https://sport.innopolis.university/")
                login_btn = page.locator("text=Войти")
                if await login_btn.is_visible():
                    await login_btn.click()

                username_field = page.locator("input[type='email'], input[name='username'], input[name='login']").first
                await username_field.wait_for(state="visible", timeout=15000)
                await username_field.fill(username)

                password_field = page.locator("input[type='password'], input[name='password']").first
                if not await password_field.is_visible():
                    await page.keyboard.press("Enter")
                    await password_field.wait_for(state="visible", timeout=10000)

                await password_field.fill(password)
                await page.keyboard.press("Enter")

                print("Ожидаю загрузки профиля...")
                await page.locator("text=Overall sport hours").first.wait_for(state="visible", timeout=20000)

                cookies = await context.cookies()
                csrf = session = None
                for cookie in cookies:
                    if cookie['name'] == 'csrftoken':
                        csrf = cookie['value']
                    elif cookie['name'] == 'sessionid':
                        session = cookie['value']

                if csrf and session:
                    return {"csrf": csrf, "session": session}
                return None

            except Exception as e:
                print(f"❌ Ошибка Playwright: {e}")
                return None
            finally:
                await browser.close()


# ================= ФУНКЦИИ API (БЕЗ ПРОКСИ) =================
async def fetch_schedule(auth, start_date, end_date):
    return await portal.schedule(auth, start_date, end_date)


async def cancel_slot(auth, training_id):
    return await portal.cancel(auth, training_id)


async def wishlist_scanner():
    server_ops.scan.update(running=True, started=time.time(), ok=None)
    before = engine.scan_failures
    try:
        await engine.scan()
        server_ops.scan['ok'] = engine.scan_failures == before
    except Exception:
        server_ops.scan['ok'] = False
        raise
    finally:
        server_ops.scan.update(running=False, finished=time.time())


# ================= FSM-СОСТОЯНИЯ =================
class AuthState(StatesGroup):
    username = State()
    password = State()


class WishState(StatesGroup):
    sport = State()
    weekdays = State()
    time = State()


@dp.message(Command('help'))
@dp.message(F.text == '🛟 Помощь при ошибках')
async def cmd_help(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        '<b>Помощь при ошибках</b>\n\n'
        '<b>Cloudflare 1016 / Origin DNS error</b>\n'
        'Ссылка могла устареть после перезапуска сервера. Закрой Mini App, '
        'нажми /app и открой приложение через новую кнопку в ответе бота. '
        'Кнопка в старом сообщении не обновляется.\n\n'
        '<b>Страница не открывается, 502 или 1033</b>\n'
        'Подожди минуту и запроси новую ссылку: /app. Если ошибка остаётся, '
        'сообщи владельцу: нужно проверить сервер, интернет и HTTPS-туннель на ноутбуке.\n\n'
        '<b>Сессия истекла / требуется вход</b>\n'
        'Открой приложение заново через /app. Если оно просит войти на спортивный портал, '
        'используй /login — понадобится почта @innopolis.university и пароль.\n\n'
        '<b>Пропали кнопки или бот ждёт ввод</b>\n'
        'Нажми /start, чтобы прервать текущий ввод и вернуть главное меню. '
        'Цели и записи сохранятся.\n\n'
        '<b>Не удаётся записаться</b>\n'
        'Обнови расписание в Mini App: запись открывается за 7 дней до начала, '
        'а свободные места могут закончиться. Если функция отключена модератором, '
        'команда её не включит.\n\n'
        '<b>Мест нет или нужны напоминания</b>\n'
        'Открой /app: кнопка «В ожидание» следит за конкретным занятием. '
        'Во вкладке «Автозапись» можно убрать ожидание или выключить напоминания. '
        'Команда /reminders переключает напоминания за сутки и за час.\n\n'
        '<b>Бот совсем не отвечает</b>\n'
        'Команды не помогут, пока нет соединения с Telegram. '
        'Сообщи владельцу, чтобы он проверил запущенную задачу InnoSport Mini App '
        'и соединение ноутбука с интернетом и VPN.',
        parse_mode='HTML', reply_markup=main_menu
    )


@dp.message(Command('app'))
async def cmd_app(message: types.Message):
    if not MINIAPP_URL:
        return await message.answer('Mini App ещё не подключён. Укажи его HTTPS-адрес в MINIAPP_URL и перезапусти бота.')
    await message.answer('Твоё расписание и автозапись:', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Открыть Inno Sport ↗', web_app=types.WebAppInfo(url=MINIAPP_URL))]
    ]))


@dp.message(Command('moderation'))
async def cmd_moderation(message: types.Message):
    if message.chat.type != "private":
        return await message.answer("Открой личный чат бота для входа в модерацию.")
    from moderator_auth import ModeratorAuth
    auth = ModeratorAuth(store)
    if auth.owner() != str(message.from_user.id):
        return await message.answer('Раздел доступен только владельцу бота.')
    if not MINIAPP_URL:
        return await message.answer('Сервер модерации пока не подключён.')
    connection = 'INNOSPORT|' + MINIAPP_URL.rstrip('/') + '|' + auth.issue(message.from_user.id)
    await message.answer('Скопируй строку ниже в Inno Sport Admin. Код действует 5 минут и используется один раз. Сессия — 30 минут.\n\n' + '<code>' + escape(connection) + '</code>' + '\n\nОтозвать доступ всех устройств: /modrevoke', parse_mode='HTML')


@dp.message(Command('modrevoke'))
async def cmd_modrevoke(message: types.Message):
    from moderator_auth import ModeratorAuth
    auth = ModeratorAuth(store)
    if auth.owner() != str(message.from_user.id):
        return await message.answer('Раздел доступен только владельцу бота.')
    auth.revoke()
    await message.answer('Все сессии модерации и коды входа отозваны.')


# ================= ИНТЕРФЕЙС И КНОПКИ =================
@dp.message(Command('reminders'))
async def cmd_reminders(message: types.Message):
    uid = str(message.from_user.id)
    if uid not in USERS:
        return await message.answer('Сначала авторизуйся: /login')
    USERS[uid]['reminders'] = not USERS[uid].get('reminders', True)
    await save_db_async()
    await message.answer('Напоминания включены: за сутки и за час до занятия.' if USERS[uid]['reminders'] else 'Напоминания выключены. Включить снова: /reminders')


@dp.callback_query(F.data == 'show_bookings')
async def show_bookings(callback: types.CallbackQuery):
    await callback.answer()
    # This handler reads only the account of the person pressing the button.
    message = types.Message(message_id=0, date=now(), chat=types.Chat(id=callback.from_user.id, type='private'))
    await cmd_my_trainings(message.as_(bot))


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Спортивный ассистент Иннополиса готов.\n\n"
        "👇 Используй **кнопки меню** внизу экрана для управления ботом.\n\n"
        "⚠️ _Внимание: Если хочешь отменить запись, делай это через кнопку «📅 Мои записи». "
        "Только так бот поймёт, что тебе туда не нужно, и не забронирует её заново._",
        parse_mode="Markdown",
        reply_markup=main_menu
    )


@dp.message(F.text == "❌ Отмена")
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено. Возвращаю в главное меню 🔙", reply_markup=main_menu)


@dp.message(F.text == "🚀 Искать сейчас")
async def cmd_search_now(message: types.Message):
    if not enabled(sys.modules[__name__], 'auto_booking'):
        return await message.answer('Автоматическая запись временно отключена модератором.')
    msg = await message.answer("⏳ Запускаю ручную проверку расписания...")
    await engine.scan(str(message.chat.id))

    try:
        await msg.delete()
    except Exception:
        pass

    await message.answer("✅ Ручная проверка завершена! Если места были, я их забронировал (или зарядил снайпера).")


@dp.message(Command('login'))
@dp.message(F.text == "🔐 Авторизация")
async def cmd_login(message: types.Message, state: FSMContext):
    if not enabled(sys.modules[__name__], 'new_logins'):
        return await message.answer('Новые подключения временно отключены модератором.')
    await message.answer("Введи университетскую почту: имя@innopolis.university",
                         reply_markup=cancel_menu)
    await state.set_state(AuthState.username)


@dp.message(AuthState.username)
async def process_username(message: types.Message, state: FSMContext):
    username = (message.text or "").strip()
    if not is_university_email(username):
        return await message.answer('Нужна почта с доменом @innopolis.university. Введи адрес ещё раз.', reply_markup=cancel_menu)
    await state.update_data(username=username)
    await message.answer("Введи **пароль**:\n*(Он будет надежно зашифрован перед сохранением)*",
                         parse_mode="Markdown", reply_markup=cancel_menu)
    await state.set_state(AuthState.password)


@dp.message(AuthState.password)
async def process_password(message: types.Message, state: FSMContext):
    if not enabled(sys.modules[__name__], 'new_logins'):
        await state.clear()
        return await message.answer('Новые подключения временно отключены модератором.', reply_markup=main_menu)
    try:
        await message.delete()
    except Exception:
        pass

    password = message.text or ""
    if not password:
        return await message.answer("Отправь пароль текстом.")
    data = await state.get_data()
    username = data.get("username")
    if not is_university_email(username):
        await state.set_state(AuthState.username)
        return await message.answer('Сначала введи почту с доменом @innopolis.university.', reply_markup=cancel_menu)

    msg = await message.answer("⏳ Запускаю виртуальный браузер, пытаюсь войти...",
                               reply_markup=types.ReplyKeyboardRemove())

    tokens = await auto_login(username, password)

    try:
        await msg.delete()
    except Exception:
        pass

    if tokens:
        chat_id = str(message.chat.id)
        if chat_id not in USERS:
            USERS[chat_id] = {"wishlist": [], "cancelled": []}
        elif "cancelled" not in USERS[chat_id]:
            USERS[chat_id]["cancelled"] = []

        USERS[chat_id]["auth"] = {
            "csrf": encrypt_data(tokens["csrf"]),
            "session": encrypt_data(tokens["session"])
        }

        encrypted_pwd = encrypt_data(password)
        USERS[chat_id]["credentials"] = {"username": username, "password": encrypted_pwd}
        await save_db_async()

        await message.answer("✅ Аккаунт привязан! Данные и токены надежно зашифрованы.", reply_markup=main_menu)
    else:
        await message.answer("❌ Ошибка входа. Проверь логин/пароль.", reply_markup=main_menu)
    await state.clear()


@dp.message(F.text == "📅 Мои записи")
async def cmd_my_trainings(message: types.Message):
    chat_id = str(message.chat.id)
    auth = get_decrypted_auth(USERS.get(chat_id, {}))

    if not auth:
        return await message.answer("Сначала привяжи аккаунт через кнопку «🔐 Авторизация»")

    msg = await message.answer("🔍 Ищу твои записи на этой и следующей неделе...")

    today = now()
    monday = today - timedelta(days=today.weekday())
    next_monday = monday + timedelta(days=7)
    next_next_monday = next_monday + timedelta(days=7)

    schedule_current = await fetch_schedule(auth, monday.strftime("%Y-%m-%d"), next_monday.strftime("%Y-%m-%d"))
    schedule_next = await fetch_schedule(auth, next_monday.strftime("%Y-%m-%d"), next_next_monday.strftime("%Y-%m-%d"))

    schedule = schedule_current + schedule_next

    if not schedule:
        return await msg.edit_text(
            "⚠️ Ошибка получения данных. Сервер недоступен или сессия истекла (жди авто-обновления).")

    found = False
    for session in schedule:
        extended = session.get("extendedProps", {})
        if extended.get("checked_in"):
            title = session.get("title", "")
            start_dt = parse_date(session.get("start", ""))

            if start_dt <= now():
                continue

            found = True
            t_id = extended.get("id")
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{t_id}")]
            ])
            await message.answer(f"📅 {title}\n🕒 {start_dt.strftime('%d.%m %H:%M')}", reply_markup=keyboard)

    if not found:
        await msg.edit_text("🤷 Актуальных записей нет.")
    else:
        try:
            await msg.delete()
        except Exception:
            pass


@dp.callback_query(F.data.startswith("cancel_"))
async def process_cancel(callback: types.CallbackQuery):
    training_id = callback.data.split("_")[1]
    chat_id = str(callback.message.chat.id)
    auth = get_decrypted_auth(USERS.get(chat_id, {}))

    if not auth:
        return await callback.answer("Сначала авторизуйся.", show_alert=True)
    # Block a pending sniper before the network call; serialize with in-flight booking.
    key = (chat_id, training_id)
    async with engine.slot_locks.setdefault(key, asyncio.Lock()):
        status = await cancel_slot(await engine.auth(chat_id), training_id)
        if status in (200, 201, 204):
            title = (callback.message.text or '').split('\n')[0].removeprefix('📅 ').strip()
            store.mark_cancelled(chat_id, training_id, title=title or None)
    if status in (200, 201, 204):
        await callback.answer("Запись отменена")
        await callback.message.edit_text(callback.message.text + "\n\n✅ ОТМЕНЕНО")
    else:
        await callback.answer("⚠️ Ошибка отмены!", show_alert=True)


@dp.message(F.text == "📋 Мой вишлист")
async def cmd_wishlist(message: types.Message):
    chat_id = str(message.chat.id)
    wishlist = USERS.get(chat_id, {}).get("wishlist", [])

    if not wishlist:
        return await message.answer("Твой вишлист пуст. Используй кнопку «🎯 Добавить цель»")

    days_map = {0: "ПН", 1: "ВТ", 2: "СР", 3: "ЧТ", 4: "ПТ", 5: "СБ", 6: "ВС"}

    for i, wish in enumerate(wishlist):
        days_text = ", ".join([days_map.get(d, str(d)) for d in wish['weekdays']])
        text = f"🎯 {wish['sport']}\nДни: {days_text}\nВремя: {wish['time_from']}:00 - {wish['time_to']}:00"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delwish_{wish['id']}")]
        ])
        await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("delwish_"))
async def process_delwish(callback: types.CallbackQuery):
    wish_id = callback.data.removeprefix("delwish_")
    chat_id = str(callback.message.chat.id)

    try:
        idx = next((i for i, w in enumerate(USERS.get(chat_id, {}).get("wishlist", [])) if w["id"] == wish_id), None)
        if idx is None:
            return await callback.answer("Цель уже удалена.")
        sport_name = USERS[chat_id]["wishlist"][idx]["sport"]
        del USERS[chat_id]["wishlist"][idx]
        await save_db_async()

        await callback.message.edit_text(f"🗑 Задача на мониторинг «{sport_name}» успешно удалена!")
        await callback.answer("Удалено!")

        await engine.scan(chat_id)
    except IndexError:
        await callback.answer("⚠️ Задача уже удалена.", show_alert=True)
    except Exception as e:
        print(f"Ошибка удаления: {e}")
        await callback.answer("⚠️ Произошла ошибка", show_alert=True)


@dp.message(F.text == "🎯 Добавить цель")
async def cmd_add_wish(message: types.Message, state: FSMContext):
    if not enabled(sys.modules[__name__], 'new_wishes'):
        return await message.answer('Добавление целей временно отключено модератором.')
    if str(message.chat.id) not in USERS or not USERS[str(message.chat.id)].get("auth"):
        return await message.answer("Сначала привяжи аккаунт через кнопку «🔐 Авторизация»")

    await message.answer("Название тренировки? (Например: Volleyball)", reply_markup=cancel_menu)
    await state.set_state(WishState.sport)


@dp.message(WishState.sport)
async def process_wish_sport(message: types.Message, state: FSMContext):
    sport = (message.text or "").strip()
    if not sport or len(sport) > 100:
        return await message.answer("Введи название текстом, до 100 символов.")
    await state.update_data(sport=sport)
    await state.update_data(weekdays=[])
    await message.answer("Выбери дни недели и нажми «Готово»:", reply_markup=days_keyboard([]))
    await state.set_state(WishState.weekdays)


def days_keyboard(selected):
    labels = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    buttons = [InlineKeyboardButton(text=('✓ ' if i in selected else '') + day,
                                    callback_data=f'day_{i}') for i, day in enumerate(labels)]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:4], buttons[4:],
        [InlineKeyboardButton(text='Готово', callback_data='days_done')]])


@dp.callback_query(WishState.weekdays, F.data.startswith('day_'))
async def toggle_day(callback: types.CallbackQuery, state: FSMContext):
    day = int(callback.data.removeprefix('day_'))
    if day not in range(7):
        return await callback.answer()
    data = await state.get_data()
    selected = set(data.get('weekdays', []))
    selected.symmetric_difference_update({day})
    await state.update_data(weekdays=sorted(selected))
    await callback.message.edit_reply_markup(reply_markup=days_keyboard(selected))
    await callback.answer()


@dp.callback_query(WishState.weekdays, F.data == 'days_done')
async def finish_days(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('weekdays'):
        return await callback.answer('Выбери хотя бы один день.', show_alert=True)
    await state.set_state(WishState.time)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer('Укажи часы начала и конца через пробел, например 18 22. Конец окна не включается.', reply_markup=cancel_menu)
    await callback.answer()


@dp.message(WishState.weekdays)
async def process_wish_days(message: types.Message, state: FSMContext):
    try:
        days = sorted(set(int(d.strip()) for d in (message.text or "").split(",")))
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError
        await state.update_data(weekdays=days)
        await message.answer("Окно времени? (Часы через пробел: 18 22)", reply_markup=cancel_menu)
        await state.set_state(WishState.time)
    except ValueError:
        await message.answer("Ошибка формата. Только цифры через запятую.", reply_markup=cancel_menu)


@dp.message(WishState.time)
async def process_wish_time(message: types.Message, state: FSMContext):
    if not enabled(sys.modules[__name__], 'new_wishes'):
        await state.clear()
        return await message.answer('Добавление целей временно отключено модератором.', reply_markup=main_menu)
    try:
        time_from, time_to = map(int, (message.text or "").split())
        if not 0 <= time_from < time_to <= 24:
            raise ValueError
    except ValueError:
        return await message.answer("Введи начало и конец: 0 ≤ начало < конец ≤ 24. Например: 18 22.", reply_markup=cancel_menu)

    data = await state.get_data()
    chat_id = str(message.chat.id)

    USERS[chat_id]["wishlist"].append({
        "id": uuid4().hex,
        "sport": data["sport"],
        "weekdays": data["weekdays"],
        "time_from": time_from,
        "time_to": time_to
    })
    await save_db_async()

    await message.answer(f"✅ Добавлено!", reply_markup=main_menu)
    await state.clear()
    await engine.scan(str(message.chat.id))


# ================= ACCESS / ERRORS / STARTUP =================
class PrivateAccess(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        allowed = {int(x.strip()) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
        if not user or (chat and chat.type != "private") or (allowed and user.id not in allowed):
            return
        if store:
            store.access.touch(user.id, user.username)
            if store.access.blocked(user.id):
                return
            store.remember_telegram_user(user.id, user.username)
        if menu_sync:
            await menu_sync.sync_chat(user.id)
        return await handler(event, data)


dp.update.outer_middleware(JournalMiddleware(lambda: journal))
dp.update.outer_middleware(PrivateAccess())


@dp.errors()
async def handle_error(event):
    if journal:
        journal.finish(event.update.update_id, 'error', type(event.exception).__name__)
    logging.error("Handler failed: %s", type(event.exception).__name__)
    update = event.update
    text = ("🔐 Требуется повторный вход через «Авторизация»." if isinstance(event.exception, AuthExpired)
            else "⚠️ Не удалось выполнить действие. Попробуй ещё раз немного позже.")
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.message:
        await update.message.answer(text, reply_markup=main_menu)
    return True


@dp.message(F.text == "⏸ Пауза / Продолжить")
async def toggle_pause(message: types.Message):
    uid = str(message.chat.id)
    if uid not in USERS:
        return await message.answer("Сначала авторизуйся.")
    USERS[uid]["paused"] = not USERS[uid].get("paused", False)
    await save_db_async()
    paused = USERS[uid]["paused"]
    await message.answer("⏸ Автозапись приостановлена. Цели сохранены." if paused else "▶️ Автозапись включена.")
    if not paused:
        await engine.scan(uid)


async def main():
    global bot, login_lock, engine, journal, menu_sync, owner_alerts, server_ops
    if not BOT_TOKEN or BOT_TOKEN == "replace_with_new_bot_token":
        raise SystemExit("Укажи BOT_TOKEN в файле .env рядом с bot.py")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    journal = RequestJournal(store.directory)
    bot = Bot(token=BOT_TOKEN)
    owner_alerts = OwnerAlerts(bot, store)
    logging.getLogger().addHandler(owner_alerts)
    login_lock = asyncio.Lock()
    engine = Engine(sys.modules[__name__], store, portal)
    from server_operations import ServerOperations
    server_ops = ServerOperations(sys.modules[__name__])
    scheduler.add_job(server_ops.daily, "interval", minutes=15, max_instances=1, coalesce=True)
    scheduler.add_job(wishlist_scanner, 'interval', minutes=3, max_instances=1, coalesce=True)
    scheduler.add_job(engine.features.reminders, 'interval', minutes=1, max_instances=1, coalesce=True)
    scheduler.start()
    web_runner = None
    try:
        await bot.set_my_commands([
            types.BotCommand(command='start', description='Главное меню'),
            types.BotCommand(command='app', description='Свежая ссылка на Mini App'),
            types.BotCommand(command='help', description='Помощь при ошибках'),
            types.BotCommand(command='login', description='Вход на спортивный портал'),
            types.BotCommand(command='reminders', description='Включить или выключить напоминания'),
        ])
        if os.getenv('WEB_ENABLED', '1' if MINIAPP_URL else '0') == '1':
            from miniapp import start_server
            web_runner = await start_server(sys.modules[__name__])
            logging.info('Mini App server started on port %s', os.getenv('WEB_PORT', '8080'))
            if MINIAPP_URL:
                menu_sync = MiniAppMenu(bot, MINIAPP_URL)
                known_chats = set(USERS) | {
                    str(row[0]) for row in journal.db.execute('SELECT telegram_id FROM telegram_users')
                }
                await menu_sync.publish(known_chats)
        await server_ops.daily()
        await engine.enrich_booking_metadata()
        await wishlist_scanner()
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if web_runner:
            await web_runner.cleanup()
        await engine.close()
        logging.getLogger().removeHandler(owner_alerts)
        await owner_alerts.shutdown()
        await bot.session.close()
        store.db.close()
        journal.close()


if __name__ == "__main__":
    asyncio.run(main())
