# WeChatDirect

WeChatDirect 主要供 AI 调用，是一个 Windows-only 的本地工具：从当前电脑上已登录的 WeChat 本地数据库和缓存中，读取一段有界的聊天或朋友圈上下文，并按需生成本机导出或保全文件。命令以结构化 JSON 表达消息、读取范围、缺口和下一步，便于 AI 判断本次结果能支持什么结论。

它面向其他项目交付同一份有序消息和真实可读的媒体：保留发送者、引用关系，以及每次图片、表情包和其他附件出现的位置。媒体含义由消费项目和 AI 在实际读取后理解。HTML 只是可选查看方式，不是 AI 的输入要求或运行依赖。

源数据库和缓存始终只读。普通 `context` 只查本机；显式 `export-context` 或 `media-open` 可以按选定消息自带的原生微信 CDN 地址物化表情，并严格核对原生 MD5、声明大小和图片解码结果。`--local-only` 可禁止这一步。工具不会改写 WeChat 数据、自动登录、打开远端主页、点赞、评论或启动后台同步，也不会搜索或拼造媒体地址、补抓全账号历史。

本项目与 Tencent 或 WeChat 没有关联。只应读取本人设备上、本人有权访问的数据；不要用它绕过账号、设备或他人的访问边界。

## 先了解边界

- 主 CLI 只支持 Windows，要求 Python 3.14 或更高版本。
- 账号按配置槽位严格隔离。每次读取都绑定一个账号的本地身份；身份不匹配时直接失败，不会把另一个账号的数据当作答案。
- 聊天和朋友圈范围以当前设备可见的本地数据为准，不等于远端全历史。不可解析的正文、不可打开的媒体、缺少索引或源库在读取期间发生变化时，会作为明确缺口返回。
- 朋友圈读取的是当前本机缓存；缓存中没有目标时，工具会报告未命中，并要求在同一账号中由用户先完成必要的本机操作后再重试。
- 图片通过消息原生 MD5 与 `hardlink.db` 精确定位，使用现有保护配置中的媒体密钥解开 V1/V2 DAT；可读原件优先，缩略图会明确标记。表情保留原 PNG/GIF 等格式及每次出现的位置；本机封装无法读取时，显式物化可尝试同条消息已记录的原生 CDN，原生 MD5/大小不符就保留缺口。
- `VoiceInfo` 语音仍按同消息精确取出，按需派生 WAV；通话状态不是可转写的语音文件。视频、文件仅在消息 MD5 能通过本机原生索引定位到实际文件时交付；本机缺失、封装不可解或来源不一致时不猜内容。
- `WXGF` 图像封装需要本机 PATH 中可用的 `ffmpeg` 与 `ffprobe`；仅在分区和帧数可证明时转成 PNG 或保留动作的 GIF。复杂多分区/透明度关系未能证明时明确保留缺口，不把第一帧冒充完整动图。

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

安装会带上主 CLI 需要的 `cryptography`、验证图片和帧信息的 `Pillow`，以及 Windows Python 读取 `Asia/Shanghai` 时区所需的 `tzdata`。安装完成后可使用 console script，或直接运行脚本：

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

图片解码按当前来源身份从同一配置载体的 `wxidConfigs` 读取 `imageAesKey`、`imageXorKey`，缺少账号专属项时才使用身份一致的当前顶层项；沿用原有 DPAPI/safe 解封流程，仅在进程内使用。不会从昵称或配置目录名猜账号，不要求重新粘贴已保存的密钥，也不把密钥写入阅读包、终端或 Git。媒体字段缺失不影响普通文字读取。

## 命令

下面的 `primary`、`secondary` 是示例配置中的隔离槽位名，不代表真实账号；`<contact>`、`<group>`、`<locator>` 和 `<output>` 都必须替换为当前结果中的值。除特别说明外，输出是 stdout 上的一份 JSON 回执或结果。

### `context`：读取一段聊天上下文

```powershell
wechat-direct context --account primary --contact "<contact-or-group>" --lookback-days 7
wechat-direct context --account auto --contact "<contact-or-group>" --contains "<keyword>"
```

这是有界读取，不会先做全账号同步。`auto` 在多个账号都匹配或无法唯一定位时会停止并返回候选，不会猜测。结果包含消息、发送者方向、可影响含义的媒体关系和缺口。

AI 读取结果时还应检查这些字段：

