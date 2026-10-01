"""Read WoW's serialized SavedVariables as bounded data, never as Lua code.

Only AgentBridgeState's recipe, collection and achievement caches are exposed. Files come from the selected
game's account directory, never from a path supplied by a prompt or snapshot.
"""
from itertools import islice
import math
from pathlib import Path
import re
import time

MAX_FILE = 4 * 1024 * 1024
MAX_SCAN = 16 * 1024 * 1024
MAX_FILES = 64
MAX_VALUES = 200000
MAX_DEPTH = 32
NUMBER = re.compile(rb'-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
COLLECTIONS = frozenset(('mounts', 'pets', 'stablepets'))


def saved_section(section):
    return isinstance(section, str) and (section.startswith('recipes:') or section in COLLECTIONS
                                        or re.fullmatch(r'achievements:[1-8]', section) is not None)


class _Reader:
    def __init__(self, data, *, max_file=None, max_values=None):
        if len(data) > (MAX_FILE if max_file is None else max_file):
            raise ValueError('SavedVariables file too large')
        self.data, self.pos, self.values = data.removeprefix(b'\xef\xbb\xbf'), 0, 0
        self.max_values = MAX_VALUES if max_values is None else max_values

    def skip(self):
        while self.pos < len(self.data):
            if self.data[self.pos] in b' \t\r\n':
                self.pos += 1
            elif self.data.startswith(b'--', self.pos):
                # WoW writes line comments such as -- [1], not Lua long comments.
                if self.data.startswith(b'--[', self.pos):
                    raise ValueError('Unsupported comment')
                end = self.data.find(b'\n', self.pos)
                self.pos = len(self.data) if end < 0 else end + 1
            else:
                break

    def take(self, token):
        self.skip()
        if self.data.startswith(token, self.pos):
            self.pos += len(token)
            return True
        return False

    def need(self, token):
        if not self.take(token):
            raise ValueError('Unexpected SavedVariables syntax')

    def string(self):
        quote = self.data[self.pos]
        self.pos += 1
        out = bytearray()
        escapes = {ord(k): ord(v) for k, v in zip('abfnrtv', '\a\b\f\n\r\t\v')}
        while self.pos < len(self.data):
            c = self.data[self.pos]
            self.pos += 1
            if c == quote:
                return out.decode('utf-8', errors='strict')
            if c in (10, 13):
                raise ValueError('Unescaped newline in string')
            if c != 92:
                out.append(c)
                continue
            if self.pos == len(self.data):
                break
            c = self.data[self.pos]
            self.pos += 1
            if c in escapes:
                out.append(escapes[c])
            elif c in (34, 39, 92):
                out.append(c)
            elif c in (10, 13):
                if c == 13 and self.data[self.pos:self.pos + 1] == b'\n':
                    self.pos += 1
                out.append(10)
            elif 48 <= c <= 57:
                digits = bytes([c])
                while len(digits) < 3 and self.data[self.pos:self.pos + 1].isdigit():
                    digits += self.data[self.pos:self.pos + 1]
                    self.pos += 1
                value = int(digits)
                if value > 255:
                    raise ValueError('Invalid string escape')
                out.append(value)
            else:
                raise ValueError('Unsupported string escape')
        raise ValueError('Unterminated string')

    def value(self, depth=0):
        self.values += 1
        if depth > MAX_DEPTH or self.values > self.max_values:
            raise ValueError('SavedVariables complexity limit')
        self.skip()
        if self.pos == len(self.data):
            raise ValueError('Truncated SavedVariables')
        if self.data[self.pos] in (34, 39):
            return self.string()
        if self.take(b'{'):
            result, index = {}, 1
            while not self.take(b'}'):
                if self.take(b'['):
                    key = self.value(depth + 1)
                    if type(key) not in (str, int):
                        raise ValueError('Unsupported table key')
                    self.need(b']'); self.need(b'=')
                else:
                    key, index = index, index + 1
                if key in result:
                    raise ValueError('Duplicate table key')
                result[key] = self.value(depth + 1)
                if not (self.take(b',') or self.take(b';')):
                    self.need(b'}')
                    break
            return result
        for token, value in ((b'true', True), (b'false', False), (b'nil', None)):
            if self.take(token):
                return value
        number = NUMBER.match(self.data, self.pos)
        if number:
            token = number.group()
            if len(token) > 80:
                raise ValueError('Number too long')
            self.pos = number.end()
            value = float(token) if any(c in token for c in (b'.', b'e', b'E')) else int(token)
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError('Nonfinite number')
            return value
        raise ValueError('Only serialized literals are allowed')


def parse_savedvariables(data):
    reader = _Reader(data)
    reader.need(b'AgentBridgeState'); reader.need(b'=')
    state = reader.value()
    reader.take(b';'); reader.skip()
    if reader.pos != len(reader.data) or not isinstance(state, dict):
        raise ValueError('Expected one complete AgentBridgeState assignment')
    return state


def _snapshots(state):
    result = {}
    for bucket in ('characterRecipes', 'characterCollections', 'characterAchievements'):
        owners = state.get(bucket)
        if not isinstance(owners, dict):
            continue
        for owner, sections in owners.items():
            if not isinstance(owner, str) or not isinstance(sections, dict):
                continue
            for section, doc in sections.items():
                if not saved_section(section) or not isinstance(doc, dict):
                    continue
                expected = ('characterRecipes' if section.startswith('recipes:') else
                            'characterAchievements' if section.startswith('achievements:') else 'characterCollections')
                if bucket != expected:
                    continue
                rev, body = doc.get('revision'), doc.get('body')
                if isinstance(rev, str) and isinstance(body, str):
                    result[owner, section, rev] = body
    return result


class SavedSnapshots:
    def __init__(self, game_dir, clock=time.monotonic):
        self.root = Path(game_dir).resolve() / 'WTF' / 'Account'
        self.clock, self.next_check, self.files = clock, -1e9, {}

    def refresh(self):
        now = self.clock()
        if now < self.next_check:
            return
        self.next_check = now + 2
        updated, remaining = {}, MAX_SCAN
        try:
            root = self.root.resolve()
            paths = list(islice(root.glob('*/SavedVariables/AgentBridge.lua'), MAX_FILES))
        except (OSError, RuntimeError):
            self.files = {}
            return
        for path in paths:
            try:
                path = path.resolve()
                if not path.is_relative_to(root):
                    continue
                before = path.stat()
                if not path.is_file() or before.st_size > min(MAX_FILE, remaining):
                    continue
                remaining -= before.st_size
                signature = (before.st_mtime_ns, before.st_ctime_ns, before.st_size, before.st_ino)
                cached = self.files.get(path)
                if cached and cached[0] == signature:
                    updated[path] = cached
                    continue
                with path.open('rb') as source:
                    data = source.read(MAX_FILE + 1)
                after = path.stat()
                if signature != (after.st_mtime_ns, after.st_ctime_ns, after.st_size, after.st_ino) or len(data) != before.st_size:
                    continue  # Reload is still writing; retry at the next check.
                try:
                    entries = _snapshots(parse_savedvariables(data))
                except (ValueError, RecursionError):
                    entries = {}  # Incomplete/unsupported files fall back to pixels.
                updated[path] = (signature, entries)
            except (OSError, RuntimeError):
                continue
        self.files = updated

    def candidates(self, owner, wanted):
        self.refresh()
        for _, entries in self.files.values():
            for section, rev in wanted:
                body = entries.get((owner, section, rev))
                if body is not None:
                    yield section, rev, body
