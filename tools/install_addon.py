"""Install or update the addon and create any missing font-bank slots.

Safe to rerun, including while WoW is open: existing slots, the self-test font
and the epoch file are never overwritten. New Lua takes effect after /reload;
a brand-new install needs a full client restart so WoW lists the addon.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion.native import ADDON_NAME, copy_mono_font, ensure_bank, make_font, selftest_data  # noqa: E402
from companion.hybrid import install_slots  # noqa: E402
from companion.wow import game_dir_for, game_processes, write_epoch  # noqa: E402

SOURCE = ROOT / 'addon' / ADDON_NAME


def install(destination, progress=None, count=None, settings_path=ROOT / 'state' / 'settings.json'):
    destination = Path(destination).resolve()
    if destination.name != ADDON_NAME or destination.parent.name.lower() != 'addons':
        raise ValueError(f'Destination must be <WoW>/Interface/AddOns/{ADDON_NAME}')
    destination.mkdir(parents=True, exist_ok=True)
    for path in SOURCE.iterdir():
        if path.is_file() and path.suffix.lower() in ('.lua', '.xml', '.toc'):
            shutil.copy2(path, destination / path.name)
    if not (destination / 'Epoch.lua').exists():
        write_epoch(destination / 'Epoch.lua', '1')
    copy_mono_font(destination)
    selftest = destination / 'selftest.ttf'
    if not selftest.exists():
        with selftest.open('xb') as file:
            file.write(make_font(selftest_data(), 0))
    running = bool(game_processes(game_dir_for(destination)))
    report = ensure_bank(destination, **({'count': count} if count else {}), progress=progress, running=running)
    report['hybrid_files_created'] = install_slots(destination)
    settings_path = Path(settings_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings = json.loads(settings_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        settings = {}
    settings['addon'] = str(destination)
    settings_path.write_text(json.dumps(settings, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help=f'Full path ending in Interface/AddOns/{ADDON_NAME}')
    args = parser.parse_args()
    report = install(args.directory, lambda n: print(f'  created {n:,} slots...', flush=True))
    print(json.dumps(report, indent=2))
    print('Installed. Restart WoW if this is the first install (or /reload after an update).')


if __name__ == '__main__':
    main()
