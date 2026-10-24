"""Persistent switches shared by Telegram handlers, the scanner and HTTP API."""
import json

DEFAULT_FEATURES = {'auto_booking': True, 'manual_booking': True, 'new_wishes': True, 'new_logins': True}


class FeaturePolicy:
    def __init__(self, db):
        self.db = db
        db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        row = db.execute("SELECT value FROM settings WHERE key='features'").fetchone()
        self.flags = dict(DEFAULT_FEATURES)
        if row:
            self.flags.update({key: value for key, value in json.loads(row[0]).items()
                               if key in DEFAULT_FEATURES and type(value) is bool})
        db.commit()

    def update(self, flags):
        if not isinstance(flags, dict) or set(flags) != set(DEFAULT_FEATURES) or any(type(v) is not bool for v in flags.values()):
            raise ValueError('Invalid feature switches')
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('features', ?)", (json.dumps(flags),))
        self.flags = dict(flags)


def enabled(backend, name):
    policy = getattr(getattr(backend, 'store', None), 'features', None)
    return policy.flags.get(name, True) if policy is not None else True
