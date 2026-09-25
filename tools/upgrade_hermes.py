"""Enable the bridge's project tools, memory, skills and optional local tools.

Run using Hermes's Python. This edits only the selected bridge profile. Install
agent-browser in <home>/runtime and edge-tts in the Hermes venv separately.
"""
import argparse
import json
from pathlib import Path


def upgrade(home, browser_executable=None, speech=False, vision=False):
    home = Path(home)
    if browser_executable and not Path(browser_executable).is_file():
        raise ValueError('Browser executable not found')
    flags_path = home / 'bridge-tools.json'
    flags = json.loads(flags_path.read_text(encoding='utf-8')) if flags_path.exists() else {}
    if not isinstance(flags, dict) or any(type(value) is not bool for value in flags.values()):
        raise ValueError('bridge-tools.json must map feature names to true/false')
    from ruamel.yaml import YAML
    yaml = YAML()
    path = home / 'config.yaml'
    with path.open(encoding='utf-8') as handle:
        data = yaml.load(handle)
    if not isinstance(data, dict):
        raise ValueError('Configure the Hermes bridge profile first')
    backup = home / 'config.before-project-tools.yaml'
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    data.setdefault('terminal', {}).update(backend='local', cwd='.', timeout=180)
    data.setdefault('memory', {}).update(memory_enabled=True, user_profile_enabled=True)
    data.setdefault('agent', {}).update(max_turns=40)
    data.setdefault('delegation', {}).update(max_concurrent_children=2, oneshot_max_children=2,
                                            max_spawn_depth=1, child_timeout_seconds=180,
                                            orchestrator_enabled=False, subagent_auto_approve=False)
    aux = data.setdefault('auxiliary', {})
    aux['title_generation'] = {'enabled': False}
    aux['background_review'] = {'enabled': False}
    if vision:
        # Use the existing credentialed Gemini route for text-only main models.
        aux['vision'] = {'provider': 'bridge-gemini', 'model': 'gemini-3.1-pro-preview',
                         'reasoning_effort': 'low', 'timeout': 90}
    if speech:
        data.setdefault('tts', {}).update(provider='edge')
    if browser_executable:
        data.setdefault('browser', {}).update(backend='local', inactivity_timeout=120,
                                              headed=False, use_real_profile=False, cdp_url='')
    # Imported profiles start as JSON (flow-style YAML). Emit ordinary block
    # mappings so provider URLs parse consistently in both YAML 1.1 and 1.2.
    def block_style(value):
        if hasattr(value, 'fa'):
            value.fa.set_block_style()
        if isinstance(value, dict):
            for child in value.values():
                block_style(child)
        elif isinstance(value, list):
            for child in value:
                block_style(child)
    block_style(data)
    with path.open('w', encoding='utf-8') as handle:
        yaml.dump(data, handle)
    flags.update(browser=bool(browser_executable), vision=vision, speech=speech)
    flags_path.write_text(json.dumps(flags, indent=2), encoding='utf-8')
    if browser_executable:
        (home / 'bridge-runtime.json').write_text(json.dumps({
            'browser_executable': str(Path(browser_executable).resolve()),
        }, indent=2), encoding='utf-8')
    # A small bridge-specific skill gives the agent a reusable entry point; users
    # can add more skills through skill_manage without importing unrelated hooks.
    skill = home / 'skills' / 'agent-bridge'
    skill.mkdir(parents=True, exist_ok=True)
    target = skill / 'SKILL.md'
    if not target.exists():
        target.write_text('''---
name: agent-bridge
description: Work on the Agent Bridge WoW addon and companion while the user plays.
---
## When to Use
Use when changing the Agent Bridge addon or companion during a game session.

Read the selected project's AGENTS.md before editing. Use the active work folder.
Run relevant tests and report concrete results. Never synthesize game input, read
WoW memory, restart the game, or rewrite font-bank slots while WoW is running.
Only the user runs /reload. Keep replies compact and use verified item/spell IDs.
Use memory for durable preferences and skill_manage for reusable procedures;
only say these were saved after the tool confirms success.
''', encoding='utf-8')
    print('Enabled project tools, persistent memory, skills and bounded delegation.')
    print('Optional tools: ' + ', '.join(k for k, v in {
        'browser': bool(browser_executable), 'vision': vision, 'speech': speech}.items() if v))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / '.hermes' / 'agentbridge')
    parser.add_argument('--browser-executable', type=Path)
    parser.add_argument('--speech', action='store_true')
    parser.add_argument('--vision', action='store_true')
    args = parser.parse_args()
    upgrade(args.home, args.browser_executable, args.speech, args.vision)
