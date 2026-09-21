# codex-model-bridge

[简体中文](README.md) | **English**

A lightweight local proxy for Codex. Keep official sign-in, the official model catalog,
and account quota queries while switching between official and third-party models with `/model`.

Configure providers in Codex's own `config.toml`. The bridge discovers models and generates
a cache; separate profiles, routing tables, and handwritten model catalog JSON are not required.

## How it works

```text
model_providers in Codex config.toml
    │ manual refresh: query each provider's /models
    ▼
Generated third-party model cache (no credentials)
    │ loaded when the service starts
    ▼
Codex → 127.0.0.1:17843
    ├─ Official catalog: fetched dynamically, then extended with cached models
    ├─ Official requests: forwarded with official authentication
    └─ provider/model-id: routed using that provider's credentials and upstream ID
```

Initialize the third-party catalog once and refresh it when needed. Official models are still
fetched dynamically; new official models do not require refreshing the third-party cache.
Quota information such as `weekly left` always refers to the official account, not third-party balances.

## Requirements

- Python 3.11+ with `venv` and `pip`.
- Linux / POSIX. File locking currently requires `fcntl`; native Windows is not supported.
- Codex CLI, already signed in with a ChatGPT account. Existing authentication can be reused.
- Third-party upstreams exposing both Responses and model discovery endpoints:
  `<base_url>/responses` and `<base_url>/models`.
  This project does not translate Chat Completions or Anthropic Messages protocols.

The current verified environment is Python 3.14.3 and Codex CLI 0.155.1. Other versions need validation.

## Quick start

### 1. Install

```sh
git clone https://github.com/kowloonzh/codex-model-bridge.git
cd codex-model-bridge
python3 -m venv .venv
.venv/bin/python -m pip install .
codex login status
```

If you are not signed in, run `codex login` and choose ChatGPT authentication.

### 2. Configure a third-party provider

Add a provider to `~/.codex/config.toml`. Replace the example URL with your actual service:

```toml
[model_providers.my_provider]
name = "My provider"
base_url = "https://provider.example/v1"
wire_api = "responses"
env_key = "MY_PROVIDER_API_KEY"
```

Set the credential in the terminal that will run the bridge:

```sh
export MY_PROVIDER_API_KEY='your-api-key'
```

Provider names are up to you. Add more `[model_providers.<name>]` sections for more providers.
Keep Codex's top-level provider on the built-in `openai` provider with official authentication;
do not select a third-party provider there or set a static `model_catalog_json`.

### 3. Discover models and start the bridge

```sh
.venv/bin/codex-model-bridge refresh
.venv/bin/codex-model-bridge --check
.venv/bin/codex-model-bridge
```

The default listener is `127.0.0.1:17843`. The last command runs in the foreground;
leave that terminal open. `--check` validates local configuration and cache loading, not inference.

### 4. Start Codex

In another terminal:

```sh
codex -c 'openai_base_url="http://127.0.0.1:17843/v1"'
```

Use `/model` to select an official model or `my_provider/<upstream-model-id>`.
The same model from different providers appears as separate entries.

To make this the default, place this line at the **top level of `~/.codex/config.toml`, before any table headers**:

```toml
openai_base_url = "http://127.0.0.1:17843/v1"
```

You can then launch `codex` normally. A profile that selects a third-party provider may bypass the bridge.
Remove this line to restore the original official endpoint; stopping the bridge while leaving the line
in place will leave Codex unable to connect.

## Linux systemd user service

Run these commands from the cloned project directory. Install into a separate runtime directory
so the service does not depend on a development virtual environment:

```sh
mkdir -p ~/.local/share/codex-model-bridge ~/.local/bin ~/.config/systemd/user
python3 -m venv ~/.local/share/codex-model-bridge/venv
~/.local/share/codex-model-bridge/venv/bin/python -m pip install .
ln -s ~/.local/share/codex-model-bridge/venv/bin/codex-model-bridge ~/.local/bin/codex-model-bridge
export PATH="$HOME/.local/bin:$PATH"
```

