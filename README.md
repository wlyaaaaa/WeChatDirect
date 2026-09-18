# WeChatDirect

WeChatDirect 主要供 AI 调用，是一个 Windows-only 的本地工具：从当前电脑上已登录的 WeChat 本地数据库和缓存中，读取一段有界的聊天或朋友圈上下文，并按需生成本机导出或保全文件。命令以结构化 JSON 表达消息、读取范围、缺口和下一步，便于 AI 判断本次结果能支持什么结论。

它面向其他项目交付同一份有序消息和真实可读的媒体：保留发送者、引用关系，以及每次图片、表情包和其他附件出现的位置。媒体含义由消费项目和 AI 在实际读取后理解。HTML 只是可选查看方式，不是 AI 的输入要求或运行依赖。

源数据库和缓存始终只读。普通 `context` 只查本机；显式 `export-context` 或 `media-open` 可以按选定消息自带的原生微信 CDN 地址物化表情，并严格核对原生 MD5、声明大小和图片解码结果。`--local-only` 可禁止这一步。工具不会改写 WeChat 数据、自动登录、打开远端主页、点赞、评论或启动后台同步，也不会搜索或拼造媒体地址、补抓全账号历史。

处理大群中的本人参与时，`context --self-only`在原生发送者归属层筛选，再解码本人消息；归属未确定的消息仍返回，不能当作没有本人发言。分页、固定时间窗口与引用目标保持，`selectionScope`明确其只覆盖本人及待定发送者。需要对方前后回应时，用普通`context --around <时间>`补读；该筛选不声称已读全群或全部必要语境，也不检测已消失历史或所有旧消息修订。

本项目与 Tencent 或 WeChat 没有关联。只应读取本人设备上、本人有权访问的数据；不要用它绕过账号、设备或他人的访问边界。

## 0.2.1：交付完整性回执

`recover-export` 执行 `complete` 或 `rollback` 后，目录存在性字段反映实际操作后的状态；`phase` 为 `completed` 或 `rolled_back`，`observedPhase` 保留操作前阶段。检查模式不修改归档。

`preserve` 和 `export-context` 的 `packageCreated=true` 只表示阅读包或保全包已经生成；`status=partial` 表示选中的消息、引用附件或派生 WAV 仍存在明确缺口，不等于命令执行失败。已生成的部分包仍返回退出码 0，并可用 `verify-export` 验证其现有内容和文件关系。`verify-export` 通过不证明源聊天完整，也不代表所有附件都在本机缓存中。

两种包的 `delivery` 统一报告消息数、引用消息数、媒体出现次数、实际可读与不可读媒体数、缺失 WAV 数、缺口类别及 `hasMore`。每一次引用附件出现都计入统计；SILK 成功但 WAV 失败时保留原件并明确报告。消费方不要仅凭退出码 0 或“包已生成”认定内容完整；`hasMore=true` 时仍须沿游标读取后续页。

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
python -m pip install -c constraints-verified.txt .
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

如需指定其他 Python 3.11 解释器，可设置 `WECHAT_DIRECT_VOICE_PYTHON` 为该解释器的完整路径。主 CLI 仍使用 Python 3.14 或更高版本；`media-open --voice-wav`、阅读包、联系人档案、定点媒体修复和保全中的语音派生 WAV 会使用语音解释器。

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

### `changes`：发现本机当前会话的增量候选

```powershell
wechat-direct changes --account both --since "2026-09-01T00:00:00+08:00"
wechat-direct changes --account primary --since "2026-09-01T00:00:00+08:00" --until "2026-09-08T12:00:00+08:00"
```

这个只读命令为后续阅读选择会话，不读取或返回消息正文，也不会复制数据库。`--account` 必须明确为 `primary`、`secondary` 或 `both`；使用 `both` 时，结果的 `accounts.primary` 与 `accounts.secondary` 完全分列，并各自带现有账号身份承诺。

调用开始时会固定并回显 `discovery.requestedWindow.untilS`。每个账号返回所有当前 `SessionTable` 中 `lastTimestamp >= since` 的候选，以及所有时间未知的会话；不会以条数截断。隐藏群同样保留。读取期间如果一个会话的当前最后时间已经晚于 `untilS`，它仍会以 `timestampState=observed_after_until` 返回，避免因为扫描后新消息而漏掉可能需要按原时间窗阅读的会话。消费方应继续用该固定 `untilS` 对选中的会话读取正文。

