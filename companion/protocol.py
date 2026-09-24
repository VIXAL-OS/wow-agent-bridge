"""Wire formats shared with the addon. Fixed-size, checksummed, never executable.

WoW -> companion: 64-byte frames drawn as a 128 x 8 cell strip. Each bit is a
*pair* of adjacent cells, one light and one dark, and the decoder compares the
two. Reading a difference rather than an absolute level means the strip stays
readable at any opacity, so it can be drawn semi-transparently over the UI.
  CPB1  prompt fragment   20-byte header + 40-byte padded chunk + Adler-32
  CPBN  receive control   20-byte header + 20-byte control + padding + Adler-32
Companion -> WoW: 4096-byte packets carried by font glyph advance widths.
  CFN2  reply fragment    32-byte header + 4060-byte padded chunk + Adler-32
Multi-byte integers are big endian.
"""
from dataclasses import dataclass
import math
import struct
import time
import zlib

from PIL import Image

FRAME_SIZE, COLS, ROWS = 64, 128, 8
BITS = FRAME_SIZE * 8
PAIRS_PER_ROW = COLS // 2
MIN_CONTRAST = 10  # median pair difference of 255; below this there is no strip
FRAME_HEADER = struct.Struct('>4sBBBB8sI')  # magic, version, length, part, total, session, request
PROMPT_CHUNK, PROMPT_PARTS = 40, 200  # up to 8,000 bytes: text, links' tooltips, game context
MAX_PROMPT = PROMPT_CHUNK * PROMPT_PARTS
CONTROL = struct.Struct('>IIHHII')  # slot, remaining_ms, part, flags, request, loaded
CONTROL_VERSION = 3
MAX_REMAINING_MS = 30000

REPLY_SIZE = 4096
REPLY_HEADER = struct.Struct('>4sBBH8sIIIHH')  # magic, version, state, length, session, request, slot, revision, part, total
REPLY_CHUNK = REPLY_SIZE - REPLY_HEADER.size - 4
MAX_PARTS = 127
MAX_TEXT = 60000
BANK_SIZE = 65535
STATES = {'waiting': 0, 'queued': 1, 'working': 2, 'streaming': 3, 'done': 4, 'failed': 5, 'interrupted': 6}


def adler(data):
    return struct.pack('>I', zlib.adler32(data))


def encode_prompt(text, session=b'12345678', request=1):
    data = text.encode('utf-8')
    if not 1 <= len(data) <= MAX_PROMPT:
        raise ValueError(f'Prompt must contain 1..{MAX_PROMPT} UTF-8 bytes')
    total = math.ceil(len(data) / PROMPT_CHUNK)
    frames = []
    for part in range(total):
        chunk = data[part * PROMPT_CHUNK:(part + 1) * PROMPT_CHUNK]
        body = FRAME_HEADER.pack(b'CPB1', 1, len(chunk), part, total, session, request)
        body += chunk.ljust(PROMPT_CHUNK, b'\0')
        frames.append(body + adler(body))
    return frames


def _check_frame(frame):
    if len(frame) != FRAME_SIZE or adler(frame[:60]) != frame[60:]:
        raise ValueError('Invalid frame length/checksum')


def parse_prompt(frame):
    _check_frame(frame)
    magic, version, length, part, total, session, request = FRAME_HEADER.unpack(frame[:20])
    if magic != b'CPB1' or version != 1 or not 1 <= total <= PROMPT_PARTS or not 0 <= part < total:
        raise ValueError('Invalid prompt header')
    if not 1 <= length <= PROMPT_CHUNK or (part < total - 1 and length != PROMPT_CHUNK) or request == 0:
        raise ValueError('Invalid prompt payload')
    if any(frame[20 + length:60]):
        raise ValueError('Nonzero padding')
    return f'{session.hex()}:{request}', part, total, frame[20:20 + length]


@dataclass(frozen=True)
class Control:
    session: str
    slot: int
    remaining_ms: int
    part: int
    active: bool
    request: int
    loaded: int

    @property
    def key(self):
        return f'{self.session}:{self.request}'