If the command link already exists, inspect its target before replacing another installation.

Save the following as `~/.config/systemd/user/codex-model-bridge.service`:

```ini
[Unit]
Description=Codex Model Bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.local/share/codex-model-bridge
ExecStart=%h/.local/share/codex-model-bridge/venv/bin/codex-model-bridge --codex-home %h/.codex
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-%h/.config/codex-model-bridge/environment
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
UMask=0077

[Install]
WantedBy=default.target
```

When using `env_key`, create an environment file for the service:

```sh
mkdir -p ~/.config/codex-model-bridge
touch ~/.config/codex-model-bridge/environment
chmod 600 ~/.config/codex-model-bridge/environment
```

Edit that file, for example:

```ini
MY_PROVIDER_API_KEY=your-api-key
# Add your actual HTTPS_PROXY, HTTP_PROXY and NO_PROXY values if needed.
```

Variables exported in your terminal are not automatically inherited by systemd. Both the refresh
command and the service need the provider credentials. The service reads this file; when running
`refresh` in a terminal, export the corresponding variables there as well.

Start the service after stopping any foreground bridge on the same port:

```sh
codex-model-bridge refresh
systemctl --user daemon-reload
systemctl --user enable --now codex-model-bridge
systemctl --user status codex-model-bridge
```

To keep the user service running after logout or start it at boot, use `loginctl enable-linger "$USER"`.
Whether this requires administrator privileges depends on your system's policy.

## Everyday operations

After adding or removing a provider, or when you want the latest upstream model list:

```sh
codex-model-bridge refresh
systemctl --user restart codex-model-bridge
```

For foreground use, restart the foreground process instead. Codex may cache its model list;
restart Codex to reload it.

```sh
curl http://127.0.0.1:17843/health
journalctl --user -u codex-model-bridge -f
systemctl --user stop codex-model-bridge
```

To upgrade, run the runtime environment's `pip install .` from a checkout containing the new code,
then restart the service.

## Configuration and cache

| Data | Default location |
|---|---|
| Provider endpoints and authentication | `~/.codex/config.toml` |
| Generated third-party catalog | `~/.cache/codex-model-bridge/models.json` |
| Optional systemd environment file | `~/.config/codex-model-bridge/environment` |

`CODEX_HOME` overrides the Codex directory; `XDG_CACHE_HOME` overrides the cache root.
The cache is generated data, not a user-maintained configuration. The old bridge routing file
is not read by default.

```sh
codex-model-bridge refresh --codex-home PATH --cache PATH
codex-model-bridge --cache PATH --port 17843
codex-model-bridge --check
```

Supported authentication options are `experimental_bearer_token`, `env_key`, and
`auth.command` / `auth.args`, along with `http_headers` / `env_http_headers`.
Authentication commands come from your Codex configuration, run without an extra shell wrapper,
and must print a single-line token within 10 seconds. They run during refresh and service startup.
Credentials are never written to the model cache and are not automatically rotated while the service
is running; restart the service after changing credentials.

Legacy `--config PATH` (manual routes) and `--profile NAME` (one default model) remain available
explicitly, but are unnecessary for discovery and cannot be combined with `refresh`.

## Discovery behavior and limitations

- Providers refresh independently. Timeouts, authentication failures, or invalid responses retain
  the last successful cache for the same endpoint. A successful empty list clears the old entries.
- Results are `updated`, `retained`, or `skipped`. Exit code is 0 if at least one provider updated,
  otherwise 1.
- Removing a provider removes it on restart. Changing its endpoint or protocol invalidates its cache
  and requires a refresh.
- File locking and atomic replacement protect the cache; its permissions are 600. With no cache,
  the bridge can still serve official models only.
- Model names are prefixed by provider. Old unprefixed aliases are not preserved automatically;
  select a new model entry in existing conversations.
