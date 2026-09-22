"""Run the real addon Lua (Lua 5.1) against the real companion publisher.

The client model is deliberately pessimistic: a font path is read from disk on
first use and cached for the life of the "process", and every glyph advance is
rounded to whole screen pixels at the configured scale.
"""
from io import BytesIO
import os
from pathlib import Path
import shutil

from fontTools.ttLib import TTFont
from lupa.lua51 import LuaRuntime

from companion.native import BANK_FORMAT, NativeBridge, copy_mono_font, make_font, prepare_bank, selftest_data
from companion.protocol import Assembler, BANK_SIZE, frame_kind, parse_control
from companion.wow import write_epoch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'addon' / 'AgentBridge'


def packet_from_font(source):
    """Read a published font back the way the addon does: two glyphs per byte."""
    from companion.native import GLYPHS, PUA, NIBBLE_STEP
    font = TTFont(source if isinstance(source, (str, Path)) else BytesIO(source))
    cmap, hmtx = font.getBestCmap(), font['hmtx'].metrics
    values = [hmtx[cmap[PUA + i]][0] // NIBBLE_STEP - 2 for i in range(GLYPHS)]
    return bytes(high * 16 + low for high, low in zip(values[::2], values[1::2]))


def install_small(game, slots=96):
    """Addon copy with a small bank (plus the last slot the bridge checks for)."""
    addon = Path(game) / 'Interface' / 'AddOns' / 'AgentBridge'
    addon.mkdir(parents=True, exist_ok=True)
    for path in SOURCE.iterdir():
        if path.suffix.lower() in ('.lua', '.xml', '.toc'):
            shutil.copy2(path, addon / path.name)
    write_epoch(addon / 'Epoch.lua', '1')
    (addon / 'selftest.ttf').write_bytes(make_font(selftest_data()))
    (addon / '.bankformat').write_text(BANK_FORMAT, encoding='ascii')
    copy_mono_font(addon)
    prepare_bank(addon, count=slots)
    last = addon / f'reply{BANK_SIZE:05}.ttf'
    if not last.exists():
        os.link(addon / 'reply00001.ttf', last)
    return addon


class Client:
    """Font loading/measurement as WoW 3.3.5a actually does it, per process.

    Measured in the live client on 2026-09-22: the rasterised em is capped
    (~32 px), so requesting a larger font size changes nothing, every glyph
    advance is rounded to whole pixels, and the last glyph of a string
    contributes its ink width rather than its advance.
    """

    MAX_EM_PX = 32

    def __init__(self, addon, scale=1080 / 768, rounding=True, max_em=MAX_EM_PX):
        self.addon, self.scale, self.rounding, self.max_em = Path(addon), scale, rounding, max_em
        self.cache, self.loads = {}, []

    def load(self, path):
        key = bytes(path).decode().lower()
        if key in self.cache:
            return True
        file = self.addon / key.replace('\\', '/').split('/')[-1]
        if not file.is_file():
            return False
        font = TTFont(BytesIO(file.read_bytes()))
        metrics, glyphs, upm = font['hmtx'].metrics, font['glyf'], font['head'].unitsPerEm
        ink = {}
        for name in metrics:
            glyph = glyphs[name]
            ink[name] = glyph.xMax if getattr(glyph, 'numberOfContours', 0) else 0
        by_code = {cp: (metrics[name][0], ink[name]) for cp, name in font.getBestCmap().items()}
        self.cache[key] = (by_code, upm)
        self.loads.append(file.name)
        return True

    def measure(self, path, size, text):
        metrics, upm = self.cache[bytes(path).decode().lower()]
        em = min(size * self.scale, self.max_em) if self.max_em else size * self.scale
        characters = bytes(text).decode('utf-8')
        px = 0.0
        for index, ch in enumerate(characters):
            advance, ink = metrics.get(ord(ch), (512, 64))
            width = (ink if index == len(characters) - 1 else advance) * em / upm
            px += round(width) if self.rounding else width
        return px / self.scale


class Sim:
    FRAME, CAPTURE = 1 / 60, 0.07

    def __init__(self, game, agent, saved=None, scale=1080 / 768, rounding=True, slots=96):
        self.addon = install_small(game, slots)
        self.t, self.agent = 1000.0, agent
        self.client = Client(self.addon, scale, rounding)
        self.native = NativeBridge(self.addon, clock=lambda: self.t)
        self.assembler = Assembler(clock=lambda: self.t)
        self.jobs, self.writes, self.companion_on = {}, 0, True
        self.boot(saved)

    def boot(self, saved=None):
        """A UI load: fresh Lua state, same client process (font cache kept)."""
        self.lua = LuaRuntime(encoding=None, unpack_returned_tuples=True)
        bridge = self.lua.table_from({b'now': lambda: self.t, b'load': self.client.load,
                                      b'measure': self.client.measure})
        self.lua.execute((ROOT / 'tests' / 'wowstub.lua').read_bytes(), bridge)
        if saved is not None:
            self.lua.globals().AgentBridgeState = self.to_lua(saved)
        toc = (self.addon / 'AgentBridge.toc').read_text().splitlines()
        for name in [line.strip() for line in toc if line.strip() and not line.startswith('#')]:
            self.lua.execute((self.addon / name).read_bytes())
        self.g = self.lua.globals()
        self.g.STUB.fire(b'ADDON_LOADED', b'AgentBridge')
        self.g.STUB.fire(b'PLAYER_ENTERING_WORLD')
        self.ns = self.g.AgentBridge

    def to_lua(self, value):
        if isinstance(value, dict):
            return self.lua.table_from({k.encode() if isinstance(k, str) else k: self.to_lua(v) for k, v in value.items()})
        return value.encode() if isinstance(value, str) else value

    def saved(self):
        """SavedVariables as the client would write and restore them."""
        def convert(value):
            if hasattr(value, 'items'):
                return {(k.decode() if isinstance(k, bytes) else k): convert(v) for k, v in value.items()}
            return value.decode() if isinstance(value, bytes) else value
        return convert(self.g.AgentBridgeState)

    def reload(self):
        self.boot(self.saved())

    def send(self, text):
        box = self.g.AgentBridgeInput
        box.text = text.encode()
        box.scripts[b'OnEnterPressed'](box)

    def snapshot(self, key):
        job = self.jobs.get(key)
        if not job:
            return {'id': key, 'state': 'waiting', 'reply': ''}
        state, reply = self.agent(job['prompt'], self.t - job['start'])
        return {'id': key, 'state': state, 'reply': reply}

    def capture(self):
        data = self.g.STUB.strip()
        if not data or not self.companion_on:
            return
        data = bytes(data)
        if frame_kind(data) == 'control':
            if self.native.accept(parse_control(data), self.snapshot(parse_control(data).key)):
                self.writes += 1
        elif frame_kind(data) == 'prompt':
            result = self.assembler.accept(data)
            if result and result[0] not in self.jobs:
                self.jobs[result[0]] = {'prompt': result[1], 'start': self.t}

    def run(self, seconds, until=None):
        end, next_capture = self.t + seconds, self.t
        while self.t < end:
            self.t += self.FRAME
            self.g.STUB.update(self.FRAME)
            if self.t >= next_capture:
                self.capture()
                next_capture = self.t + self.CAPTURE
            if until and until():
                return True
        return False

    # Views -------------------------------------------------------------
    def body(self):
        return b'\n'.join(bytes(line) for line in self.g.AgentBridgeBody.lines.values()).decode('utf-8')

    def last_reply(self):
        last = self.g.AgentBridgeState.last
        return bytes(last.reply).decode('utf-8') if last else None

    def status(self):
        return self.ns.ReceiverInfo()
