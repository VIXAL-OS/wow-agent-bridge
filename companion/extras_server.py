"""Expose the verified Hermes browser/media tools over a private MCP stdio pipe.

Run with Hermes's Python. No HTTP listener, agent loop, or credential forwarding
to the calling model. Each caller has its own headless browser session.
"""
import asyncio
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
import uuid


def initialize(home, project, level, web):
    # Imports stay lazy so the companion does not depend on Hermes or MCP.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from hermes_runtime import prepare
    from hermes_policy import BROWSER_READ, BROWSER_WRITE, features, install_guards, tool_names
    prepare(home, project)
    from model_tools import get_tool_definitions
    from toolsets import create_custom_toolset
    from tools.registry import registry
    flags = features(home)
    media = set(BROWSER_READ + BROWSER_WRITE + ['vision_analyze', 'text_to_speech'])
    allowed = [name for name in tool_names(level, web, flags) if name in media]
    create_custom_toolset('agentbridge-media', 'Agent Bridge browser and media', tools=allowed)
    install_guards(project, level)
    definitions = get_tool_definitions(['agentbridge-media'], quiet_mode=True, skip_tool_search_assembly=True)
    actual = {item['function']['name'] for item in definitions}
    if actual != set(allowed):
        raise RuntimeError('Shared tools unavailable; check runtime dependencies: ' + ', '.join(set(allowed) - actual))
    return registry, {item['function']['name']: item['function'] for item in definitions}


async def serve(home, project, level, web):
    # Third-party tool imports/logging must never corrupt the JSON-RPC stream.
    with redirect_stdout(sys.stderr):
        registry, definitions = initialize(home, project, level, web)
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    import mcp_types as types
    import jsonschema
    task_id = 'agentbridge-' + uuid.uuid4().hex[:16]
    mutex = asyncio.Lock()

    async def list_tools(ctx, params):
        from hermes_policy import BROWSER_WRITE
        return types.ListToolsResult(tools=[types.Tool(
            name=name, description=definition.get('description', ''),
            input_schema=definition['parameters'],
            annotations=types.ToolAnnotations(
                read_only_hint=name not in BROWSER_WRITE + ['text_to_speech'],
                destructive_hint=name in BROWSER_WRITE,
                open_world_hint=True,
            ),
        ) for name, definition in definitions.items()])

    async def call_tool(ctx, params):
        name, args = params.name, params.arguments or {}
        if name not in definitions:
            return types.CallToolResult(content=[types.TextContent(type='text', text='Tool is disabled at this access level.')], is_error=True)
        try:
            jsonschema.validate(args, definitions[name]['parameters'])
        except jsonschema.ValidationError as error:
            return types.CallToolResult(content=[types.TextContent(type='text', text=error.message)], is_error=True)
        def dispatch():
            with redirect_stdout(sys.stderr):
                return registry.dispatch(name, args, task_id=task_id)
        async with mutex:
            raw = await asyncio.to_thread(dispatch)
        text = raw if isinstance(raw, str) else json.dumps(raw)
        try:
            result = json.loads(text)
            failed = isinstance(result, dict) and (bool(result.get('error')) or result.get('success') is False)
        except ValueError:
            failed = False
        return types.CallToolResult(content=[types.TextContent(type='text', text=text)], is_error=failed)

    server = Server('agentbridge', version='1.0.0', on_list_tools=list_tools, on_call_tool=call_tool)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        with redirect_stdout(sys.stderr):
            from browser_lifetime import cleanup_browsers
            cleanup_browsers()


if __name__ == '__main__':
    home, project = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    if not home.is_dir() or not project.is_dir() or sys.argv[4] not in ('web', 'none'):
        raise ValueError('Invalid bridge home, project or web mode')
    asyncio.run(serve(home, project, sys.argv[3], sys.argv[4] == 'web'))
