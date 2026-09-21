import json
import asyncio
from pathlib import Path
import tempfile
import unittest

from aiohttp import web
import aiohttp
from aiohttp.test_utils import TestClient, TestServer

try:
    from codex_model_bridge import load_settings, create_app
except ImportError:
    load_settings = create_app = None


class ConfigTests(unittest.TestCase):
    def test_reads_profile_provider_and_catalog_without_exposing_secret(self):
        self.assertIsNotNone(load_settings, 'configuration loader is not implemented')
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            (p / 'config.toml').write_text('[model_providers.proxy]\nbase_url="http://localhost:9999/v1"\nwire_api="responses"\nexperimental_bearer_token="private-key"\n')
            (p / 'qax.config.toml').write_text('model="deepseek-v4-flash"\nmodel_provider="proxy"\nmodel_catalog_json="models.json"\n')
            (p / 'models.json').write_text(json.dumps({'models': [{'slug': 'deepseek-v4-flash', 'tool_mode': None}]}))
            settings = load_settings(p, 'qax').routes[0]
            self.assertEqual(settings.model['slug'], 'deepseek-v4-flash')
            self.assertEqual(settings.token, 'private-key')
            self.assertNotIn('private-key', repr(settings))
            self.assertEqual(settings.base_url, 'http://localhost:9999/v1')


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(create_app, 'HTTP bridge is not implemented')
        self.requests = []
        self.generation = 0
        self.status = 200
        self.stream_release = asyncio.Event()

        async def upstream(request):
            raw = await request.read()
            self.requests.append((request.path, dict(request.headers), raw, request.query_string))
            if request.path.endswith('/models'):
                self.generation += 1
                return web.json_response({'models': [{'slug': f'gpt-new-{self.generation}', 'visibility': 'list'}], 'extra': 'keep'}, status=self.status)
            if raw and json.loads(raw).get('test_stream'):
                response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
                await response.prepare(request)
                await response.write(b'data: first\n\n')
                await self.stream_release.wait()
                await response.write(b'data: [DONE]\n\n')
                return response
            if raw and json.loads(raw).get('test_reset'):
                response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
                await response.prepare(request)
                complete = json.loads(raw)['test_reset'] == 'complete'
                await response.write(b'data: [DONE]\n\n' if complete else b'data: partial\n\n')
                await asyncio.sleep(0.02)
                request.transport.close()
                return response
            if request.path.endswith('/slow'):
                return web.Response()
            return web.Response(body=b'data: {"type":"response.completed"}\n\ndata: [DONE]\n\n', status=self.status,
                                headers={'Content-Type': 'text/event-stream', 'x-codex-primary-used-percent': '12'})

        app = web.Application()
        app.router.add_route('*', '/{path:.*}', upstream)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        from codex_model_bridge import Settings, Route
        settings = Settings(routes=[Route(base_url=str(self.upstream.make_url('/deepseek')), token='ds-secret',
                            model={'slug': 'deepseek-v4-flash', 'tool_mode': None, 'use_responses_lite': False},
                            upstream_model='deepseek-v4-flash')],
                            official_url=str(self.upstream.make_url('/official')))
        self.client = TestClient(TestServer(create_app(settings)))
        await self.client.start_server()
        self.auth = {'Authorization': 'Bearer official-secret', 'ChatGPT-Account-Id': 'account-private',
                     'Cookie': 'session=private', 'User-Agent': 'codex_cli_rs/0.155.1'}

    async def asyncTearDown(self):
        if hasattr(self, 'client'):
            await self.client.close()
        if hasattr(self, 'upstream'):
            await self.upstream.close()

    async def test_dynamic_catalog_includes_new_official_models_and_deepseek(self):
        for i in (1, 2):
            r = await self.client.get('/v1/models?client_version=0.155.1', headers=self.auth)
            data = await r.json()
            self.assertEqual([m['slug'] for m in data['models']], [f'gpt-new-{i}', 'deepseek-v4-flash'])
            self.assertEqual(data['extra'], 'keep')
            self.assertIsNone(data['models'][1]['tool_mode'])
            self.assertEqual(self.requests[-1][3], 'client_version=0.155.1')

    async def test_official_request_preserves_auth_body_and_rate_limit_header(self):
        body = {'model': 'gpt-6-astra', 'input': [], 'service_tier': 'priority'}
        r = await self.client.post('/v1/responses', json=body, headers=self.auth)
        self.assertEqual(r.status, 200)
        self.assertIn(b'[DONE]', await r.read())
        path, headers, raw, _ = self.requests[-1]
        self.assertEqual(path, '/official/responses')
        self.assertEqual(headers['Authorization'], 'Bearer official-secret')
        self.assertEqual(headers['ChatGPT-Account-Id'], 'account-private')
        self.assertEqual(json.loads(raw), body)
        self.assertEqual(r.headers['x-codex-primary-used-percent'], '12')

    async def test_deepseek_isolates_auth_and_removes_foreign_reasoning(self):
        body = {'model': 'deepseek-v4-flash', 'service_tier': 'priority', 'previous_response_id': 'resp_official',
                'input': [{'type': 'reasoning', 'encrypted_content': 'opaque', 'summary': []},
                          {'type': 'message', 'role': 'user', 'content': 'hello'}]}
        r = await self.client.post('/v1/responses', json=body, headers=self.auth)
        self.assertEqual(r.status, 200)
        await r.read()
        path, headers, raw, _ = self.requests[-1]
        self.assertEqual(path, '/deepseek/responses')
        self.assertEqual(headers['Authorization'], 'Bearer ds-secret')
        self.assertNotIn('ChatGPT-Account-Id', headers)
        self.assertNotIn('Cookie', headers)
        self.assertNotIn(b'official-secret', raw)
        sent = json.loads(raw)
        self.assertNotIn('previous_response_id', sent)
        self.assertNotIn('service_tier', sent)
        self.assertEqual(sent['input'], [body['input'][1]])

    async def test_failed_checkpoint_export_does_not_send_lost_context_to_custom_provider(self):
        r = await self.client.post('/v1/responses', headers=self.auth,
            json={'model': 'deepseek-v4-flash', 'input': [{'type': 'compaction', 'encrypted_content': 'opaque'}]})
        self.assertEqual(r.status, 502)
        self.assertFalse(any(path.startswith('/deepseek/') for path, *_ in self.requests))

    async def test_compact_routes_by_model(self):
        for model, expected in [('deepseek-v4-flash', '/deepseek/responses/compact'), ('gpt-6-astra', '/official/responses/compact')]:
            r = await self.client.post('/v1/responses/compact', headers=self.auth, json={'model': model, 'input': []})
            await r.read()
            self.assertEqual(self.requests[-1][0], expected)

    async def test_no_auth_or_browser_origin_does_not_reach_upstream(self):
        for headers in ({}, {**self.auth, 'Origin': 'https://evil.invalid'}):
            r = await self.client.get('/v1/models', headers=headers)
            self.assertIn(r.status, (401, 403))
        self.assertEqual(self.requests, [])

    async def test_upstream_error_is_not_reported_as_success(self):
        self.status = 429
        r = await self.client.post('/v1/responses', headers=self.auth, json={'model': 'gpt-6-astra'})
        self.assertEqual(r.status, 429)

    async def test_health_and_unsupported_routes(self):
        r = await self.client.get('/health')
        self.assertEqual(await r.json(), {'status': 'ok'})
        r = await self.client.get('/v1/responses', headers=self.auth)
        self.assertEqual(r.status, 426)
        r = await self.client.post('/arbitrary', headers=self.auth)
        self.assertEqual(r.status, 404)

    async def test_stream_is_delivered_before_upstream_finishes(self):
        try:
            r = await self.client.post('/v1/responses', headers=self.auth,
                                      json={'model': 'gpt-6-astra', 'test_stream': True})
            first = await asyncio.wait_for(r.content.readuntil(b'\n\n'), 1)
            self.assertEqual(first, b'data: first\n\n')
        finally:
            self.stream_release.set()
        self.assertIn(b'[DONE]', await r.read())

    async def test_empty_and_malformed_requests_are_rejected(self):
        for body in (b'', b'{', b'[]', b'{}'):
            r = await self.client.post('/v1/responses', headers=self.auth, data=body)
            self.assertEqual(r.status, 400)
        self.assertEqual(self.requests, [])

    async def test_upstream_cache_validator_is_removed(self):
        r = await self.client.get('/v1/models', headers={**self.auth, 'If-None-Match': 'old'})
        await r.read()
        self.assertNotIn('If-None-Match', self.requests[-1][1])

    async def test_deepseek_response_cannot_overwrite_official_quota_headers(self):
        r = await self.client.post('/v1/responses', headers=self.auth,
                                  json={'model': 'deepseek-v4-flash', 'input': []})
        await r.read()
        self.assertNotIn('x-codex-primary-used-percent', r.headers)

    async def test_codex_zstd_request_is_decoded_before_deepseek_routing(self):
        import zstandard
        body = zstandard.ZstdCompressor().compress(json.dumps({'model': 'deepseek-v4-flash', 'input': []}).encode())
        r = await self.client.post('/v1/responses', data=body,
                                  headers={**self.auth, 'Content-Encoding': 'zstd'})
        self.assertEqual(r.status, 200)
        await r.read()
        self.assertEqual(self.requests[-1][0], '/deepseek/responses')
        self.assertNotIn('Content-Encoding', self.requests[-1][1])

    async def test_catalog_derives_release_version_from_codex_user_agent(self):
        r = await self.client.get('/v1/models', headers={**self.auth, 'User-Agent': 'codex_cli_rs/0.155.1-alpha.2 (Linux)'})
        await r.read()
        self.assertEqual(self.requests[-1][3], 'client_version=0.155.1')

    async def test_completed_sse_survives_upstream_unclean_close(self):
        r = await self.client.post('/v1/responses', headers=self.auth,
                                  json={'model': 'gpt-6-astra', 'test_reset': 'complete'})
        self.assertEqual(await r.read(), b'data: [DONE]\n\n')

    async def test_incomplete_sse_is_not_falsely_completed(self):
        r = await self.client.post('/v1/responses', headers=self.auth,
                                  json={'model': 'gpt-6-astra', 'test_reset': 'partial'})
        with self.assertRaises(aiohttp.ClientPayloadError):
            await r.read()

    async def test_switch_back_scrubs_foreign_reasoning_but_keeps_native_encryption(self):
        native = {'id': 'rs_native', 'type': 'reasoning', 'encrypted_content': 'official-opaque', 'summary': []}
        foreign = {'id': 'rs_foreign', 'type': 'reasoning', 'content': [{'type': 'reasoning_text', 'text': 'private reasoning'}], 'summary': []}
        message = {'id': 'msg_foreign', 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'ORCHID_472'}]}
        r = await self.client.post('/v1/responses', headers=self.auth,
            json={'model': 'gpt-6-astra', 'previous_response_id': 'resp_foreign', 'input': [native, foreign, message]})
        await r.read()
        sent = json.loads(self.requests[-1][2])
        self.assertNotIn('previous_response_id', sent)
        self.assertEqual(len(sent['input']), 2)
        self.assertEqual(sent['input'][0]['encrypted_content'], 'official-opaque')
        self.assertNotIn('id', sent['input'][0])
        self.assertEqual(sent['input'][1]['content'], message['content'])
        self.assertNotIn('id', sent['input'][1])

    async def test_native_only_reasoning_is_forwarded_unchanged(self):
        body = {'model': 'gpt-6-astra', 'input': [{'id': 'rs_native', 'type': 'reasoning', 'encrypted_content': 'opaque', 'summary': []}]}
        r = await self.client.post('/v1/responses', headers=self.auth, json=body)
        await r.read()
        self.assertEqual(json.loads(self.requests[-1][2]), body)


if __name__ == '__main__':
    unittest.main()
