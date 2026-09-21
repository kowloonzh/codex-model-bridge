# codex-model-bridge

**简体中文** | [English](README.en.md)

一个轻量的 Codex 本地代理。在保留官方登录、官方模型列表和账户额度查询的同时，
通过 `/model` 在官方模型与第三方模型之间切换。

第三方 provider 只需配置在 Codex 的 `config.toml` 中。bridge 自动发现模型并生成缓存，
不要求额外维护 profile、路由表或模型目录 JSON。

## 工作方式

```text
Codex config.toml 中的 model_providers
    │ 手动 refresh：查询每个 provider 的 /models
    ▼
生成的第三方模型缓存（不保存凭据）
    │ 服务启动时加载
    ▼
Codex → 127.0.0.1:17843
    ├─ 官方模型目录：动态获取，再追加第三方模型
    ├─ 官方模型请求：使用官方认证转发
    └─ provider/model-id：使用对应 provider 的认证和真实模型 ID
```

第三方模型列表初始化一次即可，需要更新时手动刷新。官方模型列表仍动态获取，
不必为了官方新增模型刷新第三方缓存。`weekly left` 等额度始终代表官方账户，
不代表第三方 provider 的余额或额度。

## 前提条件

- Python 3.11+，支持 `venv` 和 `pip`。
- Linux / POSIX 环境；当前文件锁实现依赖 `fcntl`，不支持原生 Windows。
- 已安装 Codex CLI，并使用 ChatGPT 账户登录。已有登录无需重复操作。
- 第三方上游同时提供 Responses API 和模型列表接口：`<base_url>/responses`、`<base_url>/models`。
  本项目不转换 Chat Completions 或 Anthropic Messages 协议。

当前验证环境为 Python 3.14.3、Codex CLI 0.155.1。其他版本的兼容性需要实际验证。

## 快速开始

### 1. 安装

```sh
git clone https://github.com/kowloonzh/codex-model-bridge.git
cd codex-model-bridge
python3 -m venv .venv
.venv/bin/python -m pip install .
codex login status
```

如果尚未登录，运行 `codex login`，选择 ChatGPT 登录。

### 2. 配置第三方 provider

编辑 `~/.codex/config.toml`，添加 provider。地址和模型取决于你的实际服务：

```toml
[model_providers.my_provider]
name = "My provider"
base_url = "https://provider.example/v1"
wire_api = "responses"
env_key = "MY_PROVIDER_API_KEY"
```

在运行 bridge 的终端设置相应凭据：

```sh
export MY_PROVIDER_API_KEY='your-api-key'
```

provider 名称可自定义。多个 provider 就添加多个 `[model_providers.<name>]`。
不要把 Codex 顶层 `model_provider` 改为第三方 provider；客户端需要保持内置 `openai`
provider 和官方登录，也不要设置静态 `model_catalog_json`。

### 3. 初始化模型列表并启动代理

```sh
.venv/bin/codex-model-bridge refresh
.venv/bin/codex-model-bridge --check
.venv/bin/codex-model-bridge
```

默认监听 `127.0.0.1:17843`。最后一条命令在前台运行，保持该终端打开。
`--check` 只验证本地配置和缓存，不验证实际推理能力。

### 4. 启动 Codex

另开终端：

```sh
codex -c 'openai_base_url="http://127.0.0.1:17843/v1"'
```

输入 `/model`，选择官方模型或 `my_provider/<上游模型 ID>`。
同一个模型由不同 provider 提供时，会显示为不同条目。

如果要设为默认入口，把下面这一行写入 `~/.codex/config.toml` 的**顶层、所有 `[表名]` 之前**：

```toml
openai_base_url = "http://127.0.0.1:17843/v1"
```

以后直接运行 `codex`。不要再用第三方 profile 启动客户端，否则可能绕过 bridge。
恢复官方默认入口时，删除这行；仅停止代理而保留这行，会导致 Codex 无法连接。

## Linux systemd 用户服务

以下命令在克隆的项目目录执行。将程序安装到独立运行目录，避免服务依赖开发虚拟环境：