`complete=true` 只表示当前 `SessionTable` 的候选发现已完整返回，`completeScope` 会明确这一主语。它不代表所有历史变化都已检测：已从当前表消失的会话、旧消息后来补入或撤回、正文修改和标签历史都不能由这个命令证明；这些限制写在 `discovery.historicalChangeDetection`。会话项只包含账号、稳定 `nativeId`、当前标签、类型、最后时间、隐藏状态和可读的缺口字段。

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

第一次只在全新空目录中为点名对象建立本机可见档案。已有匹配 `manifest.json` 和 `state.json` 的完成态档案时，重复同一命令才会使用来源指纹、游标和有界重叠窗口增量刷新；需要重新核对全部本地历史时，可再次显式使用 `--full-reconcile`。新版本先在同级事务目录完成构建和校验，再发布。正常失败保留原档案；硬中断使用下文 `recover-export` 检查和恢复。它不是逐消息断点续跑，也不是全账号同步或常驻任务。

说话人编号只在消息所在分片的 `Name2Id` 中解释；本人原生用户名按该账号既有用户名承诺匹配，不从账号目录后缀、昵称或其他数据库推断。主副账号、私聊和群聊共用此规则，消息 `serverId` 与 `nativeId.value` 始终以字符串返回，避免长整数精度丢失。

旧版档案在下次运行同一对象的 `sync-contact` 时，先校验原档案，再自动重核该对象当前本机可见的全部历史，回执模式为 `sender_identity_reconcile`；完成后恢复普通增量。源中已不存在的旧消息保留正文和原归属证据，但现行说话人显示“身份未重核”，数量见 `senderIdentityUnrecheckedRetainedCount`。升级不会主动扫描或修改其他聊天档案。

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

需要更多选项时先运行 `wechat-direct doctor --help` 或 `wechat-direct verify-export --help`。`doctor` 应用于确认 Windows、Python、配置入口和所需本地依赖；`verify-export` 只读检查联系人档案、朋友圈快照、阅读包和保全包，验证文件哈希、大小、账号绑定及媒体关系。旧阅读包没有总清单时只报告其实际可验证范围，不把较弱的旧格式当成新格式完整验证。它不会重写、补齐或修复任何文件。它们都只报告检查结果，不代替用户决定账号、对象或公开分享范围。

## 故障与恢复边界

- 完成态档案可以再次运行同一命令做增量刷新，或显式使用 `--full-reconcile` 重核当前本机可见历史。`sync-contact` 在普通增量、完整重核与 `noChange` 返回前都会重新核对 manifest 自身哈希、manifest/state 绑定，以及 `context.md`、`ai-context.md`、`messages.jsonl`、已声明导出媒体与派生 WAV 的哈希、大小或记录数；任一不一致都会精确失败并保留原文件，不会静默覆盖未知内容。
- 新版本运行中断时，未发布构建保留在精确同级 `<output>.wechat-transaction` 目录。旧版本留下、没有事务元数据的半成品仍以 `sync_output_not_initialized` 失败；不能借新恢复命令自动接管未知旧内容。
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

## 0.2：可靠性、恢复与有界交付

源数据库保持只读。数据库快照与表结构探测共用同一个 WAL 校验器：核对格式、页大小、头部与帧累计校验，只接收连续有效的已提交前缀；明文 SQLite 兼容路径同样合并已提交 WAL。检查失败不会执行源库 checkpoint 或修写。

### 完整重核与定点媒体修复

`--full-reconcile` 会先验证旧档案，再重新生成当前源中存在的消息和媒体；不会用记录自带的旧哈希重新认证被修改的正文。损坏档案保持原样；确认需要重新导出时，使用明确的新空目录，不覆盖旧内容。源中已消失的旧消息仍按既有档案语义保留，不声称已重新证明来源。

只补齐一个已点名档案里的本地附件或 WAV 时使用：

```powershell
wechat-direct repair-media --account primary --contact "<contact-or-group>" --output "<archive-directory>"
wechat-direct repair-media --account primary --contact "<contact-or-group>" --output "<archive-directory>" --message-id "<native-id>" --kind voice
```

