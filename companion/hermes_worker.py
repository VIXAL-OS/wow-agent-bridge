"""Run Hermes in its own venv with the bridge's explicit tool/access policy."""
import os
from pathlib import Path
import sys


def main():
    mode = sys.argv.pop(1)
    if mode not in ('web', 'none'):
        raise ValueError('Invalid bridge tool mode')
    level = sys.argv.pop(1)
    project = Path(sys.argv.pop(1)).resolve()
    from hermes_runtime import prepare
    prepare(os.environ['HERMES_HOME'], project)
    if mode == 'web':
        # Explicitly register this one bundled provider: general plugin discovery
        # remains disabled, including MCP, shell hooks and outbound webhooks.
        from agent.web_search_registry import register_provider
        from plugins.web.tavily.provider import TavilyWebSearchProvider
        register_provider(TavilyWebSearchProvider())
    from toolsets import create_custom_toolset
    from hermes_policy import features, install_guards, install_scratch_policy, tool_names
    allowed = tool_names(level, mode == 'web', features(os.environ['HERMES_HOME']))
    create_custom_toolset('agentbridge', 'Agent Bridge tools and project access', tools=allowed)
    from model_tools import _select_tool_names
    if _select_tool_names(['agentbridge'], None, True) != set(allowed):
        raise RuntimeError('Hermes tool selection changed; refusing to enable unexpected tools')
    install_guards(project, level)
    install_scratch_policy(project, level)
    from browser_lifetime import cleanup_browsers
    try:
        import json
        runtime = Path(os.environ['HERMES_HOME']) / 'bridge-runtime.json'
        entry = json.loads(runtime.read_text(encoding='utf-8')).get('hermes_entry') if runtime.exists() else None
        if entry == 'oneshot-v0.22.5':
            from hermes_oneshot import run
            raise SystemExit(run(sys.argv[1:], allowed))
        if entry:
            raise ValueError('Unknown Hermes bridge entry point: ' + entry)
        from hermes_cli.main import main as hermes_main
        hermes_main()
    finally:
        cleanup_browsers()


if __name__ == '__main__':
    from hermes_runtime import bootstrap
    bootstrap(os.environ['HERMES_HOME'])
    main()
