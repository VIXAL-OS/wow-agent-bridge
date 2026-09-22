"""Rebuild the font bank and self-test font from scratch.

Needed after a font-format change, or to reclaim slots. Only safe while no
client from this game folder is running: WoW keeps serving the contents of any
font file it has already loaded until the process restarts.

The addon's saved slot counter does not need clearing by hand. The companion
writes a new epoch while the game is closed, and the addon restarts at slot 1.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion.native import BANK_FORMAT, ensure_bank, validate_addon  # noqa: E402
from companion.wow import game_dir_for, game_processes  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='Installed Interface/AddOns/AgentBridge folder')
    parser.add_argument('--force', action='store_true', help='Rebuild even though a client is running')
    args = parser.parse_args()
    addon = validate_addon(args.directory)
    running = game_processes(game_dir_for(addon))
    if running and not args.force:
        parser.error(f'WoW is running (pid {running[0]}). Close it first, or pass --force if you are '
                     'certain no reply font has been loaded this session.')
    marker = addon / '.bankformat'
    if marker.exists():
        marker.unlink()  # force the rebuild path
    print('Rebuilding; this takes a few minutes...')
    report = ensure_bank(addon, progress=lambda n: print(f'  created {n:,} slots...', flush=True))
    print(json.dumps(report, indent=2))
    print(f'Bank format {BANK_FORMAT}. Start WoW with the companion running so the bank recycles.')


if __name__ == '__main__':
    main()
