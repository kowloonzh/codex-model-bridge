import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from codex_model_bridge import Route, Settings, create_app


class CompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.exports = []
        self.custom = []
        self.fail_export = False
        self.truncate = False
        self.delta_only = False

        async def upstream(request):
            if request.path == '/official/models':
                return web.json_response({'models': [{'slug': 'gpt-native', 'visibility': 'list'}]})
            body = await request.json()
            if request.path.startswith('/custom/'):
                self.custom.append((body, dict(request.headers)))
                return web.json_response({'ok': True})
            self.exports.append((body, dict(request.headers)))
            if self.fail_export:
                return web.Response(status=503, text='private upstream diagnostic')
            await asyncio.sleep(.02)
            if self.truncate:
                return web.Response(text='data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
                                    content_type='text/event-stream')
            event = {'type': 'response.completed', 'response': {'status': 'completed', 'output': [
                {'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': 'The prior task requires keeping marker ORCHID_472 and file src/app.py.'}]}]}}
            prefix = ''
            if self.delta_only:
                prefix = 'data: '+json.dumps({'type': 'response.output_text.delta', 'delta': 'Portable summary ORCHID_472'})+'\n\n'
                event['response']['output'] = []
            return web.Response(text=prefix+'data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n', content_type='text/event-stream')

        app = web.Application()
        app.router.add_route('*', '/{path:.*}', upstream)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        settings = Settings([Route(str(self.upstream.make_url('/custom')), 'custom-token',
                                   {'slug': 'qax/flash'}, 'flash')],
                            official_url=str(self.upstream.make_url('/official')))
        self.client = TestClient(TestServer(create_app(settings)))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer official-token', 'ChatGPT-Account-Id': 'account-a',
                        'User-Agent': 'codex_cli_rs/0.155.1'}
        self.checkpoint = {'type': 'compaction', 'id': 'cmp_old', 'encrypted_content': 'gAAAAA-synthetic'}
        self.body = {'model': 'qax/flash', 'input': [self.checkpoint,
                     {'role': 'user', 'content': 'Continue the prior task; CURRENT_MESSAGE_PRIVATE'}]}

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()

    async def send(self, headers=None):
        response = await self.client.post('/v1/responses', json=self.body, headers=headers or self.headers)
        await response.read()
        return response

    async def test_exports_checkpoint_and_preserves_current_input_with_isolated_auth(self):
        response = await self.send()
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.exports), 1)
        exported, auth = self.exports[0]
        self.assertEqual(auth['Authorization'], 'Bearer official-token')
        self.assertEqual({k.lower(): v for k, v in auth.items()}['chatgpt-account-id'], 'account-a')
        self.assertNotIn('CURRENT_MESSAGE_PRIVATE', json.dumps(exported))
        self.assertEqual(exported['tools'], [])
        self.assertEqual(exported['input'][0]['encrypted_content'], self.checkpoint['encrypted_content'])
        body, headers = self.custom[0]
        self.assertEqual(headers['Authorization'], 'Bearer custom-token')
        self.assertNotIn('ChatGPT-Account-Id', headers)
        self.assertEqual(body['input'][-1], self.body['input'][-1])
        self.assertIn('ORCHID_472', json.dumps(body['input'][0]))
        self.assertNotIn('encrypted_content', json.dumps(body))
        self.assertEqual(self.body['input'][0], self.checkpoint)

    async def test_same_checkpoint_is_exported_once_even_with_concurrent_requests(self):
        responses = await asyncio.gather(self.send(), self.send())
        self.assertEqual([r.status for r in responses], [200, 200])
        self.assertEqual((await self.send()).status, 200)
        self.assertEqual(len(self.exports), 1)

    async def test_cache_is_scoped_to_account_and_credential(self):
        await self.send()
        await self.send({**self.headers, 'ChatGPT-Account-Id': 'account-b'})
        await self.send({**self.headers, 'Authorization': 'Bearer another-token'})
        self.assertEqual(len(self.exports), 3)

    async def test_failed_export_never_forwards_or_caches_partial_history(self):
        self.fail_export = True
        response = await self.send()
        self.assertEqual(response.status, 502)
        self.assertNotIn('private upstream diagnostic', await response.text())
        self.assertEqual(self.custom, [])
        self.fail_export = False
        self.assertEqual((await self.send()).status, 200)
        self.assertEqual(len(self.exports), 2)

    async def test_incomplete_stream_does_not_become_a_summary(self):
        self.truncate = True
        self.assertEqual((await self.send()).status, 502)
        self.assertEqual(self.custom, [])
        self.truncate = False
        self.assertEqual((await self.send()).status, 200)

    async def test_plain_summary_and_local_marker_need_no_official_call(self):
        self.body['input'] = [{'type': 'context_compaction'},
                              {'type': 'compaction_summary', 'summary': 'Keep ORCHID_472'},
                              self.body['input'][-1]]
        self.assertEqual((await self.send()).status, 200)
        self.assertEqual(self.exports, [])
        self.assertEqual(len(self.custom[0][0]['input']), 2)
        self.assertIn('ORCHID_472', json.dumps(self.custom[0][0]['input']))

    async def test_official_requests_keep_original_encrypted_checkpoint(self):
        self.body['model'] = 'gpt-native'
        self.assertEqual((await self.send()).status, 200)
        self.assertEqual(self.exports[0][0]['input'][0], self.checkpoint)
        self.assertEqual(self.custom, [])

    async def test_completed_event_can_omit_text_already_sent_as_deltas(self):
        self.delta_only = True
        self.assertEqual((await self.send()).status, 200)
        self.assertIn('Portable summary ORCHID_472', json.dumps(self.custom[0][0]))


if __name__ == '__main__':
    unittest.main()