def encode_control(session=b'12345678', slot=1, remaining_ms=5000, part=1, active=True, request=1):
    body = FRAME_HEADER.pack(b'CPBN', CONTROL_VERSION, CONTROL.size, 0, 1, session, 0)
    body += CONTROL.pack(slot, remaining_ms, part, int(active), request, slot - 1)
    body = body.ljust(60, b'\0')
    frame = body + adler(body)
    parse_control(frame)
    return frame


def parse_control(frame):
    _check_frame(frame)
    magic, version, length, part, total, session, message = FRAME_HEADER.unpack(frame[:20])
    if magic != b'CPBN' or (version, length, part, total, message) != (CONTROL_VERSION, CONTROL.size, 0, 1, 0):
        raise ValueError('Invalid control header')
    if any(frame[20 + CONTROL.size:60]):
        raise ValueError('Invalid control padding')
    slot, remaining, fragment, flags, request, loaded = CONTROL.unpack(frame[20:40])
    if not 1 <= slot <= BANK_SIZE + 1 or loaded != slot - 1:
        raise ValueError('Invalid return slot')
    if not 1 <= fragment <= MAX_PARTS or flags not in (0, 1) or not 0 <= remaining <= MAX_REMAINING_MS:
        raise ValueError('Invalid control fields')
    if flags and (slot > BANK_SIZE or request == 0):
        raise ValueError('Invalid active control')
    return Control(session.hex(), slot, remaining, fragment, bool(flags), request, loaded)


ENVELOPE = '\x01AB1\n'


def parse_envelope(blob):
    """Split a prompt into its header fields and the text to answer.

    The addon sends `\\x01AB1\\n`, then key=value lines, then `\\x02` and the
    body. Repeated keys collect in order (context arrives one line per `ctx=`).
    A prompt without the header is all body, as older addons send it.
    """
    if not blob.startswith(ENVELOPE):
        return {}, blob
    head, marker, body = blob[len(ENVELOPE):].partition('\x02')
    if not marker:
        return {}, blob
    fields = {}
    for line in head.split('\n'):
        key, equals, value = line.partition('=')
        if equals and key:
            fields.setdefault(key, []).append(value)
    return fields, body.lstrip('\n')


def frame_kind(frame):
    return {b'CPB1': 'prompt', b'CPBN': 'control'}.get(bytes(frame[:4]))


def reply_text(snapshot, agent='The agent'):
    """UTF-8 bytes shown in game for a job snapshot, capped at MAX_TEXT."""
    state = STATES.get(snapshot.get('state'), 0)
    text = snapshot.get('reply') or {
        0: 'Waiting for the companion to receive your prompt.',
        1: 'Your prompt is queued.',
        2: f'{agent} is working.',
        3: f'{agent} is writing.',
    }.get(state, 'Finished without response text.')
    encoded = str(text).encode('utf-8')
    if len(encoded) > MAX_TEXT:
        note = b'\n\n[Preview limit reached. The full reply is in the companion.]'
        encoded = encoded[:MAX_TEXT - len(note)].decode('utf-8', errors='ignore').encode('utf-8') + note
    return state, encoded


def make_reply_packet(session, request, slot, part, state, encoded):
    total = max(1, math.ceil(len(encoded) / REPLY_CHUNK))
    if total > MAX_PARTS:
        raise ValueError('Reply has too many fragments')
    part = part if 1 <= part <= total else 1
    payload = encoded[(part - 1) * REPLY_CHUNK:part * REPLY_CHUNK]
    revision = zlib.adler32(bytes([state]) + encoded)
    header = REPLY_HEADER.pack(b'CFN2', 2, state, len(payload), bytes.fromhex(session),
                               request, slot, revision, part, total)
    body = header + payload.ljust(REPLY_CHUNK, b'\0')
    return body + adler(body)


