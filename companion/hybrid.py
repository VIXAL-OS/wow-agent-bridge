"""Checked data in a fixed Lua assignment, never interpolated reply source.

Font packets announce a finished payload only after its atomic publication.
The addon offers one unused slot at a time; each is reusable after /reload.
"""
import re
import struct
from pathlib import Path

from .protocol import MAX_TEXT, adler

SLOTS = 16
HEADER = struct.Struct('>4s8sIHBII')
DESCRIPTOR = struct.Struct('>4sHBII')
TEMPLATE = re.compile(rb'AgentBridgeHybridData = "([0-9a-f]*)"\n')


def slot_name(slot):
    if type(slot) is not int or not 1 <= slot <= SLOTS:
        raise ValueError('Invalid hybrid slot')
    return f'AgentBridgeReply{slot:02}'


def slot_path(addon, slot):
    root = Path(addon).resolve().parent
    directory = root / slot_name(slot)
    path = directory / 'Inbox.lua'
    if directory.is_symlink() or directory.resolve().parent != root or path.is_symlink():
        raise ValueError('Hybrid slot must be inside AddOns')
    return path


def payload(session, request, slot, state, encoded):
    slot_name(slot)
    if state not in (4, 5, 6) or not 0 <= len(encoded) <= MAX_TEXT:
        raise ValueError('Hybrid payload must be a bounded final reply')
    checksum = int.from_bytes(adler(bytes([state]) + encoded), 'big')
    header = HEADER.pack(b'ABH1', bytes.fromhex(session), request, slot, state, len(encoded), checksum)
    descriptor = DESCRIPTOR.pack(b'ABH1', slot, state, len(encoded), checksum)
    return descriptor, lua_file(header + encoded)


def lua_file(data):
    source = b'AgentBridgeHybridData = "' + data.hex().encode('ascii') + b'"\n'
    validate_source(source)
    return source


def validate_source(source):
    match = TEMPLATE.fullmatch(source)
    if not match or len(match[1]) % 2 or len(match[1]) > 2 * (HEADER.size + MAX_TEXT):
        raise ValueError('Unsafe hybrid Lua template')
    return bytes.fromhex(match[1].decode('ascii'))


def install_slots(addon):
    """Create missing slots only; never erase an in-flight payload."""
    created = 0
    for slot in range(1, SLOTS + 1):
        path = slot_path(addon, slot)
        path.parent.mkdir(exist_ok=True)
        files = {
            path.parent / (slot_name(slot) + '.toc'): (
                '## Interface: 30300\n## Title: Agent Bridge reply slot %02d\n'
                '## Notes: Optional long-reply data slot.\n## LoadOnDemand: 1\n'
                '## Dependencies: AgentBridge\nInbox.lua\n' % slot).encode('ascii'),
            path: lua_file(b''),
        }
        for target, body in files.items():
            try:
                with target.open('xb') as file:
                    file.write(body)
                created += 1
            except FileExistsError:
                pass
    return created