- `coverage.hasMore` 和 `continuation`：有下一批可读内容时，把返回的 `account`、`contact` 和 `cursor` 用于下一次 `context` 调用。游标固定账号身份、联系人、时间窗和搜索条件；不要自行构造或修改游标。扫描上限和返回上限可以调整。
- `search.status`：关键词只匹配本次解码得到的消息文字。`not_found_in_page` 表示这一页未命中，通常同时返回 `status=partial` 和续查信息；只有读尽当前查询范围才能报告 `not_found_in_requested_window`。续查末页的 `not_found_in_remaining_window` 只描述剩余范围。`indeterminate_content_gaps` 表示存在无法解析的正文，不能据此断言从未说过。
- `coverage.returnedAllScanned`：本次展示的上下文是否包含所有扫描消息。关键词命中会返回其附近的小窗口，不能把该窗口当作完整历史；`continuation.purpose` 区分继续找匹配上下文和普通翻页。
- `coverage.snapshotScope`：每次调用读取独立的本地快照。续查固定查询时间上界，不承诺不同调用之间本地历史永远不变。

```powershell
wechat-direct context --account primary --contact "<contact-or-group>" --cursor "<continuation.cursor>"
wechat-direct context --account primary --contact "<contact-or-group>" --around "2026-08-01T12:00:00+08:00"
```

`--around` 在允许的时间窗中优先读取离目标时间最近的消息，再按时间顺序返回。未指定起止时间时，窗口默认围绕目标时间前后各 `--lookback-days` 天，截止时间不晚于本次读取时间。显式时间窗不包含目标时间时返回参数错误。`--since`、`--until` 和 `--around` 接受 ISO 日期/时间；未写时区时按 `Asia/Shanghai` 解释。

### `export-context`：给 AI 和其他项目的阅读包

```powershell
wechat-direct export-context --account primary --contact "<contact-or-group>" --lookback-days 7 --output "<new-directory>"
wechat-direct export-context --account primary --contact "<contact-or-group>" --cursor "<continuation.cursor>" --output "<next-page-directory>"
```

该命令与 `context` 使用相同的范围、锚点和分页参数，默认生成：

- `conversation.json`：完整的本次消息页、发送者、引用、媒体清单、读取缺口和后续游标。消息数组是阅读顺序的依据。
- `media/`：本次能够精确打开的媒体文件。媒体项中的 `exportedPath` 相对于阅读包目录，`sha256` 和 `bytes` 描述实际导出的字节；相同文件可以复用，每次消息中的出现位置仍保留。`materializationSource` 区分 `local` 与 `remote`，`quality` 标记原件/缩略图，`mimeType`、尺寸和 `frameCount` 说明实际可读格式。阅读包不需要源数据库 locator、私有 URL 或解密参数。
- `ai-context.md`：按顺序阅读的入口摘要及媒体引用。摘要不是完整历史，完整本次结果在 `conversation.json`；图片和表情包需要实际交给视觉能力查看，不能由文件名或占位文本推断内容。

输出目录必须是新目录；已有目录和同名 `.incomplete` 均会保留并返回错误。部分消息或媒体不可读取时仍交付可用内容，并报告 `partial` 与具体缺口。`coverage.hasMore` 表示还可继续读取；后续页应使用其游标保持同一账号、聊天和时间窗。

需要人工查看时，可追加 `--html` 生成引用同一批媒体文件的 `conversation.html`。复制阅读包时应一起复制整个目录，以保留相对媒体路径。默认 AI 阅读流程不需要 HTML、浏览器或服务。

### `sync-contact`：一个对象的首次导出与完成态增量刷新

```powershell
wechat-direct sync-contact --account primary --contact "<contact-or-group>"
wechat-direct sync-contact --account primary --contact "<contact-or-group>" --full-reconcile
```

第一次只在全新空目录中为点名对象建立本机可见档案。已有匹配 `manifest.json` 和 `state.json` 的完成态档案时，重复同一命令才会使用来源指纹、游标和有界重叠窗口增量刷新；需要重新核对全部本地历史时，可再次显式使用 `--full-reconcile`。它不是首次运行硬崩溃后的完整断点续跑，也不是全账号同步或常驻任务。

### `moments`：读取当前本机朋友圈缓存

```powershell
wechat-direct moments --account primary --self --lookback-days 30 --limit 20
wechat-direct moments --account primary --contact "<contact>" --lookback-days 30 --limit 20
```

必须显式指定账号，可读取该账号自己的缓存或按一个精确联系人筛选。它不会访问远端主页；当前缓存没有目标时会返回可行动的缺口。

`targetCacheStatus=target_cached_outside_requested_window` 表示该人的内容已缓存，但本次日期范围内没有命中；`targetCachedWindow` 给出已缓存内容的时间范围。这种情况不会要求重新打开资料页。只有确认目标不在当前缓存中时，才返回 `target_not_in_current_local_cache` 及相应操作提示。

### `sync-moments`：导出当前缓存快照

```powershell
wechat-direct sync-moments --account primary --self
wechat-direct sync-moments --account primary --contact "<contact>"
```

该命令建立或刷新当前设备可见的朋友圈缓存快照，不承诺远端全历史。只有完成态快照才能安全重复刷新；账号、对象和输出范围始终保持隔离。