- Matching catalogs referenced by existing profiles are optional metadata seeds. Full upstream
  catalogs can also be reused. Other models use a compatibility template, completed with the official
  catalog schema when returned to Codex. Ordinary `/models` lists often omit capability information.
- **Discovery does not prove capability compatibility.** The fallback assumes text input and uses a
  32768-token client budget if the upstream provides no window size; that is not a guarantee of actual
  context capacity. Unknown reasoning settings are omitted so the upstream uses its own default.
- **Known vision metadata gap:** a model may support images but still be advertised as text-only if
  `/models` omits that capability. Refreshing the same list cannot fix this; verified metadata or a
  subsequent adapter improvement is needed.
- Entries explicitly typed as embedding, image, audio, or rerank are not published as chat models.
  Entries without type information still require testing.
- HTTP SSE, zstd requests, and authentication isolation are supported; WebSockets are not.
  When switching to a custom model, official encrypted compaction checkpoints are first exported
  as readable handoff summaries by an official model using the original official credentials.
  Local conversation history is not edited; switching back to an official model retains the original
  checkpoint. The current user message is not sent to the summary export request.
- The first conversion of each checkpoint adds latency and consumes official quota. Summaries are
  cached only in memory, scoped to the official account and credentials. Concurrent requests share
  one export; restarting the service clears the cache. This is a model-generated handoff, not a
  verbatim decryption, so a second summarization can lose detail. Invalid authentication, exhausted
  quota, checkpoints from another backend/account, or incomplete exports fail explicitly rather
  than silently forwarding history with the checkpoint removed.
- Third-party remote compaction v2 (automatic compaction and `/compact`) runs a separate summary-only
  request with tools disabled. Only a complete, valid summary produces one `compaction` item.
  The `cmb1:` checkpoint is versioned and self-contained; its `encrypted_content` field contains
  encoded text, not encryption. Codex persists it in conversation history. The bridge restores it
  before requests to either official or third-party models, including after service restarts,
  without an additional official export call. Checkpoints created by 0.5.0 require a bridge version
  that understands this format; older versions cannot replay them. Failures, tool calls, empty or
  oversized summaries, and cancellation do not publish a new checkpoint. Summaries are not lossless
  archives. Legacy `/responses/compact` v1 forwarding is outside this v2 adaptation.
- Official account quotas remain separate from third-party billing. The bridge honors
  `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`. It listens only on loopback and is intended for
  trusted local users, not as a multi-user authentication gateway.

## Tests

Local tests do not call real models:

```sh
.venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=. .venv/bin/python scripts/smoke_remote_compaction.py
```

Integration checks require real authentication and a refreshed cache:

```sh
PYTHONPATH=. .venv/bin/python scripts/smoke_live.py --metadata-only
PYTHONPATH=. .venv/bin/python scripts/smoke_live.py --model my_provider/model-id
PYTHONPATH=. .venv/bin/python scripts/smoke_remote_compaction.py --live --model my_provider/model-id
```

The full smoke test consumes real quota. It uses temporary configuration to check the catalog,
weekly quota window, a real `pwd` tool call, and round-trip model switching within one thread.
The test Codex process uses `danger-full-access` to avoid the test host's bwrap restriction and is
instructed to run only `pwd`. It does not change normal configuration, verifies configuration/auth/cache
hashes afterward, and removes temporary credentials. See the [verification record](docs/verification.md)
for the scope actually tested.

By default, `smoke_remote_compaction.py` uses a local mock upstream and temporary Codex configuration
to exercise automatic/manual compaction, repeated compaction, restart/resume, and the official route.
It makes no real inference calls, but requires Codex, existing authentication and a model catalog cache.
`--live` uses real models for manual compaction and retained-fact checks in a separate synthetic thread;
it does not execute work from existing conversations.

## Acknowledgments

The dynamic catalog template, official forwarding, and transport compatibility work reference
[codex-chatgpt-web](https://github.com/miuuyy/codex-chatgpt-web).
See [third-party notices](THIRD_PARTY_NOTICES.md) for the source revision and upstream MIT license.
