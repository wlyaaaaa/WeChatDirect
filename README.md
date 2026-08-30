# WeChatDirect

WeChatDirect 是一个 Windows-only 的本地工具：从当前电脑上已登录的 WeChat 本地数据库和缓存中，读取一段有界的聊天或朋友圈上下文，并按需生成本机导出或保全文件。

源数据库和缓存始终只读。工具只会写入用户明确指定的本地导出、状态和保全目录，不会改写 WeChat 数据，也不会自动登录、联网补历史、打开远端主页、点赞、评论或启动后台同步。

本项目与 Tencent 或 WeChat 没有关联。只应读取本人设备上、本人有权访问的数据；不要用它绕过账号、设备或他人的访问边界。

## 先了解边界

- 主 CLI 只支持 Windows，要求 Python 3.14 或更高版本。
- 账号按配置槽位严格隔离。每次读取都绑定一个账号的本地身份；身份不匹配时直接失败，不会把另一个账号的数据当作答案。
- 聊天和朋友圈范围以当前设备可见的本地数据为准，不等于远端全历史。不可解析的正文、不可打开的媒体、缺少索引或源库在读取期间发生变化时，会作为明确缺口返回。
- 朋友圈读取的是当前本机缓存；缓存中没有目标时，工具会报告未命中，并要求在同一账号中由用户先完成必要的本机操作后再重试。
- 图片、文件、视频、表情和语音只有在消息关系与本地字节都能精确绑定时才会打开或复制；不会根据文件名、群名或上下文猜内容。通话状态不是可供转写的语音文件。

## 安装

