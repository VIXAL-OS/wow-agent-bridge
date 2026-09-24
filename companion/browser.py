"""Explicit browser clicks from the addon, separate from agent prompts."""
import re
import time
from urllib.parse import urlsplit
import webbrowser

MAX_URL = 2048


def validate_url(url):
    if not isinstance(url, str) or not 1 <= len(url.encode('utf-8')) <= MAX_URL:
        raise ValueError('Source URL is too long or empty')
    if re.search(r'[\s\x00-\x1f\x7f\\<>"|]', url):
        raise ValueError('Source URL contains invalid characters')
    try:
        parts = urlsplit(url)
        if (parts.scheme.lower() not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise ValueError('Only HTTP and HTTPS sources without embedded credentials can be opened')
        parts.port  # Reject malformed ports, rather than handing them to the browser.
    except ValueError as exc:
        raise ValueError('Invalid HTTP or HTTPS source URL') from exc
    return url


class BrowserRequests:
    """Persist before opening: retransmits and companion restarts never replay a click."""

    def __init__(self, db, opener=None):
        self.db = db
        self.opener = opener or (lambda url: webbrowser.open(url, new=2))
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS browser_requests '
                       '(id TEXT PRIMARY KEY, url TEXT, state TEXT, reply TEXT, created REAL)')

    def get(self, key):
        row = self.db.execute('SELECT state,reply FROM browser_requests WHERE id=?', (key,)).fetchone()
        return {'id': key, 'state': row[0], 'reply': row[1]} if row else None

    def accept(self, key, url):
        previous = self.get(key)
        if previous:
            return previous
        try:
            validate_url(url)
        except ValueError:
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO browser_requests VALUES (?,?,?,?,?)',
                                (key, url, 'failed', 'This source is not a valid HTTP or HTTPS URL.', time.time()))
            return self.get(key)
        interrupted = 'Browser request interrupted. Click the source again to retry.'
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO browser_requests VALUES (?,?,?,?,?)',
                                       (key, url, 'interrupted', interrupted, time.time())).rowcount
        if not inserted:
            return self.get(key)
        try:
            opened = self.opener(url)
            state = 'done' if opened else 'failed'
            reply = ('Source sent to your default browser.' if opened else
                     'The default browser could not open this source. Copy its URL to open it manually.')
        except Exception:
            state, reply = 'failed', 'The browser could not be started. Copy the source URL to open it manually.'
        with self.db:
            self.db.execute('UPDATE browser_requests SET state=?, reply=? WHERE id=?', (state, reply, key))
        return self.get(key)
