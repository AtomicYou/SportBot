"""Validate required values without executing .env or displaying secrets."""
from pathlib import Path
from urllib.parse import urlsplit


def validate(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('\"\'')
    token = values.get('BOT_TOKEN', '')
    if not token or token == 'replace_with_new_bot_token' or ':' not in token:
        raise ValueError('Fill BOT_TOKEN in .env.')
    url = urlsplit(values.get('MINIAPP_URL', ''))
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('Fill MINIAPP_URL with the external HTTPS address.')
    return True


if __name__ == '__main__':
    try:
        validate(Path('.env').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise SystemExit('Invalid .env: set BOT_TOKEN and a valid HTTPS MINIAPP_URL. Values were not printed.')
