"""Same-process HTTP interface. Telegram credentials never leave the backend."""
import asyncio
import hashlib
import hmac
import json
import os
import time
import logging
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl
from uuid import uuid4

from aiohttp import web
from core import AuthExpired, PortalError, now, parse_date
from controls import enabled
from moderation import Moderator
from moderator_auth import ModeratorAuth
from booking_features import window

STATIC = Path(__file__).with_name('web')


def validate_init_data(raw, token, clock=None):
    if not raw or len(raw) > 16384:
        raise ValueError('Открой приложение из Telegram.')
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError('Некорректные данные входа.')
    received = values.pop('hash', '')
    check = '\n'.join(f'{k}={v}' for k, v in sorted(values.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received, expected):
        raise ValueError('Не удалось подтвердить вход через Telegram.')
    age = (time.time() if clock is None else clock) - int(values['auth_date'])
    if age < -30 or age > 3600:
        raise ValueError('Вход устарел. Закрой и открой Mini App заново.')
    user = json.loads(values['user'])
    if not isinstance(user, dict) or type(user.get('id')) is not int or user['id'] <= 0:
        raise ValueError('Некорректный пользователь.')
    return str(user['id'])


def validate_wish(data):
    if not isinstance(data, dict):
        raise ValueError('Некорректная цель.')
    sport, days = data.get('sport'), data.get('weekdays')
    start, end = data.get('time_from'), data.get('time_to')
    if not isinstance(sport, str) or not 1 <= len(sport.strip()) <= 100:
        raise ValueError('Укажи название занятия до 100 символов.')
    if not isinstance(days, list) or not days or any(type(d) is not int or d not in range(7) for d in days):
        raise ValueError('Выбери хотя бы один день недели.')
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= 24:
        raise ValueError('Часы: 0 ≤ начало < конец ≤ 24.')
    return dict(id=uuid4().hex, sport=sport.strip(), weekdays=sorted(set(days)), time_from=start, time_to=end)


def create_app(bot_app=None):
    moderator = Moderator(bot_app) if bot_app is not None and getattr(bot_app.store, 'directory', None) is not None else None
    native_auth = ModeratorAuth(bot_app.store) if moderator else None
    @web.middleware
    async def guard(request, handler):
        try:
            if request.path.startswith('/api/'):
                if bot_app is None:
                    raise web.HTTPServiceUnavailable(text='Демо работает без подключения к аккаунту.')
                raw = request.headers.get('Authorization', '')
                if request.path == '/api/moderator/login' and native_auth:
                    pass
                elif request.path.startswith('/api/admin/'):
                    if not raw:
                        raise web.HTTPUnauthorized(text='Требуется вход в приложение модерации.')
                    if native_auth is None:
                        raise web.HTTPForbidden()
                    request['uid'] = native_auth.validate(raw)
                else:
                    if not raw.startswith('tma '):
                        raise web.HTTPUnauthorized(text='Открой приложение кнопкой в Telegram.')
                    try:
                        uid = validate_init_data(raw[4:], bot_app.BOT_TOKEN)
                    except (ValueError, KeyError, TypeError):
                        raise web.HTTPUnauthorized(text='Вход не подтверждён или устарел. Открой Mini App заново.')
                    allowed = {x.strip() for x in os.getenv('ALLOWED_USER_IDS', '').split(',') if x.strip()}
                    if allowed and uid not in allowed:
                        raise web.HTTPForbidden(text='Доступ к боту ограничен.')
                    access = getattr(bot_app.store, 'access', None)
                    if access:
                        verified = json.loads(dict(parse_qsl(raw[4:]))['user'])
                        access.touch(uid, verified.get('username'))
                        device_blocked = access.observe(uid, request.headers)
                        if access.blocked(uid) or device_blocked:
                            raise web.HTTPForbidden(text='Доступ к боту ограничен владельцем.')
                    if uid not in bot_app.USERS or not bot_app.USERS[uid].get('auth'):
                        raise web.HTTPForbidden(text='Сначала привяжи спортивный аккаунт через «Авторизация» в чате бота.')
                    request['uid'] = uid
                    if hasattr(bot_app.store, 'remember_telegram_user'):
                        verified_user = json.loads(dict(parse_qsl(raw[4:]))['user'])
                        bot_app.store.remember_telegram_user(uid, verified_user.get('username'))
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({'error': exc.text}, status=exc.status)
        except AuthExpired:
            response = web.json_response({'error': 'Требуется повторная авторизация в чате бота.'}, status=401)
        except PortalError:
            logging.warning('Mini App portal request failed')
            response = web.json_response({'error': 'Спортивный портал временно недоступен. Попробуй позже.'}, status=502)
        except (ValueError, TypeError, KeyError):
            response = web.json_response({'error': 'Проверь введённые данные.'}, status=400)
        except Exception as error:
            logging.error('Mini App handler failed: %s', type(error).__name__)
            response = web.json_response({'error': 'Внутренняя ошибка. Попробуй позже.'}, status=500)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'"
        return response

    app = web.Application(middlewares=[guard], client_max_size=16384)
    # Short receipts protect double clicks even if the portal's calendar update
    # arrives slightly later than its successful check-in response.
    booking_receipts = {}
    user_booking_locks = getattr(getattr(bot_app, 'engine', None), 'user_booking_locks', {})

    async def profile(request):
        user = bot_app.USERS[request['uid']]
        return web.json_response({'paused': bool(user.get('paused')), 'wishlist': user.get('wishlist', []), 'connected': True,
            'cancelled_ids': list(user.get('cancelled', [])),
            'reminders': user.get('reminders', True),
            'waitlist': bot_app.store.waiting(request['uid']) if hasattr(bot_app.store, 'waiting') else [],
            'features': {name: enabled(bot_app, name) for name in ('auto_booking', 'manual_booking', 'new_wishes')},
            'support_code': moderator.code(request['uid']) if moderator else None,
            'moderator': False})

    async def load_week(uid, offset):
        monday = now().date() - timedelta(days=now().weekday()) + timedelta(days=offset * 7)
        auth = await bot_app.engine.auth(uid)
        try:
            events = await bot_app.portal.schedule(auth, str(monday), str(monday + timedelta(days=7)))
        except AuthExpired:
            auth = await bot_app.engine.auth(uid, refresh=True)
            events = await bot_app.portal.schedule(auth, str(monday), str(monday + timedelta(days=7)))
        if hasattr(bot_app.store, 'update_booking_titles'):
            bot_app.store.update_booking_titles(uid, events)
        return monday, events

    async def schedule(request):
        offset = int(request.query.get('week', '0'))
        if offset not in (0, 1):
            raise ValueError()
        monday, events = await load_week(request['uid'], offset)
        result = []
        past_bookings = []
        for event in events:
            try:
                extra = event.get('extendedProps', {})
                start = parse_date(event['start'])
                if extra.get('id') is None:
                    continue
                item = {'id': str(extra['id']), 'title': str(event.get('title', 'Занятие')),
                               'start': start.isoformat(), 'checked_in': bool(extra.get('checked_in')),
                               'end': window(event)[1].isoformat(),
                               'can_check_in': bool(extra.get('can_check_in')),
                               'opens_at': (start - timedelta(days=7)).isoformat()}
                if start <= now():
                    if item['checked_in']:
                        past_bookings.append(item)
                else:
                    result.append(item)
            except (ValueError, KeyError, TypeError):
                continue
        return web.json_response({'week_start': str(monday), 'events': sorted(result, key=lambda e: e['start']),
                                  'past_bookings': sorted(past_bookings, key=lambda e: e['start'])})

    async def add_wish(request):
        if not enabled(bot_app, 'new_wishes'):
            raise web.HTTPForbidden(text='Добавление целей временно отключено модератором.')
        wish = validate_wish(await request.json())
        user = bot_app.USERS[request['uid']]
        if len(user.get('wishlist', [])) >= 50:
            raise web.HTTPConflict(text='Достигнут лимит: 50 целей.')
        user.setdefault('wishlist', []).append(wish)
        await bot_app.save_db_async()
        # Save is the API transaction boundary. Scanning is periodic: a slow portal
        # must not turn a saved goal into a failed request and cause duplicate retries.
        return web.json_response(wish, status=201)

    async def remove_wish(request):
        user = bot_app.USERS[request['uid']]
        user['wishlist'] = [w for w in user.get('wishlist', []) if w['id'] != request.match_info['id']]
        await bot_app.save_db_async()
        return web.json_response({'ok': True})

    async def pause(request):
        data = await request.json()
        if not isinstance(data, dict) or type(data.get('paused')) is not bool:
            raise ValueError()
        bot_app.USERS[request['uid']]['paused'] = data['paused']
        await bot_app.save_db_async()
        return web.json_response({'paused': data['paused']})

    async def reminders(request):
        data = await request.json()
        if not isinstance(data, dict) or type(data.get('enabled')) is not bool:
            raise ValueError()
        bot_app.USERS[request['uid']]['reminders'] = data['enabled']
        await bot_app.save_db_async()
        return web.json_response({'enabled': data['enabled']})

    async def waitlist(request):
        uid, tid = request['uid'], request.match_info['id']
        async with bot_app.engine.slot_locks.setdefault((uid, tid), asyncio.Lock()):
            if request.method == 'DELETE':
                bot_app.store.remove_waiting(uid, tid)
                return web.json_response({'ok': True})
            if not enabled(bot_app, 'new_wishes'):
                raise web.HTTPForbidden(text='Добавление целей и ожидания временно отключено модератором.')
            if len(bot_app.store.waiting(uid)) >= 50 and not bot_app.store.is_waiting(uid, tid):
                raise web.HTTPConflict(text='Достигнут лимит: 50 занятий в ожидании.')
            for week in (0, 1):
                _, events = await load_week(uid, week)
                event = next((e for e in events if str(e.get('extendedProps', {}).get('id')) == tid), None)
                if event is not None:
                    if parse_date(event['start']) <= now() or event.get('extendedProps', {}).get('checked_in'):
                        raise web.HTTPConflict(text='Занятие уже началось или ты уже записан.')
                    if not enabled(bot_app, 'new_wishes'):
                        raise web.HTTPForbidden(text='Добавление целей и ожидания временно отключено модератором.')
                    bot_app.store.add_waiting(uid, event)
                    # Keep cancellation history until an actual successful rebooking.
                    return web.json_response({'ok': True})
            raise web.HTTPNotFound(text='Занятие не найдено. Обнови расписание.')

    async def cancel(request):
        uid, tid = request['uid'], request.match_info['id']
        await cancel_training(uid, tid)
        return web.json_response({'ok': True})

    async def cancel_training(uid, tid):
        # Only cancel a future booking belonging to this authenticated account.
        found = False
        title = None
        for week in (0, 1):
            _, events = await load_week(uid, week)
            found |= any(str(e.get('extendedProps', {}).get('id')) == tid
                         and e.get('extendedProps', {}).get('checked_in')
                         and parse_date(e['start']) > now() for e in events)
            for event in events:
                if str(event.get('extendedProps',{}).get('id')) == tid:
                    title = event.get('title')
        if not found:
            raise web.HTTPNotFound(text='Активная запись не найдена. Обнови расписание.')
        key = (uid, tid)
        async with bot_app.engine.slot_locks.setdefault(key, asyncio.Lock()), user_booking_locks.setdefault(uid, asyncio.Lock()):
            status = await bot_app.portal.cancel(await bot_app.engine.auth(uid), tid)
            if status not in (200, 201, 204):
                raise web.HTTPConflict(text='Портал не подтвердил отмену.')
            bot_app.store.mark_cancelled(uid, tid, title=title)
            booking_receipts.pop(key, None)

    async def book(request):
        uid, tid = request['uid'], request.match_info['id']
        key = (uid, tid)
        async with bot_app.engine.slot_locks.setdefault(key, asyncio.Lock()), user_booking_locks.setdefault(uid, asyncio.Lock()):
            if not enabled(bot_app, 'manual_booking'):
                raise web.HTTPForbidden(text='Ручная запись временно отключена модератором.')
            timestamp = time.monotonic()
            for old_key, recorded in list(booking_receipts.items()):
                if timestamp - recorded >= 30:
                    booking_receipts.pop(old_key, None)
            if tid in bot_app.USERS[uid].get('cancelled', []):
                booking_receipts.pop(key, None)
            if key in booking_receipts:
                return web.json_response({'ok': True, 'already_booked': True})
            # A manual check-in does not depend on wishes or the auto-booking pause.
            # Always obtain the target from this user's fresh calendar, not the UI.
            for attempt in range(2):
                target = None
                for week in (0, 1):
                    _, events = await load_week(uid, week)
                    target = next((e for e in events if str(e.get('extendedProps', {}).get('id')) == tid), None)
                    if target is not None:
                        break
                if target is None or parse_date(target['start']) <= now():
                    raise web.HTTPNotFound(text='Будущее занятие не найдено. Обнови расписание.')
                extra = target.get('extendedProps', {})
                if extra.get('checked_in'):
                    if hasattr(bot_app.store, 'remember_interval'):
                        start, end = window(target)
                        bot_app.store.mark_booked(uid, tid, title=target.get('title'))
                        bot_app.store.remember_interval(uid, tid, start, end)
                    return web.json_response({'ok': True, 'already_booked': True})
                if not extra.get('can_check_in'):
                    raise web.HTTPConflict(text='Запись сейчас недоступна: возможно, она ещё не открылась или места закончились. Обнови расписание.')
                feature_service = getattr(bot_app.engine, 'features', None)
                start, end = window(target)
                if feature_service is not None and await feature_service.conflict(uid, tid, start, end):
                    raise web.HTTPConflict(text='Это занятие пересекается с другой твоей записью. Сначала отмени её или выбери другое время.')
                if not enabled(bot_app, 'manual_booking'):
                    raise web.HTTPForbidden(text='Ручная запись временно отключена модератором.')
                try:
                    auth = await bot_app.engine.auth(uid)
                    if not enabled(bot_app, 'manual_booking'):
                        raise web.HTTPForbidden(text='Ручная запись временно отключена модератором.')
                    status = await bot_app.portal.book(auth, tid)
                except AuthExpired:
                    if attempt:
                        raise
                    await bot_app.engine.auth(uid, refresh=True)
                    continue
                if status == 429:
                    raise web.HTTPTooManyRequests(text='Портал просит подождать. Попробуй записаться немного позже.')
                if status not in (200, 201):
                    raise web.HTTPConflict(text='Портал не подтвердил запись. Места могли закончиться. Обнови расписание.')
                booking_receipts[key] = time.monotonic()
                bot_app.store.mark_booked(uid, tid, title=target.get('title'))
                if hasattr(bot_app.store, 'remember_interval'):
                    bot_app.store.remember_interval(uid, tid, start, end)
                cancelled = bot_app.USERS[uid].get('cancelled', [])
                if tid in cancelled:
                    cancelled.remove(tid)
                    await bot_app.save_db_async()
                return web.json_response({'ok': True, 'already_booked': False})

    async def static(request):
        name = request.match_info.get('name', 'index.html')
        if name not in ('index.html', 'app.js', 'style.css'):
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    app.router.add_get('/api/profile', profile)
    app.router.add_get('/api/schedule', schedule)
    app.router.add_post('/api/wishes', add_wish)
    app.router.add_delete('/api/wishes/{id}', remove_wish)
    app.router.add_put('/api/pause', pause)
    app.router.add_put('/api/reminders', reminders)
    app.router.add_post('/api/waitlist/{id}', waitlist)
    app.router.add_delete('/api/waitlist/{id}', waitlist)
    app.router.add_post('/api/bookings/{id}/cancel', cancel)
    app.router.add_post('/api/bookings/{id}/book', book)
    if moderator:
        moderator.mount(app, load_week, cancel_training)
        native_auth.mount(app)
        from server_operations import ServerOperations
        operations = getattr(bot_app, "server_ops", None) or ServerOperations(bot_app)
        operations.mount(app)
    app.router.add_get('/', static)
    app.router.add_get('/{name}', static)
    return app


async def start_server(bot_app):
    runner = web.AppRunner(create_app(bot_app), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, os.getenv('WEB_HOST', '127.0.0.1'), int(os.getenv('WEB_PORT', '8080'))).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner


if __name__ == '__main__':
    # Static preview only; no bot token, database or portal connection required.
    import argparse
    parser = argparse.ArgumentParser(description='Inno Sport demo server (no real accounts)')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    web.run_app(create_app(), host=args.host, port=args.port, access_log=None)
