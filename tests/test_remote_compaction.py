import asyncio
import base64
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from codex_model_bridge import Route, Settings, create_app


def events(text):
    return [json.loads(line[6:]) for line in text.splitlines()
            if line.startswith('data: ') and line != 'data: [DONE]']


class RemoteCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.mode = 'ok'
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

        async def upstream(request):
            body = await request.json()
            self.requests.append((request.path, body, dict(request.headers)))
            self.started.set()
            if self.mode == 'wait':
                try:
                    await asyncio.Future()
                finally:
                    self.cancelled.set()
            if self.mode == 'error':
                return web.Response(status=503, text='private diagnostic')
            text = 'Keep ORCHID_472; edit src/app.py next.' if self.mode != 'empty' else ''
            items = [{'type': 'reasoning', 'id': 'rs_1', 'summary': []},
                     {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]},
                     {'type': 'message', 'role': 'assistant', 'content': []}]
            if self.mode == 'tool':
                items.append({'type': 'function_call', 'name': 'shell', 'call_id': 'call_1', 'arguments': '{}'})
            output = [{'type': 'response.output_item.done', 'output_index': i, 'item': item} for i, item in enumerate(items)]
            if self.mode != 'truncated':
                output.append({'type': 'response.completed', 'response': {'id': 'resp_upstream', 'status': 'completed',
                    'output': [], 'usage': {'input_tokens': 123, 'output_tokens': 45, 'total_tokens': 168}}})
            return web.Response(text=''.join('data: '+json.dumps(e)+'\n\n' for e in output), content_type='text/event-stream')

        app = web.Application()
        app.router.add_route('*', '/{path:.*}', upstream)
        self.upstream = TestServer(app, handler_cancellation=True)
        await self.upstream.start_server()
        self.settings = Settings([Route(str(self.upstream.make_url('/custom')), 'custom-token',
                                       {'slug': 'custom/flash'}, 'flash')],
                                 official_url=str(self.upstream.make_url('/official')))
        self.client = TestClient(TestServer(create_app(self.settings), handler_cancellation=True))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer official-token', 'ChatGPT-Account-Id': 'account'}
        self.body = {'model': 'custom/flash', 'stream': True, 'instructions': 'Original constraint: preserve user files.',
                     'tools': [{'type': 'function', 'name': 'shell'}], 'tool_choice': 'required',
                     'parallel_tool_calls': True, 'text': {'format': {'type': 'json_object'}},
                     'input': [{'role': 'user', 'content': 'Remember ORCHID_472.'},
                               {'type': 'additional_tools', 'tools': [{'type': 'function', 'name': 'shell'}]},
                               {'type': 'compaction_trigger'}]}

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()

    async def compact(self, body=None):
        r = await self.client.post('/v1/responses', headers=self.headers, json=body or self.body)
        return r, await r.text()

    async def checkpoint(self):
        r, text = await self.compact()
        self.assertEqual(r.status, 200)
        items = [e['item'] for e in events(text) if e['type'] == 'response.output_item.done']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['type'], 'compaction')
        return items[0], events(text)

    async def test_v2_returns_one_checkpoint_and_truthful_completion(self):
        checkpoint, output = await self.checkpoint()
        completed = next(e['response'] for e in output if e['type'] == 'response.completed')
        self.assertEqual(completed['output'], [checkpoint])
        self.assertEqual(completed['usage']['total_tokens'], 168)
        self.assertEqual(completed['model'], 'custom/flash')
        self.assertTrue(checkpoint['encrypted_content'].startswith('cmb1:'))

    async def test_summary_request_cannot_execute_tools_and_keeps_constraints(self):
        await self.checkpoint()
        path, body, headers = self.requests[-1]
        self.assertEqual(path, '/custom/responses')
        self.assertEqual(headers['Authorization'], 'Bearer custom-token')
        self.assertNotIn('ChatGPT-Account-Id', headers)
        self.assertEqual(body['tools'], [])
        for name in ('tool_choice', 'parallel_tool_calls', 'text'):
            self.assertNotIn(name, body)
        self.assertIn('preserve user files', json.dumps(body['input']))
        self.assertFalse(any(i.get('type') in ('compaction_trigger', 'additional_tools') for i in body['input']))

    async def test_repeated_compaction_retains_previous_summary(self):
        checkpoint, _ = await self.checkpoint()
        await self.compact({**self.body, 'input': [checkpoint, {'role': 'user', 'content': 'Next step'}, {'type': 'compaction_trigger'}]})
        self.assertEqual(len(self.requests), 2)
        self.assertIn('ORCHID_472', json.dumps(self.requests[-1][1]))
        self.assertNotIn('cmb1:', json.dumps(self.requests[-1][1]))

    async def test_self_contained_checkpoint_replays_after_restart_to_both_backends(self):
        checkpoint, _ = await self.checkpoint()
        await self.client.close()
        self.client = TestClient(TestServer(create_app(self.settings)))
        await self.client.start_server()
        for model, path in [('custom/flash', '/custom/responses'), ('gpt-native', '/official/responses')]:
            r = await self.client.post('/v1/responses', headers=self.headers, json={'model': model,
                'previous_response_id': 'resp_old', 'input': [checkpoint, {'id': 'msg_old', 'role': 'user', 'content': 'Continue'}]})
            await r.read()
            self.assertEqual(r.status, 200)
            self.assertEqual(self.requests[-1][0], path)
            body = self.requests[-1][1]
            self.assertIn('ORCHID_472', json.dumps(body))
            self.assertNotIn('encrypted_content', json.dumps(body))
            self.assertNotIn('previous_response_id', body)
            self.assertNotIn('id', body['input'][-1])

    async def test_failures_never_publish_a_checkpoint(self):
        for mode in ('error', 'empty', 'truncated', 'tool'):
            with self.subTest(mode=mode):
                self.mode = mode
                r, text = await self.compact()
                self.assertEqual(r.status, 502)
                self.assertNotIn('cmb1:', text)
                self.assertNotIn('private diagnostic', text)

    async def test_invalid_capsules_do_not_fall_through_to_official_export(self):
        for value in ['cmb1:broken!', 'cmb2:unknown', 'cmb1:'+base64.b64encode(b'{"v":1,"summary":""}').decode()]:
            r, _ = await self.compact({'model': 'custom/flash', 'input': [{'type': 'compaction', 'encrypted_content': value}]})
            self.assertEqual(r.status, 400)
        self.assertEqual(self.requests, [])

    async def test_native_compaction_remains_passthrough(self):
        native = {**self.body, 'model': 'gpt-native'}
        r, _ = await self.compact(native)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.requests[0][0], '/official/responses')
        self.assertEqual(self.requests[0][1], native)

    async def test_client_cancellation_cancels_summary_request(self):
        self.mode = 'wait'
        task = asyncio.create_task(self.compact())
        await asyncio.wait_for(self.started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(self.cancelled.wait(), 2)

    async def test_capsule_codec_preserves_escaped_text_at_limit(self):
        from codex_model_bridge.portable import encode, decode
        text = '"\n' * 16000
        self.assertEqual(decode(encode(text)), text)

    async def test_malformed_triggers_are_rejected_without_upstream_calls(self):
        for items in [[{'type': 'compaction_trigger'}, {'role': 'user', 'content': 'late'}],
                      [{'type': 'compaction_trigger'}, {'type': 'compaction_trigger'}]]:
            r, _ = await self.compact({**self.body, 'input': items})
            self.assertEqual(r.status, 409)
        self.assertEqual(self.requests, [])

    async def test_nonstream_v2_returns_one_checkpoint(self):
        r, text = await self.compact({**self.body, 'stream': False})
        self.assertEqual(r.status, 200)
        data = json.loads(text)
        self.assertEqual(data['status'], 'completed')
        self.assertEqual([i['type'] for i in data['output']], ['compaction'])


if __name__ == '__main__':
    unittest.main()