```sh
mkdir -p ~/.local/share/codex-model-bridge ~/.local/bin ~/.config/systemd/user
python3 -m venv ~/.local/share/codex-model-bridge/venv
~/.local/share/codex-model-bridge/venv/bin/python -m pip install .
ln -s ~/.local/share/codex-model-bridge/venv/bin/codex-model-bridge ~/.local/bin/codex-model-bridge
export PATH="$HOME/.local/bin:$PATH"
```

如果命令链接已存在，请先核对它的目标，不要覆盖其他安装。

保存以下内容为 `~/.config/systemd/user/codex-model-bridge.service`：

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

若使用 `env_key`，为服务创建环境文件：

```sh
mkdir -p ~/.config/codex-model-bridge
touch ~/.config/codex-model-bridge/environment
chmod 600 ~/.config/codex-model-bridge/environment
```

编辑该文件，例如：

```ini
MY_PROVIDER_API_KEY=your-api-key
# 如需网络代理，再添加实际的 HTTPS_PROXY、HTTP_PROXY、NO_PROXY。
```

终端中的 `export` 不会自动传给 systemd。刷新命令和服务都必须能读取 provider 凭据；
上述环境文件由服务读取，终端执行 `refresh` 时仍需在终端设置对应环境变量。

启动服务（先停止占用同一端口的前台 bridge）：

```sh
codex-model-bridge refresh
systemctl --user daemon-reload
systemctl --user enable --now codex-model-bridge
systemctl --user status codex-model-bridge
```

如需退出登录后继续运行或在开机时启动用户服务，可执行 `loginctl enable-linger "$USER"`；
是否需要管理员权限取决于系统策略。

## 日常操作

新增、删除 provider，或希望同步上游模型变更时：

```sh
codex-model-bridge refresh
systemctl --user restart codex-model-bridge
```

前台运行方式则重新启动前台进程。Codex 自身可能缓存列表，重新启动 Codex 可加载新目录。

```sh
curl http://127.0.0.1:17843/health
journalctl --user -u codex-model-bridge -f
systemctl --user stop codex-model-bridge
```

升级时，在包含新代码的项目目录重新执行运行环境的 `pip install .`，然后重启服务。

## 配置与缓存

| 内容 | 默认位置 |
|---|---|
| provider 地址和认证 | `~/.codex/config.toml` |
| 自动生成的第三方模型缓存 | `~/.cache/codex-model-bridge/models.json` |
| 可选 systemd 环境文件 | `~/.config/codex-model-bridge/environment` |

`CODEX_HOME` 可改变 Codex 配置目录，`XDG_CACHE_HOME` 可改变缓存根目录。
缓存不是用户配置，无需编辑。默认不读取旧版 bridge 路由文件。

```sh
codex-model-bridge refresh --codex-home PATH --cache PATH
codex-model-bridge --cache PATH --port 17843
codex-model-bridge --check
```

支持 `experimental_bearer_token`、`env_key`、`auth.command` / `auth.args`，以及
`http_headers` / `env_http_headers`。认证命令来自你的 Codex 配置，无额外 shell 包装，
标准输出须为单行 token，10 秒超时；刷新和服务启动时会执行它。凭据不写入模型缓存，
运行期间也不会自动轮换，凭据变更后需重启服务。

旧的 `--config PATH`（手动路由）和 `--profile NAME`（单模型）仍可显式使用，
但默认自动发现流程不需要它们，也不能与 `refresh` 组合使用。

## 自动发现的行为与限制

- provider 独立刷新，超时、鉴权失败或非法列表会保留同地址上次成功的结果；成功空列表则清空旧条目。
- 报告状态为 `updated`、`retained` 或 `skipped`。至少一个 provider 成功时退出码为 0，否则为 1。
- 删除 provider 后重启即可停止发布；地址或协议变更会使旧缓存失效，需要刷新。
- 缓存使用文件锁和原子替换，权限 600。不缓存凭据，没有缓存时仍可仅提供官方模型。
- 名称自动加 provider 前缀。旧版无前缀别名不自动保留，已有会话需重新选择模型。
- 已有 profile 关联的匹配模型目录是可选元数据来源；上游完整目录也可复用。其余采用兼容模板，
  返回 Codex 时补齐官方目录 schema。普通 `/models` 往往不包含完整能力信息。
