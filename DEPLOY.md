# Перенос на Linux / NAS с Docker Compose

Для Debian 13 использовать [инструкцию и скрипты Debian](deploy/debian/README.md). Для Ubuntu Server на NAS использовать [пошаговую инструкцию и скрипты Ubuntu](deploy/ubuntu/README.md). В общих командах ниже добавлять `sudo` перед `docker`, если текущий пользователь не имеет доступа к Docker daemon.

Исходники не содержат рабочего токена, баз, ключей и учётных данных. Docker-конфигурация подготовлена для Linux; её сборка на текущем Windows-ноутбуке не проверена, поскольку Docker отсутствует.

## Новый запуск

Установить Git, Docker Engine и Compose plugin. Затем:

```sh
git clone https://github.com/AtomicYou/SportBot.git
cd SportBot
cp .env.example .env
# Вписать BOT_TOKEN и постоянный внешний HTTPS-адрес в MINIAPP_URL.
install -d -m 700 data
sudo chown 10001:10001 data
chmod 600 .env
docker compose build
docker compose run --rm bot python setup_owner.py YOUR_NUMERIC_TELEGRAM_ID
docker compose up -d
docker compose logs -f --tail=100 bot
```

Заменить YOUR_NUMERIC_TELEGRAM_ID своим числовым ID Telegram, не username. Команда закрепляет владельца модерации; без этого файла модерация закрыта. После запуска написать /moderation в личном чате бота и вставить строку в Android-приложение.

Порт 8082 опубликован только на loopback сервера. Подключить к нему HTTPS reverse proxy или туннель и указать его адрес в MINIAPP_URL. Не открывать API напрямую через незащищённый HTTP. Для прокси в другом контейнере нужна отдельная настройка сети: loopback хоста внутри контейнера имеет другой смысл. Универсальный туннель без пользовательского домена в этот комплект не включён.

## Перенос существующих пользователей

Не запускать два экземпляра с одним токеном. Сначала остановить на ноутбуке задачу «InnoSport Mini App» и убедиться, что supervisor и bot.py остановлены. Сделать согласованный SQLite backup обеих баз. Копировать на сервер приватным каналом (SFTP/SSH), а не через GitHub:

- bot.sqlite3 и journal.sqlite3 из одного завершённого набора копирования;
- исходный secret.key — без него сохранённые пароли и сессии не расшифруются;
- moderator-identity.json;
- .env с токеном — отдельно, с новым серверным MINIAPP_URL.

Разместить базы и ключи в data/. Не смешивать снимки с оставшимися от другой версии файлами -wal/-shm. Сохранить текущие файлы отдельно перед восстановлением; удалять старые служебные файлы SQLite можно только при полностью остановленных процессах, после создания корректной резервной копии.

Перед запуском отозвать восстановленные сессии модерации и коды:

```sh
sudo chown -R 10001:10001 data
docker compose run --rm bot python -c "import sqlite3; c=sqlite3.connect('data/bot.sqlite3'); names={r[0] for r in c.execute('SELECT name FROM sqlite_master')}; [c.execute('DELETE FROM '+n) for n in ('moderator_sessions','moderator_pairing') if n in names]; c.commit(); c.close()"
docker compose up -d
```

После запуска проверить: Telegram polling без конфликтов, /app открывает текущий HTTPS-адрес, авторизация портала, расписание, /moderation, автоматические копии. Не проверять перенос массовыми попытками записи. Доступность Telegram без VPN зависит от сети сервера.

## Эксплуатация

`docker compose logs -f bot` — журнал; `docker compose restart bot` — перезапуск; `docker compose down` — остановка. Каталог data сохраняется благодаря bind mount. Автоматические копии находятся в data/backups/automatic. Они на том же носителе: сохранять дополнительную приватную копию на другой диск/NAS.

Обновление: сделать резервную копию, `git pull`, `docker compose up -d --build`. Следить за свободным местом и не публиковать .env, data, дампы и ключи. Windows EXE-монитор читает локальные файлы; для просмотра удалённого сервера его отдельно адаптировать или использовать закрытое подключение. APK обращается к HTTPS API и работает с новым сервером после получения свежей строки /moderation.