### `media-open`：打开一条精确绑定的媒体

定位值必须来自同一账号、同一消息结果中的 `locator`。图片、文件或视频仅找到原生路径时，返回 `openable=null`、`materializable=true`，实际打开成功后才标记可读；`requiresNetwork` 区分本地待读取与需要请求原生表情来源。不会根据模糊文件名扫描目录或猜图。示例：

```powershell
wechat-direct media-open --account primary --locator "<voice-locator>" --output "<output.silk>"
wechat-direct media-open --account primary --locator "<voice-locator>" --output "<output.wav>" --voice-wav
wechat-direct media-open --account primary --locator "<media-locator>" --output "<output-file>" --local-only
```

目标输出必须不存在。图片会交付标准可读格式，表情保留静态或动画；语音普通调用保留 SILK，`--voice-wav` 使用上文的 Python 3.11 + `pilk` 派生 WAV。stdout 回执给出实际类型、质量、来源、字节数和 SHA-256。

### `preserve`：保全一个明确的聊天窗口

```powershell
wechat-direct preserve --account primary --contact "<contact-or-group>" --lookback-days 1 --output "<preserve-directory>"
```

该命令生成一个自包含保全目录，复制点名窗口内当前本机能精确打开的媒体。原始语音保留 SILK，可用时派生 WAV；其他媒体保留可读格式及与消息的关系。保全不会自动请求远端表情；无法打开的内容仍保留明确缺口。

### `doctor` 与 `verify-export`

这两个命令用于安装后的环境/配置检查和导出完整性检查。典型调用为：

```powershell
wechat-direct doctor --config "<path-to-accounts.json>"
wechat-direct verify-export --output "<export-directory>"
```

需要更多选项时先运行 `wechat-direct doctor --help` 或 `wechat-direct verify-export --help`。`doctor` 应用于确认 Windows、Python、配置入口和所需本地依赖；`verify-export` 只读检查 `sync-contact` 或 `sync-moments` 产生的 v1 导出，不接受 `preserve` 保全目录，也不会重写、补齐或修复任何文件。它们都只报告检查结果，不代替用户决定账号、对象或公开分享范围。

## 故障与恢复边界

- 完成态档案可以再次运行同一命令做增量刷新，或显式使用 `--full-reconcile` 重核当前本机可见历史。`sync-contact` 在普通增量与 `noChange` 快速返回前都会重新核对 manifest 自身哈希、manifest/state 绑定，以及 `context.md`、`ai-context.md`、`messages.jsonl`、已声明导出媒体与派生 WAV 的哈希、大小或记录数；任一不一致都会精确失败并保留原文件，不会静默覆盖未知内容。
- 首次运行若在 `state.json` 提交前硬崩溃，目录会保留为没有 state 的半成品，并以 `sync_output_not_initialized` 精确失败。工具不会自动删除、覆盖或猜测接管这些未知内容；如需重做，应由用户保留或另行检查原目录后选择一个新的空输出目录。
- 遗留 `.sync.lock` 会以 `sync_already_running_or_stale_lock` 失败。工具无法仅凭锁文件证明原进程已结束，因此不会自动删除它。
- `verify-export` 是只读验证，不会修复归档；本项目也不提供 restore/import（恢复/导入）回微信的能力。
- 命令执行失败时，JSON 保留稳定的 `error`，并提供 `retryable` 与 `nextAction`。`retryable` 只说明原命令是否适合重试，不代表可以扩大账号、聊天或写入范围；已有 `.incomplete` 输出会保留，调用者可检查原文件或选择新的明确输出位置。

## 输出文件

`sync-contact` 的默认联系人导出目录包含：

- `ai-context.md`：面向 AI 的近期小上下文；
- `context.md`：完整的人类可读档案，查早期内容时只读取命中附近；
- `messages.jsonl`：完整结构记录，供精确检索、去重和增量合并；
- `manifest.json`：范围、完整性和文件哈希；
- `state.json`：增量游标与来源状态；
- `last-run.json`：最近一次运行的回执、缺口和耗时；
- `media/`：能精确绑定并在本机打开的图片、表情、语音、视频和文件，以及可用的派生 WAV。`sync-contact` 不会自动向远端补取整份历史的表情；后来可用的本地媒体可通过 `--full-reconcile` 重新核对。

`sync-moments` 使用相同的 `ai-context.md`、`context.md`、`manifest.json`、`state.json` 和 `last-run.json`，并以 `moments.jsonl` 保存朋友圈结构记录。

`preserve` 目录包含 `messages.json`、`manifest.json` 和按消息关系组织的 `media/`；语音 WAV 从同一 SILK 派生。`media-open` 只创建用户指定的单个媒体文件，并返回来源与输出哈希。

## 许可证

本项目使用 MIT License，见 [LICENSE](LICENSE)。
