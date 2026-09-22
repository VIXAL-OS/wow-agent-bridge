"""Reply bytes -> glyph advance widths in first-use addon fonts.

Each byte uses two glyphs, one per 4-bit nibble: glyph U+E000 + i carries
nibble i with advance (2 + value) * 256 font units at 1024 units/em, so one
step is an eighth of an em.

That step matters. WoW 3.3.5a caps the rasterised em (~32 px measured), and a
larger font size does not change measured widths, so resolution has to come
from the font: a step of em/8 is ~4 px where a byte-per-glyph scale (em/64)
would be ~0.5 px and collide. No hinting programs, no executable content; the
addon never reads these files as raw bytes.
"""
from io import BytesIO
import os
from pathlib import Path
import shutil
import tempfile
import time

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from .protocol import BANK_SIZE, REPLY_SIZE, make_reply_packet, reply_text

ADDON_NAME = 'AgentBridge'
UNITS_PER_EM = 1024
PUA = 0xE000
SEED_LINKS = 512  # NTFS allows 1024 hard links per file; stay well below.
NIBBLE_STEP = UNITS_PER_EM // 8
GLYPHS = REPLY_SIZE * 2
VALUES = 16


def advance(value):
    return (2 + value) * NIBBLE_STEP


def nibbles(data):
    for byte in data:
        yield byte >> 4
        yield byte & 15


def _box(width, height):
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0)); pen.lineTo((width, 0)); pen.lineTo((width, height)); pen.lineTo((0, height))
    pen.closePath()
    return pen.glyph()


_TEMPLATE = {}


def make_font(data, tag=0):
    """Build a TrueType font whose data glyph advances spell out `data`."""
    if len(data) != REPLY_SIZE:
        raise ValueError('Reply packets are exactly %d bytes' % REPLY_SIZE)
    # A conventional printable Latin repertoire keeps the loader happy; '!' and
    # '"' are the 0/15 calibration glyphs and '~' is the common trailing glyph.
    latin = {c: 8 for c in range(33, 127)}
    latin.update({33: 0, 34: 15, 126: 8})
    order = ['.notdef', 'space'] + [f'g{c}' for c in latin] + [f'd{i}' for i in range(GLYPHS)]
    cmap = {32: 'space', **{c: f'g{c}' for c in latin}, **{PUA + i: f'd{i}' for i in range(GLYPHS)}}
    if 'glyphs' not in _TEMPLATE:
        glyphs = {'.notdef': _box(64, 512), 'space': TTGlyphPen(None).glyph()}
        glyphs.update({f'g{c}': _box(64, 512) for c in latin})
        # Tiny data outlines keep rasterisation cheap; only the advance matters.
        glyphs.update({f'd{i}': _box(32, 32) for i in range(GLYPHS)})
        _TEMPLATE['glyphs'] = glyphs
    metrics = {'.notdef': (512, 0), 'space': (256, 0)}
    metrics.update({f'g{c}': (advance(v), 0) for c, v in latin.items()})
    metrics.update({f'd{i}': (advance(v), 0) for i, v in enumerate(nibbles(data))})
    fb = FontBuilder(UNITS_PER_EM, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(dict(_TEMPLATE['glyphs']))
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=800, descent=-224)
    family = f'AgentBridgeReply{tag}'
    fb.setupNameTable({'familyName': family, 'styleName': 'Regular', 'uniqueFontIdentifier': family,
                       'fullName': family, 'psName': family, 'version': 'Version 1.0'})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-224, usWinAscent=800, usWinDescent=224)
    fb.setupPost()
    fb.setupMaxp()
    output = BytesIO()
    fb.save(output)
    return output.getvalue()


def selftest_byte(index):
    """Mirrored in Receiver.lua. The first eight bytes hold nibbles 0..15 in
    order, so the addon can read one width per value; the rest check decoding."""
    if index < 8:
        return 34 * index + 1  # (2i << 4) | (2i + 1)
    return (37 * index + 11) % 256


