"""Explicit administrator-only provisioning; never guesses an owner by username."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Pin the moderation owner Telegram ID')
    parser.add_argument('telegram_id', type=int)
    args = parser.parse_args()
    if args.telegram_id <= 0:
        parser.error('Telegram ID must be positive')
    directory = Path(__file__).resolve().parent/'data'
    directory.mkdir(exist_ok=True)
    identity = directory/'moderator-identity.json'
    if identity.exists():
        current = json.loads(identity.read_text(encoding='utf-8'))
        if str(current['telegram_id']) != str(args.telegram_id):
            raise SystemExit('Another owner is already pinned. Existing identity was not changed.')
        print('The requested owner is already pinned.')
        return
    with identity.open('x', encoding='utf-8') as stream:
        json.dump({'telegram_id': str(args.telegram_id), 'pinned_at': datetime.now(timezone.utc).isoformat()}, stream, indent=2)
    print('Moderation owner pinned.')


if __name__ == '__main__':
    main()