- **发现成功不等于能力已验证。** 兼容模板默认文本输入；缺少上下文信息时使用 32768 的客户端预算，
  这不保证是上游实际窗口。未知推理能力使用上游默认值，不发送猜测的推理等级。
- **识图元数据存在已知限制：** 上游支持图片但 `/models` 没有说明时，可能仍被标为文本模型。
  重新刷新同样的列表无法修正这一点，需要可信的能力元数据或后续适配。
- 上游明确标记为 embedding、image、audio、rerank 的条目不作为聊天模型发布；缺少类型信息时仍需实测。
- 支持 HTTP SSE、zstd 请求和认证隔离，不支持 WebSocket。切到第三方模型时，如果历史包含官方
  加密压缩检查点，会先使用原官方认证请求官方模型导出可读摘要，再继续第三方调用。
  不修改本地会话记录，切回官方时仍使用原检查点；当前消息不会发给摘要导出请求。
- 每个检查点首次转换会额外消耗官方额度并增加等待时间。摘要仅在内存中缓存，按官方账户和
  认证隔离；相同检查点的并发请求共用一次导出，重启服务会清空缓存。
  这是模型生成的可读交接摘要，不是逐字解密，可能有二次摘要的信息损失。
  官方认证失效、配额不足、检查点不属于该官方账户或导出未完整结束时会明确失败，
  不会丢掉检查点后继续向第三方发送不完整历史。
- 第三方模型的远程压缩 v2（自动压缩及 `/compact`）由 bridge 单独生成摘要：清除工具配置，
  完整成功后返回一个 `compaction` 项。摘要封装为 `cmb1:` 版本化、自包含检查点；虽使用
  `encrypted_content` 传输字段，内容只是编码，不是加密。检查点会保存到 Codex 会话历史中。
  后续发给官方或第三方模型前都由 bridge 还原为可读摘要，服务重启后也可恢复，不额外调用官方导出。
  0.5.0 生成的检查点需要支持该格式的 bridge 版本，旧版本无法读取。
  压缩失败、输出工具调用、摘要为空/超限或客户端取消时，不发布新检查点。摘要不是无损归档。
  旧版 `/responses/compact` v1 转发不在此次 v2 适配范围内。
- 保留官方账户额度查询，不统一第三方计费。遵循 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`。
  仅监听 loopback，面向可信本机用户，不是多用户认证网关。

## 测试

本地测试不调用真实模型：

```sh
.venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=. .venv/bin/python scripts/smoke_remote_compaction.py
```

集成检查需要真实登录和已经刷新的缓存：

```sh
PYTHONPATH=. .venv/bin/python scripts/smoke_live.py --metadata-only
PYTHONPATH=. .venv/bin/python scripts/smoke_live.py --model my_provider/model-id
PYTHONPATH=. .venv/bin/python scripts/smoke_remote_compaction.py --live --model my_provider/model-id
```

完整 smoke 消耗实际额度，使用临时配置检查模型目录、weekly 窗口、真实 `pwd` 和同会话往返切换。
测试子 Codex 使用 `danger-full-access` 以避开测试主机的 bwrap 限制，仅请求执行 `pwd`；
不改变日常配置，结束后校验配置/认证/缓存哈希并清理临时凭据。
具体已验证范围见 [验证记录](docs/verification.md)。

`smoke_remote_compaction.py` 默认使用本机模拟上游及临时 Codex 配置，验证自动压缩、连续手动压缩、
重启恢复和切回官方路径，不发起真实推理；需要已安装 Codex、已有登录和模型目录缓存。
`--live` 会调用真实模型，验证手动压缩及摘要事实保留；只使用独立合成会话，不执行原有任务。

## 致谢

参考了 [codex-chatgpt-web](https://github.com/miuuyy/codex-chatgpt-web) 的动态目录模板、
官方请求转发与传输兼容处理。引用版本及上游 MIT 许可见 [第三方声明](THIRD_PARTY_NOTICES.md)。
