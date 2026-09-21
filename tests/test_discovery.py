import asyncio
import json
from pathlib import Path
import tempfile
import sys
import fcntl
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient
import codex_model_bridge as bridge

try:
    from codex_model_bridge.discovery import refresh, load_cached_settings
except ImportError:
    refresh = load_cached_settings = None


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(refresh, 'provider discovery is not implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.cache = self.home / 'cache/models.json'
        self.calls = []
        self.fail_beta = False
        self.alpha_models = [{'id': 'fast', 'context_window': 64000, 'supports_images': True}]

        async def upstream(request):
            self.calls.append((request.path, request.headers.get('Authorization')))
            if request.path == '/beta/models' and self.fail_beta:
                return web.json_response({'error': 'contains-private-data'}, status=503)
            if request.path.endswith('/models'):
                rows = self.alpha_models if request.path.startswith('/alpha') else [{'id': 'fast'}]
                return web.json_response({'data': rows})
            data = await request.json()
            return web.json_response(data)

        app = web.Application()
        app.router.add_route('*', '/{path:.*}', upstream)
        self.server = TestServer(app)
        await self.server.start_server()
        self.write_config()

    def write_config(self, token='alpha-secret', beta=True, changed=False):
        data = f'''[model_providers.alpha]
base_url = "{self.server.make_url('/changed' if changed else '/alpha')}"
wire_api = "responses"
experimental_bearer_token = "{token}"
'''
        if beta:
            data += f'''[model_providers.beta]
base_url = "{self.server.make_url('/beta')}"
wire_api = "responses"
experimental_bearer_token = "beta-secret"
'''
        (self.home / 'config.toml').write_text(data)

    async def asyncTearDown(self):
        if hasattr(self, 'server'):
            await self.server.close()
        if hasattr(self, 'temp'):
            self.temp.cleanup()

    async def test_refresh_discovers_without_profiles_and_cache_contains_no_credentials(self):
        report = await refresh(self.home, self.cache)
        self.assertEqual(report['alpha']['status'], 'updated')
        self.assertEqual(set(self.calls), {('/alpha/models', 'Bearer alpha-secret'), ('/beta/models', 'Bearer beta-secret')})
        cached = self.cache.read_text()
        self.assertNotIn('alpha-secret', cached)
        self.assertNotIn('beta-secret', cached)
        settings = load_cached_settings(self.home, self.cache)
        self.assertEqual([r.model['slug'] for r in settings.routes], ['alpha/fast', 'beta/fast'])
        self.assertEqual(settings.routes[0].model['context_window'], 64000)
        self.assertEqual(settings.routes[0].model['input_modalities'], ['text', 'image'])
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)

    async def test_failure_preserves_only_failed_provider_while_success_updates(self):
        await refresh(self.home, self.cache)
        self.alpha_models.append({'id': 'large'})
        self.fail_beta = True
        report = await refresh(self.home, self.cache)
        self.assertEqual(report['beta']['status'], 'retained')
        self.assertNotIn('contains-private-data', str(report) + self.cache.read_text())
        self.assertEqual([r.model['slug'] for r in load_cached_settings(self.home, self.cache).routes],
                         ['alpha/fast', 'alpha/large', 'beta/fast'])

    async def test_startup_is_offline_credentials_are_current_and_removed_providers_disappear(self):
        await refresh(self.home, self.cache)
        self.write_config(token='rotated-secret', beta=False)
        calls = len(self.calls)
        settings = load_cached_settings(self.home, self.cache)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(len(settings.routes), 1)
        self.assertEqual(settings.routes[0].token, 'rotated-secret')

    async def test_changed_endpoint_does_not_reuse_old_model_list(self):
        await refresh(self.home, self.cache)
        self.write_config(changed=True)
        settings = load_cached_settings(self.home, self.cache)
        self.assertEqual([r.model['slug'] for r in settings.routes], ['beta/fast'])

    async def test_missing_cache_does_not_prevent_official_only_start(self):
        settings = load_cached_settings(self.home, self.cache)
        self.assertEqual(settings.routes, [])

    async def test_successful_empty_response_removes_old_models(self):
        await refresh(self.home, self.cache)
        self.alpha_models = []
        await refresh(self.home, self.cache)
        self.assertEqual([r.model['slug'] for r in load_cached_settings(self.home, self.cache).routes], ['beta/fast'])

    async def test_existing_profile_catalog_is_an_optional_metadata_seed(self):
        (self.home / 'custom.config.toml').write_text('model="fast"\nmodel_provider="alpha"\nmodel_catalog_json="catalog.json"\n')
        (self.home / 'catalog.json').write_text(json.dumps({'models': [{'slug': 'fast', 'tool_mode': None,
                'context_window': 100000, 'default_reasoning_level': 'high',
                'supported_reasoning_levels': [{'effort': 'high', 'description': 'Known model'}]}]}))
        await refresh(self.home, self.cache)
        (self.home / 'custom.config.toml').unlink()
        (self.home / 'catalog.json').unlink()
        route = load_cached_settings(self.home, self.cache).routes[0]
        self.assertEqual(route.model['default_reasoning_level'], 'high')
        self.assertTrue(route.reasoning_passthrough)

    async def test_generated_metadata_inherits_schema_but_not_native_capabilities(self):
        await refresh(self.home, self.cache)
        settings = load_cached_settings(self.home, self.cache)
        native = {'slug': 'gpt-future', 'visibility': 'list', 'base_instructions': 'official template',
                  'tool_mode': 'code_mode_only', 'use_responses_lite': True, 'supports_search_tool': True,
                  'context_window': 999999, 'comp_hash': 'native-hash'}
        async def official(request):
            return web.json_response({'models': [native]})
        app = web.Application()
        app.router.add_get('/models', official)
        async with TestServer(app) as server:
            settings.official_url = str(server.make_url('')).rstrip('/')
            async with TestClient(TestServer(bridge.create_app(settings))) as client:
                r = await client.get('/v1/models', headers={'Authorization': 'Bearer official-secret'})
                self.assertEqual(r.status, 200)
                data = await r.json()
                self.assertEqual(data['models'][0], native)
                model = data['models'][1]
                self.assertEqual(model['base_instructions'], 'official template')
                self.assertIsNone(model['tool_mode'])
                self.assertFalse(model['use_responses_lite'])
                self.assertFalse(model['supports_search_tool'])
                self.assertNotIn('comp_hash', model)

    async def test_unknown_reasoning_uses_upstream_default_instead_of_gpt_effort(self):
        await refresh(self.home, self.cache)
        settings = load_cached_settings(self.home, self.cache)
        async with TestClient(TestServer(bridge.create_app(settings))) as client:
            r = await client.post('/v1/responses', headers={'Authorization': 'Bearer official-secret'},
                    json={'model': 'alpha/fast', 'reasoning': {'effort': 'ultra'}, 'input': []})
            body = await r.json()
            self.assertEqual(body['model'], 'fast')
            self.assertNotIn('reasoning', body)

    async def test_unverified_models_default_to_high_without_forwarding_effort(self):
        await refresh(self.home, self.cache)
        route = load_cached_settings(self.home, self.cache).routes[0]
        self.assertEqual(route.model['default_reasoning_level'], 'high')
        self.assertEqual([level['effort'] for level in route.model['supported_reasoning_levels']], ['high'])
        self.assertEqual(route.model['supported_reasoning_levels'][0]['description'],
                         'Use upstream default; capabilities unverified')
        self.assertFalse(route.reasoning_passthrough)

    async def test_provider_auth_command_is_used_without_storing_credential(self):
        config = (self.home / 'config.toml').read_text().replace('experimental_bearer_token = "alpha-secret"',
            'auth = { command = '+json.dumps(sys.executable)+', args = ["-c", "print(\'helper-secret\')"] }')
        (self.home / 'config.toml').write_text(config)
        report = await refresh(self.home, self.cache)
        self.assertEqual(report['alpha']['status'], 'updated')
        self.assertIn(('/alpha/models', 'Bearer helper-secret'), self.calls)
        self.assertNotIn('helper-secret', self.cache.read_text())
        self.assertEqual(load_cached_settings(self.home, self.cache).routes[0].token, 'helper-secret')

    async def test_invalid_or_duplicate_list_retains_last_good_provider(self):
        await refresh(self.home, self.cache)
        self.alpha_models = [{'id': 'fast'}, {'id': 'fast'}]
        report = await refresh(self.home, self.cache)
        self.assertEqual(report['alpha']['status'], 'retained')
        self.assertEqual(len(load_cached_settings(self.home, self.cache).routes), 2)

    async def test_concurrent_refresh_is_rejected_without_changing_cache(self):
        await refresh(self.home, self.cache)
        before = self.cache.read_bytes()
        with open(str(self.cache) + '.lock', 'r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'already running'):
                await refresh(self.home, self.cache)
        self.assertEqual(before, self.cache.read_bytes())

    async def test_refresh_cli_and_default_cache_loader(self):
        p = await asyncio.create_subprocess_exec(sys.executable, '-m', 'codex_model_bridge', 'refresh',
            '--codex-home', str(self.home), '--cache', str(self.cache),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await p.communicate()
        self.assertEqual(p.returncode, 0, err.decode())
        self.assertNotIn(b'alpha-secret', out + err)
        self.assertEqual(len(bridge.load_settings(self.home, cache_path=self.cache).routes), 2)


if __name__ == '__main__':
    unittest.main()