它不重扫聊天历史、不访问远端 CDN，不改变已读游标；可复用已验证的 SILK 派生 WAV。没有可用来源时保留缺口，错误不会伪装成已补齐。语音解码超时、解释器失效、非法 WAV 均转为可解释缺口；原 SILK 与其他消息继续保留。

### 按字节分页与长文本

`context --byte-limit 65536` 可把 stdout JSON 控制在 32–512 KiB 范围内。结果过大时先缩小消息页并保留续查游标；单条长文本使用 `segmentedFields` 明确指出预览长度、总长度、原文哈希和续读入口，不静默删字：

```powershell
wechat-direct message-part --account primary --contact "<same-contact>" --cursor "<segmentedFields.cursor>"
```

按 `offset`、`nextOffset` 接续，直到 `continuation=null`。分段按 Unicode 字符边界切分，整体 SHA-256 可核对；源内容变化时明确拒绝旧分段，不拼接两个版本。阅读包和保全文件保留完整选定消息，字节预算只控制终端传输。极端大元数据无法放入预算时，提示改用明确的新目录 `export-context`，不会把缺页说成全部读完。

### 归档事务与显式恢复

联系人同步、朋友圈快照和定点媒体修复均在独立同级事务目录构建完整新版本，通过离线验证后再发布，最后才输出成功回执。原档案在普通构建失败后保持可用。该做法需要容纳一个暂存副本的磁盘空间；没有常驻服务或新中央数据库。

发布使用两次目录改名，并非跨多个路径的全局原子操作。极端硬中断发生在两次改名之间时，原档案仍保存在该事务目录的 `previous` 中，可检查后恢复：

```powershell
wechat-direct recover-export --output "<exact-archive-directory>"
wechat-direct recover-export --output "<exact-archive-directory>" --action rollback
wechat-direct recover-export --output "<exact-archive-directory>" --action complete
```

默认只检查；`rollback` 撤销未发布的本次构建或恢复原版本，`complete` 只发布已经完成且重新验证通过的暂存版本，或清理已发布事务。正在运行的事务由操作系统锁保护；旧版未知 `.sync.lock`、不匹配目录、未知半成品不会自动删除。已发布新版本不能通过 rollback 偷偷删除，需先验证后用 complete 结束清理。恢复不导入微信、不重新读取账号内容。

### 分能力诊断与临时明文

```powershell
wechat-direct doctor --environment-only
wechat-direct temp-status --root "<task-temp-parent>"
wechat-direct temp-status --root "<task-temp-parent>" --session "<exact-session>" --clean
```

`--environment-only` 不打开本地设置或账号配置；分别报告文本、普通图片、WXGF、语音 WAV、离线验证，以及 ffmpeg/ffprobe 和执行身份。`sourceAccess=not_tested` 不等于真实账号读取通过。SYSTEM 与登录用户的 PATH/解释器配置必须分别判断；不通过更换账号绕过来源权限。

临时目录来自 `WECHAT_DIRECT_TEMP_ROOT`，否则使用调用方的 TEMP/TMP 设置。解密快照和音频中间文件只进入其下的 `wechat-direct-scratch`，带无正文的可重建标记与操作系统占用锁；正常退出删除。硬中断残留由精确 session 检查和显式清理，不能批量清理其他任务或未识别原件。它是临时明文生命周期管理，不承诺安全擦除或替代磁盘加密。

### 安装与验证

`constraints-verified.txt` 记录已验证的主要依赖组合，便于复现，不禁止未来兼容版本。 静态检查规则由 `pyproject.toml` 显式固定为项目既有的 E4/E7/E9/F 正确性检查，不依赖 Ruff 版本的默认规则集合；新工具默认风格要求不自动变成业务验收要求。Windows CI 同时验证该组合和当前依赖，使用合成消息、真实 SQLite WAL、人工加密页及故障注入，不携带真实账号、聊天、密钥或导出。Python 3.11 的语音解释器和 Python 3.14 主程序分别验收。主程序环境中的 `tools/smoke_runtime.py --voice-python <python311.exe> --require-voice --require-wxgf` 可用合成音频与图像验证真实解码链；安装验收应从源码目录之外调用，防止源码导入冒充安装成功。

## 许可证

本项目使用 MIT License，见 [LICENSE](LICENSE)。
