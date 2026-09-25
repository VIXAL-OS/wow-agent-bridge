"""Import Hydra's enabled model routes into a separate local Hermes bridge profile.

Run with Hermes's venv Python (provides python-dotenv). Reads bot.py as AST data;
never imports the Discord bot. Copies only the selected model keys and Tavily key.
Refuses to overwrite an existing profile, which may contain custom settings.
"""
import argparse
import ast
import json
from pathlib import Path


def hydra_profiles(root, credentials):
    tree = ast.parse((root / 'bot.py').read_text(encoding='utf-8-sig'))
    overrides = json.loads((root / 'config.json').read_text(encoding='utf-8-sig')).get('providers', {})
    routes, keys = {}, set()
    for node in tree.body:
        call = node.value if isinstance(node, ast.Assign) else None
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != 'ModelProvider':
            continue
        fields = {}
        for keyword in call.keywords:
            try:
                fields[keyword.arg] = ast.literal_eval(keyword.value)
            except (ValueError, TypeError):
                pass
        alias = fields.get('id')
        if not alias or fields.get('completions_mode') or fields.get('enabled') is False:
            continue
        settings = overrides.get(alias, {})
        if settings.get('enabled') is False:
            continue
        backend = settings.get('backend', fields.get('backend'))
        route = dict(fields)
        backend_values = dict(fields.get('backends', {}).get(backend, {}))
        backend_values.update(settings.get(backend, {}) if backend else {})
        route.update(backend_values)
        for key in ('model', 'base_url', 'api_key_env'):
            if settings.get(key):
                route[key] = settings[key]
        key_env = route.get('api_key_env')
        if not key_env or not credentials.get(key_env):
            continue
        model = route.get('model') or route.get('model_id')
        base = route.get('base_url')
        transport = 'openai_chat'
        if route.get('sdk_type') == 'anthropic':
            base, transport = base or 'https://api.anthropic.com', 'anthropic_messages'
        if not model or not base:
            continue
        # Hermes/OpenAI clients expect the versioned API root.
        if base.rstrip('/') == 'https://api.deepseek.com':
            base += '/v1'
        routes[alias] = {'model': model, 'base_url': base, 'key_env': key_env, 'transport': transport}
        keys.add(key_env)
    if not routes:
        raise ValueError('No enabled, credentialed Hydra routes found')
    if credentials.get('TAVILY_API_KEY'):
        keys.add('TAVILY_API_KEY')
    return routes, keys


def configure(root, home):
    from dotenv import dotenv_values, set_key
    credentials = dotenv_values(root / '.env')
    routes, keys = hydra_profiles(root, credentials)
    if any((home / name).exists() for name in ('config.yaml', '.env', 'models.json')):
        raise ValueError('Bridge profile already exists; edit it locally instead of overwriting it')
    home.mkdir(parents=True, exist_ok=True)
    (home / 'workspace').mkdir(exist_ok=True)
    default = 'deepseek' if 'deepseek' in routes else next(iter(routes))
    models = {alias: {'provider': 'bridge-' + alias, 'model': route['model']} for alias, route in routes.items()}
    if 'deepseek' in models:
        models['deepseek']['reasoning'] = 'high'
    if 'gemini' in models:
        models['gemini']['reasoning'] = 'low'  # Gemini 3 requires thinking; zero budget is invalid.
    config = {
        'model': {'provider': models[default]['provider'], 'default': models[default]['model']},
        'providers': {'bridge-' + alias: {k: v for k, v in route.items() if k != 'model'}
                      for alias, route in routes.items()},
        'terminal': {'cwd': str(home / 'workspace')},
        'display': {'interface': 'cli'},
        'memory': {'memory_enabled': False, 'user_profile_enabled': False},
        'agent': {'max_turns': 12, 'reasoning_effort': 'none'},
        'auxiliary': {'title_generation': {'enabled': False}, 'background_review': {'enabled': False}},
    }
    if 'TAVILY_API_KEY' in keys:
        config['web'] = {'backend': 'tavily'}
    # JSON is valid YAML, avoiding a dependency in the companion runtime.
    (home / 'config.yaml').write_text(json.dumps(config, indent=2), encoding='utf-8')
    (home / 'models.json').write_text(json.dumps({'default': default, 'models': models}, indent=2), encoding='utf-8')
    for key in sorted(keys):
        set_key(str(home / '.env'), key, credentials[key])
    print('Configured Hermes aliases: ' + ', '.join(models))
    print('Default: ' + default + '; web: ' + ('Tavily' if 'TAVILY_API_KEY' in keys else 'not configured'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hydra', required=True, type=Path)
    parser.add_argument('--home', type=Path, default=Path.home() / '.hermes' / 'agentbridge')
    args = parser.parse_args()
    configure(args.hydra.resolve(), args.home.resolve())
