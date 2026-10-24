"""Exact stored SQLite values, available only through the owner-authenticated API."""
import math
import sqlite3
import time

from aiohttp import web

DATABASES = {'main': 'bot.sqlite3', 'journal': 'journal.sqlite3'}


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def cell(value):
    if isinstance(value, bytes):
        return {'sqlite_type': 'blob', 'hex': value.hex()}
    if isinstance(value, int) and abs(value) > 9007199254740991:
        return {'sqlite_type': 'integer', 'value': str(value)}
    if isinstance(value, float) and not math.isfinite(value):
        return {'sqlite_type': 'real', 'value': str(value)}
    return value


def mount_database_viewer(app, moderator):
    def connect(name):
        if name not in DATABASES:
            raise web.HTTPNotFound(text='База не найдена.')
        path = moderator.backend.store.directory / DATABASES[name]
        if not path.is_file():
            raise web.HTTPNotFound(text='База ещё не создана.')
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        db.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.create_function('viewer_contains', 2, lambda value, query:
            int(value is not None and query in (value.hex() if isinstance(value,bytes) else str(value)).casefold()))
        db.execute('BEGIN')
        return db

    def tables(db):
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}

    async def catalogue(request):
        name = request.match_info['database']
        db = connect(name)
        try:
            entries = [{'name': table, 'total': db.execute('SELECT count(*) FROM '+identifier(table)).fetchone()[0]}
                       for table in sorted(tables(db))]
            return web.json_response({'name': DATABASES[name], 'tables': entries})
        except sqlite3.OperationalError:
            raise web.HTTPBadRequest(text='Не удалось прочитать базу. Попробуй обновить список.')
        finally:
            db.close()

    async def rows(request):
        name, table = request.match_info['database'], request.match_info['table']
        try:
            page = int(request.query.get('page', '1'))
        except ValueError:
            raise web.HTTPBadRequest(text='Некорректная страница.')
        query = request.query.get('q', '').strip().casefold()
        if not 1 <= page <= 2147483647 or len(query) > 100:
            raise web.HTTPBadRequest(text='Некорректные параметры поиска.')
        db = connect(name)
        try:
            if table not in tables(db):
                raise web.HTTPNotFound(text='Таблица не найдена.')
            target = identifier(table)
            columns = [column[0] for column in db.execute('SELECT * FROM '+target+' LIMIT 0').description]
            where = ' WHERE '+' OR '.join('viewer_contains('+identifier(column)+', ?)' for column in columns) if query else ''
            params = [query]*len(columns) if query else []
            total = db.execute('SELECT count(*) FROM '+target+where, params).fetchone()[0]
            # Names are checked against sqlite_master and quoted; search and offsets are bound parameters.
            cursor = db.execute('SELECT * FROM '+target+where+' ORDER BY 1 DESC LIMIT ? OFFSET ?',
                                [*params, 50, (page-1)*50])
            result = [dict(zip(columns, (cell(value) for value in row))) for row in cursor.fetchall()]
            return web.json_response({'columns': columns, 'rows': result, 'total': total,
                                      'page': page, 'page_size': 50, 'limited': False})
        except sqlite3.OperationalError:
            raise web.HTTPBadRequest(text='Не удалось прочитать таблицу. Обнови список или сузь поиск.')
        finally:
            db.close()

    # Existing middleware validates signed Telegram initData and the pinned owner ID.
    app.router.add_get('/api/admin/databases/{database}', catalogue)
    app.router.add_get('/api/admin/databases/{database}/{table}', rows)