def parse_reply_packet(data):
    """Reference parser mirroring the addon's Lua validation (used by tests)."""
    if len(data) != REPLY_SIZE or adler(data[:-4]) != data[-4:]:
        raise ValueError('Invalid reply checksum')
    magic, version, state, length, session, request, slot, revision, part, total = REPLY_HEADER.unpack(data[:32])
    if magic != b'CFN2' or version != 2 or state > 6 or length > REPLY_CHUNK:
        raise ValueError('Invalid reply header')
    return dict(state=state, session=session.hex(), request=request, slot=slot, revision=revision,
                part=part, total=total, text=data[32:32 + length])


def cell_pair(bit):
    """Cells (row, col) carrying one bit: light then dark means 1."""
    row, pair = divmod(bit, PAIRS_PER_ROW)
    return row, 2 * pair, 2 * pair + 1


def read_image_frame(image):
    """Decode an exact crop of the strip by comparing each cell pair.

    Only the difference within a pair matters, so a translucent strip over any
    background still decodes. Each cell is averaged over its middle third,
    which is what the box resize below produces: that suppresses the blur at
    cell edges without reading nine pixels per cell in Python.

    The contrast test only asks whether a strip is there at all. Individual
    weak pairs keep their sign and are left to the packet checksum, because at
    low opacity a bright background can shrink one pair without corrupting it.
    """
    if image.size[0] < COLS or image.size[1] < ROWS:
        raise ValueError('Crop too small')
    thirds = image.convert('L').resize((COLS * 3, ROWS * 3), Image.BOX)
    middle = thirds.load()
    diffs = []
    for bit in range(BITS):
        row, light, dark = cell_pair(bit)
        y = row * 3 + 1
        diffs.append(middle[light * 3 + 1, y] - middle[dark * 3 + 1, y])
    strength = sorted(abs(d) for d in diffs)[BITS // 2]
    if strength < MIN_CONTRAST:
        raise ValueError('No readable strip here')
    return bytes(sum((diffs[i + j] > 0) << (7 - j) for j in range(8)) for i in range(0, BITS, 8))


def decode_image(image):
    """Return a validated frame from a strip crop, or raise ValueError."""
    frame = read_image_frame(image)
    kind = frame_kind(frame)
    if kind == 'control':
        parse_control(frame)
    elif kind == 'prompt':
        parse_prompt(frame)
    else:
        raise ValueError('Unknown frame')
    return frame


def render_frame(frame, cell=4, alpha=1.0, background=None):
    """Draw a frame the way the addon does (tests and diagnostics)."""
    from PIL import Image, ImageDraw
    size = (COLS * cell, ROWS * cell)
    base = background.resize(size).convert('RGB') if background is not None else Image.new('RGB', size, 'black')
    layer = Image.new('RGB', size)
    draw = ImageDraw.Draw(layer)
    for bit in range(BITS):
        light = bool(frame[bit // 8] & (1 << (7 - bit % 8)))
        row, first, second = cell_pair(bit)
        for col, value in ((first, 255 if light else 0), (second, 0 if light else 255)):
            draw.rectangle((col * cell, row * cell, (col + 1) * cell - 1, (row + 1) * cell - 1), fill=(value,) * 3)
    return Image.blend(base, layer, alpha)


class Assembler:
    """Collect prompt fragments; repeated optical frames are expected and harmless."""

    def __init__(self, clock=time.monotonic):
        self.pending = {}
        self.clock = clock

    def accept(self, frame):
        key, part, total, chunk = parse_prompt(frame)
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if now - v[0] < 180}
        if key not in self.pending:
            if len(self.pending) >= 64:
                self.pending.pop(next(iter(self.pending)))
            self.pending[key] = (now, total, {})
        _, expected, chunks = self.pending[key]
        if total != expected or (part in chunks and chunks[part] != chunk):
            del self.pending[key]
            raise ValueError('Conflicting fragments')
        chunks[part] = chunk
        if len(chunks) == total:
            del self.pending[key]
            return key, b''.join(chunks[i] for i in range(total)).decode('utf-8', errors='strict')
        return None