在 PowerShell 中使用主 CLI 的 Python 3.14 环境：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
```

开发依赖（包含未固定版本的 `ruff`）可安装为：

```powershell
python -m pip install -e ".[dev]"
```

安装会带上主 CLI 需要的 `cryptography`，以及 Windows Python 读取 `Asia/Shanghai` 时区所需的 `tzdata`。安装完成后可使用 console script，或直接运行脚本：

```powershell
wechat-direct --help
py -3.14 wechat_cli.py --help
```

语音解码是独立的 Python 路径。默认调用 `py -3.11`，并需要在该环境安装 `pilk`：

```powershell
py -3.11 -m pip install pilk
```

如需指定其他 Python 3.11 解释器，可设置 `WECHAT_DIRECT_VOICE_PYTHON` 为该解释器的完整路径。主 CLI 仍使用 Python 3.14 或更高版本；只有 `media-open --voice-wav` 和保全中的语音派生 WAV 会使用语音解释器。

## 配置与导出位置

配置文件的优先级从高到低为：

1. 命令行显式 `--config`。
2. 环境变量 `WECHAT_DIRECT_CONFIG`。
3. CLI 脚本同目录、被 Git 忽略的 `.wechatdirect.local.json` 中的 `config`。
4. `%LOCALAPPDATA%\WeChatDirect\accounts.json`。

导出根的优先级从高到低为：命令的显式 `--output`（该命令支持时） > `WECHAT_DIRECT_EXPORT_ROOT` > `.wechatdirect.local.json` 中的 `export_root` > `%LOCALAPPDATA%\WeChatDirect\exports`。

`.wechatdirect.local.json` 只允许两个键：`config` 和 `export_root`。可复制 `local-settings.example.json` 后填入本机值；该文件只应留在本机，不要提交或分享。

`accounts.example.json` 只展示结构和占位符。真实配置需要为两个隔离槽位分别填写：

- `config_path`：加密账号配置载体的位置；
- `local_state_path`：该账号的本地状态位置；
- `expected_source_identity_sha256`：来源身份承诺；
- `expected_moments_author_sha256`：朋友圈作者身份承诺。

示例中的路径和 `sha256:<...>` 都不是可用值，必须替换为自己的实际值。配置不保存微信密钥明文；仍应使用仅当前 Windows 用户可读的 ACL，并把本地配置、数据库路径、身份承诺、消息和媒体视为敏感资料。不要把真实配置、导出目录或终端回执发布到公开仓库。

## 命令

下面的 `primary`、`secondary` 是示例配置中的隔离槽位名，不代表真实账号；`<contact>`、`<group>`、`<locator>` 和 `<output>` 都必须替换为当前结果中的值。除特别说明外，输出是 stdout 上的一份 JSON 回执或结果。

### `context`：读取一段聊天上下文

```powershell
wechat-direct context --account primary --contact "<contact-or-group>" --lookback-days 7
wechat-direct context --account auto --contact "<contact-or-group>" --contains "<keyword>"
```

这是有界读取，不会先做全账号同步。`auto` 在多个账号都匹配或无法唯一定位时会停止并返回候选，不会猜测。结果包含消息、发送者方向、可影响含义的媒体关系和缺口。

### `sync-contact`：一个对象的首次导出与增量重放

```powershell
wechat-direct sync-contact --account primary --contact "<contact-or-group>"
wechat-direct sync-contact --account primary --contact "<contact-or-group>" --full-reconcile
```

第一次为点名对象建立本机可见档案；重复同一命令时使用来源指纹、游标和有界重叠窗口合并变化。需要重新核对全部本地历史时，才显式使用 `--full-reconcile`。这不是全账号同步，也没有常驻任务。

### `moments`：读取当前本机朋友圈缓存

```powershell
wechat-direct moments --account primary --self --lookback-days 30 --limit 20
wechat-direct moments --account primary --contact "<contact>" --lookback-days 30 --limit 20
```

必须显式指定账号，可读取该账号自己的缓存或按一个精确联系人筛选。它不会访问远端主页；当前缓存没有目标时会返回可行动的缺口。

### `sync-moments`：导出当前缓存快照

```powershell
wechat-direct sync-moments --account primary --self
wechat-direct sync-moments --account primary --contact "<contact>"
```

该命令保存并重放当前设备可见的朋友圈缓存快照，不承诺远端全历史。账号、对象和输出范围始终保持隔离。

### `media-open`：打开一个精确媒体定位

定位值必须来自同一账号、同一消息结果中的 `locator`；不会扫描目录或猜测文件：

```powershell
wechat-direct media-open --account primary --locator "<locator>" --output "<output-file>"
wechat-direct media-open --account primary --locator "<voice-locator>" --output "<output-wav>" --voice-wav
```

目标输出必须不存在。普通调用保留原始字节；`--voice-wav` 只接受精确绑定的 Tencent/WeChat SILK 语音，并使用上文的 Python 3.11 + `pilk` 路径。stdout 回执会给出输出文件的字节数和 SHA-256。

### `preserve`：保全一个明确的聊天窗口

```powershell
wechat-direct preserve --account primary --contact "<contact-or-group>" --lookback-days 1 --output "<preserve-directory>"
```

该命令只复制用户点名窗口内能精确打开的媒体，并生成自包含保全目录。原始语音保留 SILK；可用时同时生成 WAV。不能打开或解码的项目会留在缺口中，不会被静默丢弃或伪造。

### `doctor` 与 `verify-export`

这两个命令用于安装后的环境/配置检查和导出完整性检查。典型调用为：

```powershell
wechat-direct doctor --config "<path-to-accounts.json>"
wechat-direct verify-export --output "<export-directory>"
```

需要更多选项时先运行 `wechat-direct doctor --help` 或 `wechat-direct verify-export --help`。`doctor` 应用于确认 Windows、Python、配置入口和所需本地依赖；`verify-export` 只检查 `sync-contact` 或 `sync-moments` 产生的 v1 导出，不接受 `preserve` 保全目录。它们都只报告检查结果，不代替用户决定账号、对象或公开分享范围。

## 输出文件

`sync-contact` 的默认联系人导出目录包含：

- `ai-context.md`：面向 AI 的近期小上下文；
- `context.md`：完整的人类可读档案，查早期内容时只读取命中附近；
- `messages.jsonl`：完整结构记录，供精确检索、去重和增量合并；
- `manifest.json`：范围、完整性和文件哈希；
- `state.json`：增量游标与来源状态；
- `last-run.json`：最近一次运行的回执、缺口和耗时；
- `media/`：当前能精确绑定并打开的媒体。

`sync-moments` 使用相同的 `ai-context.md`、`context.md`、`manifest.json`、`state.json` 和 `last-run.json`，并以 `moments.jsonl` 保存朋友圈结构记录。

`preserve` 目录包含 `messages.json`、`manifest.json` 和按消息关系组织的 `media/`；语音 WAV 是从同一 SILK 字节派生的文件。`media-open` 只创建用户指定的单个输出文件，并在 stdout 返回来源与输出哈希。

## 许可证

本项目使用 MIT License，见 [LICENSE](LICENSE)。
