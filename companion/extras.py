"""Per-run configuration for shared browser/media tools and native CLI extras."""
import json
from pathlib import Path

from .hermes_policy import BROWSER_READ, BROWSER_WRITE, features, tool_names


def media_tools(level, web, flags):
    media = set(BROWSER_READ + BROWSER_WRITE + ['vision_analyze', 'text_to_speech'])
    return [name for name in tool_names(level, web, flags) if name in media]


def configuration(cfg):
    if not cfg.hermes:
        return None
    home = Path(cfg.hermes_home) if cfg.hermes_home else Path.home() / '.hermes' / 'agentbridge'
    flags = features(home)
    if not flags.get('coding_agents'):
        return None
    names = media_tools(cfg.sandbox, cfg.web, flags)
    return {
        'command': cfg.hermes,
        'args': [str(Path(__file__).with_name('extras_server.py')), str(home.resolve()),
                 str(cfg.project.resolve()), cfg.sandbox, 'web' if cfg.web else 'none'],
        'tools': names,
    }


def claude_options(config):
    if config is None:
        return [], []
    server = {key: config[key] for key in ('command', 'args')}
    return (['--settings', json.dumps({'autoMemoryEnabled': True}),
             '--mcp-config', json.dumps({'mcpServers': {'agentbridge': server}})],
            ['Skill', 'Agent', 'TodoWrite'] + ['mcp__agentbridge__' + name for name in config['tools']])


def codex_options(config):
    if config is None:
        return []
    values = {
        'features.memories': True,
        'memories.use_memories': True,
        'memories.generate_memories': True,
        'features.multi_agent': True,
        'agents.max_concurrent_threads_per_session': 2,
        'mcp_servers.agentbridge.command': config['command'],
        'mcp_servers.agentbridge.args': config['args'],
        'mcp_servers.agentbridge.enabled_tools': config['tools'],
        'mcp_servers.agentbridge.startup_timeout_sec': 60,
        'mcp_servers.agentbridge.tool_timeout_sec': 180,
        'mcp_servers.agentbridge.required': True,
        'approvals_reviewer': 'auto_review',
    }
    # JSON strings/arrays/bools are also valid TOML values. These are argv values,
    # never shell text; project paths and provider keys are not interpolated.
    # Keep shell/file escalation disabled as in the original headless adapter.
    # MCP requests can reach automatic review without an interactive terminal.
    policy = ('approval_policy={granular={sandbox_approval=false,rules=false,'
              'mcp_elicitations=true,request_permissions=false,skill_approval=false}}')
    return ['-c', policy] + [arg for key, value in values.items()
                            for arg in ('-c', key + '=' + json.dumps(value))]
