"""Opt-in paid/live checks. Uses a temporary CODEX_HOME; prints no credentials."""
import asyncio
import argparse
from copy import deepcopy
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile

from aiohttp import web
from codex_model_bridge import create_app, load_settings
from codex_model_bridge.discovery import default_cache_path


async def run_codex(home, *args):
    process = await asyncio.create_subprocess_exec('codex', *args,
        env={**os.environ, 'CODEX_HOME': str(home)},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 150)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        # No raw logs: request diagnostics may contain private account information.
        events = []
        for line in stdout.decode().splitlines():
            try:
                event = json.loads(line)
                if event.get('type') in ('error', 'turn.failed'):
                    events.append(event.get('type'))
                    message = event.get('message') or event.get('error', {}).get('message', '')
                    print('Failure detail:', re.sub(r'[A-Za-z0-9_./+=-]{24,}', '<redacted>', message)[:800], flush=True)
            except ValueError:
                pass
        raise RuntimeError(f'Codex failed: exit={process.returncode}, events={events}, stderr_bytes={len(stderr)}')
    return stdout.decode()


async def account_limits(home, model):
    process = await asyncio.create_subprocess_exec('codex', 'app-server',
        env={**os.environ, 'CODEX_HOME': str(home)}, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    async def rpc(identifier, method, params):
        process.stdin.write((json.dumps({'id': identifier, 'method': method, 'params': params}) + '\n').encode())
        await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), 40)
            if not line:
                raise RuntimeError('App-server exited before replying')
            message = json.loads(line)
            if message.get('id') == identifier:
                if 'error' in message:
                    raise RuntimeError(f'App-server RPC failed: {method}')
                return message['result']
    try:
        await rpc(0, 'initialize', {'clientInfo': {'name': 'bridge-smoke', 'version': '0.1.0'}})
        limits = await rpc(1, 'account/rateLimits/read', {})
        rows = [limits.get('rateLimits', {})] + list((limits.get('rateLimitsByLimitId') or {}).values())
        assert any(window.get('windowDurationMins') == 10080
                   for row in rows if isinstance(row, dict)
                   for window in (row.get('primary') or {}, row.get('secondary') or {})), 'Weekly window missing'
        print('PASS official account weekly window (10080 minutes)', flush=True)
        listing = await rpc(2, 'model/list', {'includeHidden': False})
        assert any(row.get('model') == model or row.get('id') == model for row in listing['data']), 'Custom picker entry missing'
        print('PASS custom model in visible model/list', flush=True)
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


def events(output):
    return [json.loads(line) for line in output.splitlines() if line.startswith('{')]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata-only', action='store_true', help='Only check models and quota; no inference')
    parser.add_argument('--config', type=Path, help='Optional legacy route config')
    parser.add_argument('--cache', type=Path, default=default_cache_path())
    parser.add_argument('--model', help='Custom model alias to exercise')
    parser.add_argument('--test-alias', action='store_true', help='Add a temporary alias and exercise upstream model rewriting')
    args = parser.parse_args()
    source = Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser()
    watched = [source / name for name in ('config.toml', 'auth.json')] + [args.config or args.cache]
    before = [hashlib.sha256(p.read_bytes()).digest() for p in watched]
    settings = load_settings(source, config_path=args.config, cache_path=args.cache)
    selected_model = args.model or next((r.model['slug'] for r in settings.routes
                                        if r.model['slug'] == 'claude_proxy/deepseek-v4-flash'), settings.routes[0].model['slug'])
    assert any(r.model['slug'] == selected_model for r in settings.routes), 'Selected model is missing'
    if args.test_alias:
        alias = deepcopy(next(r for r in settings.routes if r.model['slug'] == selected_model))
        alias.model['slug'] = 'bridge-smoke/' + selected_model
        alias.model['display_name'] = 'Temporary alias smoke test'
        settings.routes.append(alias)
        selected_model = alias.model['slug']
    runner = web.AppRunner(create_app(settings), access_log=None, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        with tempfile.TemporaryDirectory(prefix='codex-model-bridge-smoke-') as root:
            home = Path(root)
            shutil.copyfile(source / 'auth.json', home / 'auth.json')
            (home / 'auth.json').chmod(0o600)
            (home / 'config.toml').write_text(f'openai_base_url="http://127.0.0.1:{port}/v1"\nmodel_reasoning_effort="high"\n')
            catalog = json.loads(await run_codex(home, 'debug', 'models'))
            assert {r.model['slug'] for r in settings.routes} <= {m['slug'] for m in catalog['models']}
            assert any(m['slug'] == 'gpt-6-astra' for m in catalog['models'])
            print(f'PASS Codex parsed dynamic catalog ({len(catalog["models"])} models)', flush=True)
            await account_limits(home, selected_model)
            if args.metadata_only:
                return
            first = events(await run_codex(home, 'exec', '--skip-git-repo-check', '-C', root,
                '-s', 'danger-full-access', '-m', 'gpt-6-astra', '--json',
                'Remember the code word ORCHID_472 for this conversation. Reply only ORCHID_472. Do not use tools.'))
            thread = next(e['thread_id'] for e in first if e['type'] == 'thread.started')
            assert any('ORCHID_472' in e.get('item', {}).get('text', '') for e in first)
            print('PASS official model response', flush=True)
            second = events(await run_codex(home, 'exec', '-m', selected_model, '-s', 'danger-full-access',
                'resume', '--skip-git-repo-check', '--json', thread,
                'Run pwd exactly once using the shell tool; do not read or change files. Then repeat the code word from the preceding user message.'))
            commands = [e['item'] for e in second if e.get('item', {}).get('type') == 'command_execution'
                        and e['type'] == 'item.completed']
            if not commands or any(c.get('exit_code') != 0 for c in commands):
                print('Tool diagnostics:', [(e.get('type'), e.get('item', {}).get('type'),
                      e.get('item', {}).get('exit_code')) for e in second], flush=True)
                for e in second:
                    item = e.get('item', {})
                    if item.get('type') == 'agent_message':
                        print('Agent reply:', item.get('text', '')[:1200], flush=True)
                raise AssertionError('No successful real tool call')
            assert any('ORCHID_472' in e.get('item', {}).get('text', '') for e in second), 'DeepSeek lost history'
            print(f'PASS same thread official → {selected_model} + real shell tool + history', flush=True)
            third = events(await run_codex(home, 'exec', '-m', 'gpt-6-astra', '-s', 'danger-full-access',
                'resume', '--skip-git-repo-check', '--json', thread,
                'Repeat the code word from our conversation. Reply only the code word; no tools.'))
            assert any('ORCHID_472' in e.get('item', {}).get('text', '') for e in third), 'Official model lost history'
            print(f'PASS same thread {selected_model} → official + history', flush=True)
    finally:
        await runner.cleanup()
        assert before == [hashlib.sha256(p.read_bytes()).digest() for p in watched], 'Original config/auth changed'
        print('PASS original config/auth hashes unchanged', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
