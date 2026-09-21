"""Codex app-server compaction test. Default uses mock models; --live consumes quota."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from aiohttp import web
from codex_model_bridge import Route, Settings, create_app, load_settings
from codex_model_bridge.portable import decode, PROMPT


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--model', default='deepseek/deepseek-flash')
    args = parser.parse_args()
    home = Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser()
    watched = [home/'config.toml', home/'auth.json']
    hashes = [hashlib.sha256(p.read_bytes()).digest() for p in watched]
    runners = []
    counts = {'summary': 0, 'normal': 0}

    async def serve(app):
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        runners.append(runner)
        return f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'

    try:
        if args.live:
            settings = load_settings(home)
            custom, official = args.model, 'gpt-6-astra'
        else:
            native = json.loads((home/'models_cache.json').read_text())['models'][0]
            model = {**native, 'slug': 'gpt-fixture', 'visibility': 'list', 'tool_mode': None,
                     'use_responses_lite': False, 'prefer_websockets': False}

            async def upstream(request):
                if request.path.endswith('/models'):
                    return web.json_response({'models': [model]})
                body = await request.json()
                assert 'cmb1:' not in json.dumps(body), 'Opaque bridge checkpoint leaked upstream'
                summary = body.get('instructions') == PROMPT
                counts['summary' if summary else 'normal'] += 1
                if summary:
                    assert body['tools'] == []
                    assert 'NOTE_ABCDEFGH' in json.dumps(body['input']), 'Prior assistant state lost'
                text = 'Assistant generated NOTE_ABCDEFGH. Preserve that label.' if summary else 'NOTE_ABCDEFGH'
                item = {'type': 'message', 'id': 'msg_fixture', 'role': 'assistant', 'phase': 'final_answer',
                        'content': [{'type': 'output_text', 'text': text}]}
                usage = {'input_tokens': 45000 if counts['normal'] == 1 and not summary else 100,
                         'output_tokens': 20, 'total_tokens': 45020 if counts['normal'] == 1 and not summary else 120}
                response = {'id': 'resp_'+str(sum(counts.values())), 'status': 'completed', 'output': [item], 'usage': usage}
                events = [{'type': 'response.output_item.done', 'item': item, 'output_index': 0},
                          {'type': 'response.completed', 'response': response}]
                return web.Response(text=''.join('data: '+json.dumps(e)+'\n\n' for e in events), content_type='text/event-stream')

            upstream_app = web.Application()
            upstream_app.router.add_route('*', '/{path:.*}', upstream)
            url = await serve(upstream_app)
            custom, official = 'fixture/flash', 'gpt-fixture'
            settings = Settings([Route(url+'/custom', 'fixture-token', {**model, 'slug': custom}, 'flash')],
                                official_url=url+'/official')
        bridge = await serve(create_app(settings))
        with tempfile.TemporaryDirectory(prefix='bridge-v2-smoke-') as root:
            temporary = Path(root)
            shutil.copyfile(home/'auth.json', temporary/'auth.json')
            (temporary/'auth.json').chmod(0o600)
            config = f'openai_base_url="{bridge}/v1"\nmodel="{custom}"\nmodel_reasoning_effort="low"\n'
            if not args.live:
                config += 'model_auto_compact_token_limit=30000\n'
            (temporary/'config.toml').write_text(config)
            notices = []
            process = None

            async def launch():
                return await asyncio.create_subprocess_exec('codex', 'app-server',
                    env={**os.environ, 'CODEX_HOME': root}, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

            async def stop():
                if process.returncode is None:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

            async def receive():
                line = await asyncio.wait_for(process.stdout.readline(), 220)
                if not line:
                    raise RuntimeError('Codex app-server stopped unexpectedly')
                message = json.loads(line)
                if message.get('method') == 'error':
                    raise RuntimeError('Codex reported an error during the isolated smoke test')
                return message

            async def rpc(i, method, params):
                process.stdin.write((json.dumps({'id': i, 'method': method, 'params': params})+'\n').encode())
                await process.stdin.drain()
                while True:
                    message = await receive()
                    if message.get('id') == i:
                        if 'error' in message:
                            raise RuntimeError('RPC failed: '+method)
                        return message.get('result')
                    notices.append(message)

            async def completed():
                while True:
                    for i, message in enumerate(notices):
                        if message.get('method') == 'turn/completed':
                            notices.pop(i)
                            assert message['params']['turn']['status'] == 'completed', 'Turn failed'
                            return
                    notices.append(await receive())

            async def initialize(i):
                await rpc(i, 'initialize', {'clientInfo': {'name': 'bridge-v2-smoke', 'version': '1'},
                                           'capabilities': {'experimentalApi': True}})
                process.stdin.write(b'{"method":"initialized"}\n')
                await process.stdin.drain()

            async def turn(i, text, model=custom):
                notices.clear()
                await rpc(i, 'turn/start', {'threadId': thread, 'model': model,
                                          'input': [{'type': 'text', 'text': text}]})
                await completed()
                replies = [n['params']['item'].get('text', '') for n in notices
                           if n.get('method') == 'item/completed' and n['params']['item'].get('type') == 'agentMessage']
                assert replies, 'No assistant message'
                return replies[-1].strip()

            try:
                process = await launch()
                await initialize(1)
                result = await rpc(2, 'thread/start', {'model': custom, 'cwd': root,
                                    'approvalPolicy': 'never', 'sandbox': 'read-only'})
                thread = result['thread']['id']
                label = await turn(3, 'Invent one random label: NOTE_ followed by exactly eight uppercase English letters. Reply only the label. Do not use tools.')
                assert re.fullmatch(r'NOTE_[A-Z]{8}', label), 'Unexpected synthetic label format'
                if not args.live:
                    assert await turn(4, 'Repeat only the label you generated. No tools.') == label
                    assert counts['summary'] >= 1, 'Automatic compaction was not triggered'
                    print('PASS automatic v2 compaction', flush=True)
                for i in (5, 6):
                    notices.clear()
                    await rpc(i, 'thread/compact/start', {'threadId': thread})
                    await completed()
                    print('PASS manual v2 compaction', i-4, flush=True)
                await stop()
                checkpoints = []
                for file in (temporary/'sessions').rglob('*.jsonl'):
                    for line in file.read_text().splitlines():
                        record = json.loads(line)
                        if record.get('type') == 'compacted':
                            checkpoints += [item for item in record['payload'].get('replacement_history', [])
                                            if item.get('type') == 'compaction']
                assert len(checkpoints) >= 2
                assert label in decode(checkpoints[-1]['encrypted_content']), 'Assistant-only fact missing from summary'
                notices.clear()
                process = await launch()
                await initialize(7)
                await rpc(8, 'thread/resume', {'threadId': thread, 'cwd': root})
                assert await turn(9, 'Repeat only the label you generated earlier. No tools.') == label
                print('PASS client restart, resume and checkpoint recall', flush=True)
                assert await turn(10, 'Repeat only the label you generated earlier. No tools.', official) == label
                print('PASS switch back to official with checkpoint recall', flush=True)
            finally:
                if process:
                    await stop()
    finally:
        for runner in reversed(runners):
            await runner.cleanup()
        assert hashes == [hashlib.sha256(p.read_bytes()).digest() for p in watched]
        print('PASS original configuration/authentication unchanged', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
