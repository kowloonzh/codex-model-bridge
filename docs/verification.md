# 验证记录

## 0.5.0 第三方远程压缩 v2（2026-09-21）

- 61 项本地测试通过，覆盖单个 compaction 输出、真实用量保留、工具禁用、连续压缩、
  两种后端重放、重启恢复、未知/损坏检查点拒绝、失败/取消、SSE/JSON 响应及编码边界。
- `scripts/smoke_remote_compaction.py` 使用真实 Codex 0.155.1 客户端和模拟上游，
  验证自动压缩、连续两次手动压缩、客户端重启恢复、切回官方路径；无真实推理调用。
- `scripts/smoke_remote_compaction.py --live --model deepseek/deepseek-flash` 验证真实 DeepSeek
  连续两次手动压缩、客户端重启恢复及切回 gpt-6-astra；模型自己生成的随机标记只存在于
  历史助手回复，最新持久化检查点和后续回复均保留该标记。
- 原配置及认证文件哈希保持不变。测试均使用临时会话，不执行用户原有任务。
- 安装包与测试源码逐文件核对一致，systemd 服务和 CLI 已更新到 0.5.0。
- 部署后对用户指定会话做只读快照回放：229 个历史项，64 对工具调用/结果，选择其当前
  deepseek/deepseek-v4-pro 模型，以 low 推理进行摘要验证。压缩 HTTP 200，返回一个合法
  compaction 项，摘要 3,362 字符，耗时 87.3 秒；再次用该检查点请求上下文可读性确认通过。
  原始 rollout 哈希未变化，没有执行原任务或替原客户端写入压缩状态。
- `cmb1:` 是有界的版本化明文编码，不是加密；检查点不依赖服务内存，可跨重启恢复。
  旧版 bridge 不支持此格式。摘要不是无损归档，压缩的质量与耗时仍取决于上游模型。
- 本次适配 v2 `compaction_trigger`；旧 v1 `/responses/compact` 沿用既有转发路径。

## 0.4.0 跨 provider 压缩历史交接（2026-09-20）

- 50 项本地测试通过。新增覆盖官方检查点导出、当前消息不发送给导出请求、认证隔离、
  同检查点并发合并、账户/凭据缓存隔离、失败重试、未完成流拒绝、明文摘要及本地标记，
  并确认官方请求仍原样携带检查点。
- 官方真实 SSE 的 `response.completed` 不重复已经流式发送的 output，导出器同时收集
  `response.output_text.delta` 和 `response.output_item.done`，并只在确认 completed 后使用正文。
- 使用用户指定的分叉会话所继承的真实官方检查点验证：来源模型为 gpt-5.6-sol，检查点
  长度 7,436 字节。测试不恢复或执行原任务，仅要求目标模型返回固定确认标记。
- 修复前会因 encrypted compaction 返回 409；修复后该检查点请求返回 HTTP 200，
  qax/deepseek-v4-flash 返回准确的验证标记。生成可读摘要 4,732 字符，整个请求耗时 75.7 秒。
- 0.4.0 安装代码与测试源码逐文件核对一致，systemd 服务和 CLI 已切换到该版本。
  部署后使用同一个真实检查点再次验证，HTTP 200、确认标记匹配，耗时 72.1 秒，服务缓存已填充。
- 原始会话记录未改写，用户检查点、摘要内容和真实认证信息未加入仓库。
- 摘要为官方模型生成的交接文本，不是逐字无损解密。导出消耗官方额度；内存缓存重启即失效。
  未验证其他账号/后端的密文能够转换；失败时不继续发送缺失历史的请求。

下文为旧版本历史验证记录，涉及加密压缩一律返回 409 的限制已由 0.4.0 更新。

## 0.3.0 自动 provider 发现（2026-09-20）

- 42 项本地测试通过：自动发现、无 profile 启动、认证命令、元数据生成、失败保留、
  成功空列表清理、并发刷新锁、凭据轮换、移除 provider、变更地址缓存失效，及既有回归。
- 实际刷新 5 个 provider 全部成功：qax 9、claude_proxy 9、deepseek 2、codegen 6、codegen_yun 6。
  共 32 个第三方模型，其中 30 个采用兼容模板，2 个复用已有匹配模型目录。
- Codex 成功解析 7 个官方模型 + 32 个第三方模型，合计 39 个模型。
- 两条完整真实 smoke 均通过：`--model claude_proxy/deepseek-v4-flash`（已有元数据），
  `--model qax/deepseek-v4-flash`（兼容模板）。均验证 weekly 窗口、可见列表、官方回复、
  同一 thread 切入第三方执行真实 pwd，再切回官方并保留测试词。
- 测试前后原配置、认证及生成缓存哈希一致。
- 已构建并安装 0.3.0，核对安装代码与测试代码一致；systemd 改为读取生成缓存，旧路由文件
  和旧服务/运行环境保留备份。新增用户 PATH 中的 `codex-model-bridge` 入口。