def selftest_data():
    return bytes(selftest_byte(i) for i in range(REPLY_SIZE))


MONO_FONTS = ('consola.ttf', 'cour.ttf', 'lucon.ttf')


def copy_mono_font(destination):
    """Copy a fixed-width font from this machine so the panel can align tables.

    A local copy of a font already installed here; nothing is redistributed.
    """
    target = Path(destination) / 'mono.ttf'
    if target.exists():
        return target
    fonts = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
    for name in MONO_FONTS:
        if (fonts / name).is_file():
            shutil.copy2(fonts / name, target)
            return target
    return None


def validate_addon(directory):
    directory = Path(directory).resolve()
    if directory.name != ADDON_NAME or not (directory / f'{ADDON_NAME}.toc').is_file():
        raise ValueError(f'Select the installed {ADDON_NAME} addon folder')
    return directory


def font_path(directory, slot):
    if not 1 <= slot <= BANK_SIZE:
        raise ValueError('Font bank exhausted')
    path = directory / f'reply{slot:05}.ttf'
    if path.resolve().parent != directory.resolve():
        raise ValueError('Unexpected font path')
    return path


def atomic_write(path, body):
    """Replace a path by rename so hard-linked siblings are never written through."""
    fd, name = tempfile.mkstemp(prefix='.next-', suffix=path.suffix, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(body)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


BANK_FORMAT = f'nibble-{REPLY_SIZE}-v3'


def bank_format(directory):
    try:
        return (Path(directory) / '.bankformat').read_text(encoding='ascii').strip()
    except OSError:
        return ''


def rebuild_bank(directory, progress=None):
    """Reset every existing slot to a blank placeholder, keeping the names.

    Slots share inodes through hard links, so one write per inode resets up to
    SEED_LINKS names at once. Writing through a shared link is exactly what a
    wholesale rebuild wants, and the opposite of what publishing one reply may
    do -- which is why this runs only while the game is closed.
    """
    baseline, seen, written = make_font(bytes(REPLY_SIZE)), set(), 0
    with os.scandir(directory) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        path = directory / name
        if name.startswith(('.seed-', '.next-')):
            path.unlink(missing_ok=True)
            continue
        if not (name.startswith('reply') and name.endswith('.ttf')):
            continue
        inode = os.stat(path).st_ino
        if inode in seen:
            continue
        seen.add(inode)
        with open(path, 'wb') as file:
            file.write(baseline)
        written += 1
        if progress and written % 32 == 0:
            progress(written)
    return written


def ensure_bank(directory, count=BANK_SIZE, progress=None, running=None):
    """Fill in missing slots, or rebuild everything if the font format changed.

    A rebuild is only safe while no client is running: the game keeps serving
    the contents of any font file it has already loaded until it restarts.
    """
    directory = validate_addon(directory)
    if bank_format(directory) != BANK_FORMAT:
        if running:
            raise RuntimeError('The font bank is in an older format and must be rebuilt, which is only '
                               'safe while WoW is closed. Close the game, then run tools.reset_bank.')
        atomic_write(directory / 'selftest.ttf', make_font(selftest_data(), 0))
        rewritten = rebuild_bank(directory, progress)
        (directory / '.bankformat').write_text(BANK_FORMAT, encoding='ascii')
        report = prepare_bank(directory, count, progress)
        report['rebuilt'] = rewritten
        return report
    return prepare_bank(directory, count, progress)


def prepare_bank(directory, count=BANK_SIZE, progress=None):
    """Create missing zero-packet slots, sharing one file per SEED_LINKS names.

    Existing slots are never touched, so this is safe to rerun and to run while
    the game is open. Writers must publish with atomic_write, never in place.
    """
    directory = validate_addon(directory)
    if type(count) is not int or not 1 <= count <= BANK_SIZE:
        raise ValueError('Invalid font bank size')
    started = time.monotonic()
    existing = {entry.name for entry in os.scandir(directory) if entry.name.startswith('reply')}
    baseline, seed, links, created, groups = None, None, 0, 0, 0
    try:
        for slot in range(1, count + 1):
            if f'reply{slot:05}.ttf' in existing:
                continue
            path = font_path(directory, slot)
            if seed is None or links == SEED_LINKS:
                if seed is not None:
                    seed.unlink()
                baseline = baseline or make_font(bytes(REPLY_SIZE))
                fd, name = tempfile.mkstemp(prefix='.seed-', suffix='.ttf', dir=directory)
                with os.fdopen(fd, 'wb') as file:
                    file.write(baseline)
                seed, links, groups = Path(name), 0, groups + 1
            try:
                os.link(seed, path)
            except FileExistsError:
                continue
            except OSError as exc:
                raise RuntimeError('The font bank needs hard-link support (NTFS). '
                                   'Existing slots are preserved; rerun to resume.') from exc
            links, created = links + 1, created + 1
            if progress and created % 4096 == 0:
                progress(created)
    finally:
        if seed is not None:
            seed.unlink(missing_ok=True)
    return {'slots': count, 'created': created, 'preserved': count - created, 'seed_files': groups,
            'seconds': round(time.monotonic() - started, 2)}


class NativeBridge:
    """Write the packet the addon asked for into the slot it will load next.

    A slot may be rewritten with newer content until its advertised deadline
    (minus a safety margin); after that the addon may already be loading it.
    Multi-part transfers are served from a frozen copy of the text whose first
    part was delivered, so every fragment of an assembly shares one revision.
    """

    MARGIN = 0.75
    REWRITE_EVERY = 1.0

    def __init__(self, directory, agent_label=lambda: 'The agent', clock=time.monotonic):
        self.directory = validate_addon(directory)
        self.agent_label = agent_label
        self.clock = clock
        for slot in (1, BANK_SIZE):
            if not font_path(self.directory, slot).is_file():
                raise ValueError('Install the font bank first (tools.install_addon)')
        self.session = None
        self.frozen = {}
        self._reset()

    def _reset(self):
        self.slot, self.deadline, self.was_active = 0, 0, False
        self.written, self.written_at, self.frozen = None, 0, {}

    def accept(self, control, snapshot):
        if self.session != control.session:
            self.session = control.session
            self._reset()
        if control.slot < self.slot:
            return False
        now = self.clock()
        deadline = now + control.remaining_ms / 1000 - self.MARGIN
        if control.slot != self.slot or (control.active and not self.was_active):
            self.deadline = deadline
        else:
            # A repeated/frozen capture must never extend an old deadline.
            self.deadline = min(self.deadline, deadline)
        self.slot, self.was_active = control.slot, control.active
        if not control.active or control.remaining_ms < 1000 or now >= self.deadline:
            return False
        key = control.key
        if snapshot.get('id') != key:
            snapshot = {'id': key, 'state': 'waiting', 'reply': ''}
        if control.part == 1 or key not in self.frozen:
            content = reply_text(snapshot, self.agent_label())
        else:
            content = self.frozen[key]
        state, encoded = content
        packet = make_reply_packet(control.session, control.request, control.slot, control.part, state, encoded)
        identity = (control.session, control.slot, packet)
        if identity == self.written:
            return False
        if self.written and self.written[:2] == identity[:2] and now - self.written_at < self.REWRITE_EVERY:
            return False
        body = make_font(packet, control.slot)
        if self.clock() >= self.deadline:
            return False
        path = font_path(self.directory, control.slot)
        if not path.is_file():
            raise ValueError('Font slot missing; reinstall the bank')
        atomic_write(path, body)
        self.written, self.written_at = identity, self.clock()
        # Later fragments must come from exactly the text whose part 1 was written.
        self.frozen[key] = content
        return True
