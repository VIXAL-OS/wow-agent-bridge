"""Enable shared tools/native memory for bridge-launched Claude and Codex runs.

Requires a configured Hermes bridge profile and the Hermes MCP optional extra.
Installs one reusable skill for each CLI; does not modify their global settings.
"""
import argparse
import json
from pathlib import Path
import shutil


def configure(home, user_home):
    path = home / 'bridge-tools.json'
    flags = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(flags, dict) or any(type(value) is not bool for value in flags.values()):
        raise ValueError('Run the Hermes bridge setup first')
    source = Path(__file__).resolve().parents[1] / 'skills' / 'agent-bridge' / 'SKILL.md'
    if not source.is_file():
        raise ValueError('Agent Bridge skill is missing')
    for folder in ('.claude', '.agents'):
        target = user_home / folder / 'skills' / 'agent-bridge' / 'SKILL.md'
        if target.exists():
            print('Preserved existing skill: ' + str(target))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            print('Installed skill: ' + str(target))
    backup = home / 'bridge-tools.before-coding-extras.json'
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    flags['coding_agents'] = True
    path.write_text(json.dumps(flags, indent=2), encoding='utf-8')
    print('Enabled shared tools and native memory for bridge-launched Claude Code and Codex.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / '.hermes' / 'agentbridge')
    parser.add_argument('--user-home', type=Path, default=Path.home())
    args = parser.parse_args()
    configure(args.home.resolve(), args.user_home.resolve())
