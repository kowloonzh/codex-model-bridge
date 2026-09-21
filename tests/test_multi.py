import json
import inspect
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import codex_model_bridge as bridge


class MultiConfigTests(unittest.TestCase):
    def setUp(self):
        self.assertIn('config_path', inspect.signature(bridge.load_settings).parameters,
                      'profile-free route configuration is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / 'config.toml').write_text('''
[model_providers.alpha]
base_url = "http://localhost:9001/v1"
wire_api = "responses"
experimental_bearer_token = "alpha-secret"
[model_providers.beta]
base_url = "http://localhost:9002/v1"
wire_api = "responses"
env_key = "BRIDGE_TEST_BETA_TOKEN"
''')
        self.config = self.home / 'bridge.toml'
        self.config.write_text('''
[[models]]
slug = "alpha/fast"
provider = "alpha"
model = "upstream-fast"
catalog = "catalog.json"
[[models]]
slug = "alpha/large"
provider = "alpha"
model = "upstream-large"
catalog = "catalog.json"
[[models]]
slug = "beta/fast"
provider = "beta"
model = "vendor/fast"
catalog = "catalog.json"
catalog_model = "upstream-fast"
display_name = "Fast via Beta"
''')
        (self.home / 'catalog.json').write_text(json.dumps({'models': [
            {'slug': 'upstream-fast', 'display_name': 'Fast', 'tool_mode': None, 'context_window': 100000},
            {'slug': 'upstream-large', 'display_name': 'Large', 'tool_mode': 'code_mode_only', 'context_window': 200000}]}))
        self.env = patch.dict(os.environ, {'BRIDGE_TEST_BETA_TOKEN': 'beta-secret'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_multiple_models_providers_without_any_profile_file(self):
        settings = bridge.load_settings(self.home, config_path=self.config)
        self.assertEqual(len(settings.routes), 3)
        self.assertEqual([r.model['slug'] for r in settings.routes], ['alpha/fast', 'alpha/large', 'beta/fast'])
        self.assertEqual(settings.routes[2].upstream_model, 'vendor/fast')
        self.assertEqual(settings.routes[2].token, 'beta-secret')
        self.assertEqual(settings.routes[2].model['display_name'], 'Fast via Beta')
        self.assertEqual(settings.routes[1].model['tool_mode'], 'code_mode_only')
        self.assertNotIn('beta-secret', repr(settings))
        self.assertFalse(list(self.home.glob('*.config.toml')))

    def test_explicit_legacy_config_location_needs_no_profile(self):
        config_home = self.home / 'xdg'
        target = config_home / 'codex-model-bridge'
        target.mkdir(parents=True)
        (target / 'config.toml').write_text(self.config.read_text().replace('catalog.json', str(self.home / 'catalog.json')))
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config_home)}):
            self.assertEqual(len(bridge.load_settings(self.home, config_path=target / 'config.toml').routes), 3)

    def test_duplicate_alias_is_rejected(self):
        self.config.write_text(self.config.read_text().replace('beta/fast', 'alpha/fast'))
        with self.assertRaisesRegex(ValueError, 'duplicate|Duplicate'):
            bridge.load_settings(self.home, config_path=self.config)

    def test_bad_provider_missing_metadata_and_unknown_fields_are_rejected(self):
        original = self.config.read_text()
        for replacement in [original.replace('provider = "beta"', 'provider = "missing"'),
                            original.replace('catalog_model = "upstream-fast"', 'catalog_model = "absent"'),
                            original + 'modle = "typo"\n', 'models = []\n']:
            with self.subTest(config=replacement):
                self.config.write_text(replacement)
                with self.assertRaises(ValueError):
                    bridge.load_settings(self.home, config_path=self.config)

    def test_missing_environment_credential_is_rejected(self):
        with patch.dict(os.environ, {'BRIDGE_TEST_BETA_TOKEN': ''}):
            with self.assertRaises(ValueError):
                bridge.load_settings(self.home, config_path=self.config)

    def test_cli_check_accepts_routes_file_without_profile(self):
        result = subprocess.run([sys.executable, '-m', 'codex_model_bridge', '--codex-home', str(self.home),
                                 '--config', str(self.config), '--check'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('beta/fast', result.stdout)
        self.assertNotIn('beta-secret', result.stdout + result.stderr)


class MultiRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(hasattr(bridge, 'Route'), 'multiple routes are not implemented')
        self.requests = []
        async def upstream(request):
            if request.path.endswith('/models'):
                return web.json_response({'models': [{'slug': 'gpt-official', 'visibility': 'list'}]})
            data = await request.json()
            self.requests.append((request.path, request.headers.get('Authorization'), data, dict(request.headers)))
            return web.json_response({'model': data['model']})
        app = web.Application()
        app.router.add_route('*', '/{path:.*}', upstream)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        self.settings = bridge.Settings(routes=[
            bridge.Route(str(self.upstream.make_url('/alpha')), 'secret-a', {'slug': 'alpha/fast'}, 'fast'),
            bridge.Route(str(self.upstream.make_url('/alpha')), 'secret-a', {'slug': 'alpha/large'}, 'large'),
            bridge.Route(str(self.upstream.make_url('/beta')), 'secret-b', {'slug': 'beta/fast'}, 'fast'),
        ], official_url=str(self.upstream.make_url('/official')))
        self.client = TestClient(TestServer(bridge.create_app(self.settings)))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer official-secret', 'ChatGPT-Account-Id': 'account'}

    async def asyncTearDown(self):
        if hasattr(self, 'client'):
            await self.client.close()
        if hasattr(self, 'upstream'):
            await self.upstream.close()

    async def test_all_aliases_are_published(self):
        r = await self.client.get('/v1/models', headers=self.headers)
        self.assertEqual([m['slug'] for m in (await r.json())['models']],
                         ['gpt-official', 'alpha/fast', 'alpha/large', 'beta/fast'])

    async def test_alias_routes_to_correct_provider_auth_and_upstream_model(self):
        for alias, provider, model, token in [('alpha/fast', 'alpha', 'fast', 'secret-a'),
                                             ('alpha/large', 'alpha', 'large', 'secret-a'),
                                             ('beta/fast', 'beta', 'fast', 'secret-b')]:
            for endpoint in ('responses', 'responses/compact'):
                r = await self.client.post('/v1/' + endpoint, headers=self.headers,
                                           json={'model': alias, 'input': []})
                self.assertEqual(r.status, 200)
                await r.read()
                path, auth, body, headers = self.requests[-1]
                self.assertEqual(path, '/' + provider + '/' + endpoint)
                self.assertEqual(auth, 'Bearer ' + token)
                self.assertEqual(body['model'], model)
                self.assertNotIn('ChatGPT-Account-Id', headers)

    async def test_official_models_remain_on_official_upstream(self):
        r = await self.client.post('/v1/responses', headers=self.headers, json={'model': 'gpt-official'})
        await r.read()
        self.assertEqual(self.requests[-1][:2], ('/official/responses', 'Bearer official-secret'))

    async def test_official_slug_collision_fails_catalog(self):
        self.settings.routes[0].model['slug'] = 'gpt-official'
        r = await self.client.get('/v1/models', headers=self.headers)
        self.assertEqual(r.status, 502)


if __name__ == '__main__':
    unittest.main()