- 部署后独立验证服务健康、39 项模型目录、可见模型列表、weekly 额度窗口，全部通过。
- 模型发现不等于推理能力验收：其余 30 个第三方条目没有逐一真实推理与工具验证。
  兼容模板的上下文默认预算和上游默认推理策略不代表准确获知了所有模型能力。
- 没有验证任意后端加密压缩历史互通，仍按既有边界返回 409。

## 0.2.0 多 provider / 多模型改造（2026-09-20）

- 本地测试：29 项通过，包含原有 19 项回归。
- 新增覆盖：无 profile 时从全局 provider + 路由文件启动；同一 provider 多模型；
  两个 provider 使用不同认证与地址；别名改写上游模型 ID；Responses 与 compact 路由；
  缺失元数据/凭据、重复别名、未知字段及官方 slug 冲突拒绝。
- 真实命令：`PYTHONPATH=. python scripts/smoke_live.py --test-alias`。
  临时服务发布 7 个官方模型、原 DeepSeek 名称和一个临时别名，共 9 个模型。
- Codex 目录解析、可见 model/list、weekly 额度窗口均通过。
- 同一 thread 官方 → DeepSeek 临时别名 → 官方通过，真实 `pwd` 工具调用成功，
  前文测试词保留；证明别名已被映射为上游实际模型名称。
- 原全局配置、认证文件、bridge 路由配置哈希未变化。
- 已部署到 systemd 用户服务 0.2.0，启动参数改为 `--config`，不再使用 `--profile qax`。
  已核对安装包源码与测试源码一致；旧运行环境与 service 文件备份保留。
- 部署后健康检查、8 项模型目录、可见模型列表、weekly 额度窗口再次通过；临时测试别名未持久化。
- 多个不同真实 provider 未全部调用；两个 provider 的认证隔离通过本地 HTTP 集成测试。
  不意味着任意 Responses 兼容模型的工具协议都已验证。

以下为首版详细验证与延续的边界。

日期：2026-09-20。客户端：Codex CLI 0.155.1。
运行环境：Python 3.14.3、aiohttp 3.13.5、zstandard 0.25.0。

## 本地测试

命令：`.venv/bin/python -m unittest discover -s tests -v`

19 项测试通过，覆盖：

- profile、provider、模型目录读取和凭据 repr 隐藏。
- 动态目录随上游更新，追加模型，移除旧缓存条件，推导客户端版本。
- 官方认证、请求体与额度头透传，DeepSeek 的认证隔离。
- zstd 解码、请求校验、错误状态传播、实时 SSE 转发。
- SSE 完成后的异常断连容错，未完成的流不会被伪造为完成。
- 远程压缩端点按模型分流，加密压缩历史明确拒绝跨后端转换。
- 切回官方时清理不兼容推理字段及 item ID，保留原有官方加密推理。

## 真实验证

命令：`PYTHONPATH=. .venv/bin/python scripts/smoke_live.py`

使用临时 loopback 端口及临时 CODEX_HOME，复制现有登录以进行测试；未修改日常配置。

- Codex 成功解析合并目录：7 个官方模型，加 `deepseek-v4-flash`。
- `model/list`（不包含隐藏模型）确认 DeepSeek 出现在客户端可选列表中。
- `account/rateLimits/read` 成功返回官方账户 weekly 窗口，长度为 10080 分钟。
- `gpt-6-astra` 成功回复指定测试词。
- 同一 thread 切到 DeepSeek，真实执行 `pwd` 且 exit code 为 0，仍记得测试词。
- 同一 thread 切回 `gpt-6-astra`，成功回复先前的测试词。
- 原有 `config.toml`、`qax.config.toml`、`auth.json` 哈希保持不变。

可编辑安装、`codex-model-bridge --check` 及 wheel 构建均成功。

测试中发现并修复了 zstd 请求解析失败，以及切回官方时 reasoning.content 不兼容。
本机 `bwrap` 无法创建 loopback，故真实工具测试使用临时配置下的 danger-full-access。
该测试只请求执行 `pwd`；这不改变用户日常沙箱配置。

## 证据边界

- weekly 已验证其官方账户接口；没有以截图方式验证 TUI 状态栏渲染。
- 同一 thread 切换使用 `codex exec resume -m`；未自动操作 TUI 的 `/model` 菜单。
- 尚未真实验证长会话自动压缩、图片、搜索、MCP、子代理及所有官方模型组合。
- 加密压缩检查点不能跨后端解密，返回 409，要求先生成可读摘要并开启新会话。
- Python 3.11–3.13 的兼容性未在本机执行；当前实测为 Python 3.14.3。
- 没有安装常驻服务，也没有修改用户默认 `openai_base_url`。
