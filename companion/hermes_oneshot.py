"""Pinned v0.22.5 in-process entry: keep policy and execution in one worker.

The upstream CLI now delegates to a gateway, which would lose our process-local
tool registrations and guards. Its library one-shot runner still owns provider
resolution, native history and cleanup. Adapt that runner without starting a
gateway or enabling its CLI's automatic approval/hook switches.
"""
import argparse
from contextlib import redirect_stdout
import json
import os
import sys


def options(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['chat'])
    parser.add_argument('--cli', action='store_true', required=True)
    parser.add_argument('--oneshot', action='store_true', required=True)
    parser.add_argument('--query-file', choices=['-'], required=True)
    parser.add_argument('--format', choices=['stream-json'], required=True)
    parser.add_argument('--toolsets', choices=['agentbridge'], required=True)
    parser.add_argument('--in', dest='cwd', required=True)
    parser.add_argument('--provider', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--max-turns', type=int, required=True)
    parser.add_argument('--run-budget', type=float, required=True)
    parser.add_argument('--reasoning', default='none')
    parser.add_argument('--resume')
    parser.add_argument('--no-restore-cwd', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.max_turns <= 40 or not 0 < args.run_budget <= 86400:
        parser.error('Invalid bridge iteration/time budget')
    return args


def result_event(text, result):
    failed = (bool(result.get('error')) or result.get('completed') is not True
              or bool(result.get('partial')) or bool(result.get('interrupted')) or not text.strip())
    return {'type':'result', 'text':text, 'session_id':result.get('session_id'),
            'exit_code':1 if failed else 0,
            'error':str(result.get('error') or 'Hermes did not complete the turn.') if failed else None}


def run(argv, allowed):
    args = options(argv)
    from pathlib import Path
    if Path(args.cwd).resolve() != Path.cwd().resolve():
        raise ValueError('Worker folder differs from the validated bridge folder')
    prompt = sys.stdin.read()
    out = sys.stdout
    def emit(event):
        print(json.dumps(event, ensure_ascii=True), file=out, flush=True)
    # One process per turn, but these ARE resumable conversations. The upstream
    # finite-session marker removes skill saving and claims no later turn exists.
    os.environ.pop('HERMES_SINGLE_QUERY_SESSION', None)
    with redirect_stdout(sys.stderr):
        from gateway.session_context import declare_stateless_channel
        from hermes_cli.oneshot import _run_agent
        from tools.registry import registry
        import run_agent
        declare_stateless_channel()
        original = run_agent.AIAgent
        dispatch = registry.dispatch
        allowed = set(allowed)
        def restricted(name, params, **kwargs):
            if name not in allowed:
                return json.dumps({'error':'Tool is disabled at this access level.'})
            return dispatch(name, params, **kwargs)
        registry.dispatch = restricted
        class BridgeAgent(original):
            def __init__(self, **kwargs):
                kwargs.update(max_iterations=min(kwargs.get('max_iterations',args.max_turns),args.max_turns),
                              run_budget_seconds=args.run_budget, skip_background_review=True)
                super().__init__(**kwargs)
                actual = {tool['function']['name'] for tool in self.tools}
                if not actual <= allowed:
                    raise RuntimeError('Hermes added unexpected tools to the bridge turn: ' + ', '.join(sorted(actual - allowed)))

            def run_conversation(self, *a, **kw):
                # _run_agent disables streaming after construction; restore only
                # the compact JSONL callbacks at the actual turn boundary.
                self.stream_delta_callback = lambda text: emit({'type':'text','text':text})
                self.tool_start_callback = lambda name, *a, **kw: emit({'type':'tool_use','name':name})
                emit({'type':'system','subtype':'init','session_id':self.session_id})
                return super().run_conversation(*a, **kw)
        run_agent.AIAgent = BridgeAgent
        try:
            text, result = _run_agent(prompt, model=args.model, provider=args.provider,
                                      toolsets=['agentbridge'], use_config_toolsets=False,
                                      resume=args.resume, reasoning=args.reasoning)
            event = result_event(text, result)
        except Exception as error:
            event = {'type':'result','text':'','exit_code':1,'error':str(error)}
        finally:
            run_agent.AIAgent = original
            registry.dispatch = dispatch
    emit(event)
    return event['exit_code']
