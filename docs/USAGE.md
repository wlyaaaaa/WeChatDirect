# WeChatDirect

WeChatDirect 主要供 AI 调用，是一个 Windows-only 的本地工具：从当前电脑上已登录的 WeChat 本地数据库和缓存中，读取一段有界的聊天或朋友圈上下文，并按需生成本机导出或保全文件。命令以结构化 JSON 表达消息、读取范围、缺口和下一步，便于 AI 判断本次结果能支持什么结论。

它面向其他项目交付同一份有序消息和真实可读的媒体：保留发送者、引用关系，以及每次图片、表情包和其他附件出现的位置。媒体含义由消费项目和 AI 在实际读取后理解。HTML 只是可选查看方式，不是 AI 的输入要求或运行依赖。

源数据库和缓存始终只读。普通 `context` 只查本机；显式 `export-context` 或 `media-open` 可以按选定消息自带的原生微信 CDN 地址物化表情，并严格核对原生 MD5、声明大小和图片解码结果。`--local-only` 可禁止这一步。读取入口不会改写 WeChat 数据、自动登录、打开远端主页、点赞、评论或启动后台同步，也不会搜索或拼造媒体地址、补抓全账号历史。可选 `helper` 命令通过独立的原生服务执行显式发送或管理动作，见下文。

处理大群中的本人参与时，`context --self-only`在原生发送者归属层筛选，再解码本人消息；归属未确定的消息仍返回，不能当作没有本人发言。分页、固定时间窗口与引用目标保持，`selectionScope`明确其只覆盖本人及待定发送者。需要对方前后回应时，用普通`context --around <时间>`补读；该筛选不声称已读全群或全部必要语境，也不检测已消失历史或所有旧消息修订。

本项目与 Tencent 或 WeChat 没有关联。只应读取本人设备上、本人有权访问的数据；不要用它绕过账号、设备或他人的访问边界。

## 先了解边界

- 主 CLI 只支持 Windows，要求 Python 3.14 或更高版本。
- 账号按配置槽位严格隔离。每次读取都绑定一个账号的本地身份；身份不匹配时直接失败，不会把另一个账号的数据当作答案。
- 聊天和朋友圈范围以当前设备可见的本地数据为准，不等于远端全历史。不可解析的正文、不可打开的媒体、缺少索引或源库在读取期间发生变化时，会作为明确缺口返回。
- 朋友圈读取的是当前本机缓存；缓存中没有目标时，工具会报告未命中，并要求在同一账号中由用户先完成必要的本机操作后再重试。
- 图片通过消息原生 MD5 与 `hardlink.db` 精确定位，使用现有保护配置中的媒体密钥解开 V1/V2 DAT；可读原件优先，缩略图会明确标记。表情保留原 PNG/GIF 等格式及每次出现的位置；本机封装无法读取时，显式物化可尝试同条消息已记录的原生 CDN，原生 MD5/大小不符就保留缺口。
- `VoiceInfo` 语音仍按同消息精确取出，按需派生 WAV；通话状态不是可转写的语音文件。视频、文件仅在消息 MD5 能通过本机原生索引定位到实际文件时交付；本机缺失、封装不可解或来源不一致时不猜内容。
- `WXGF` 图像封装先用进程内的 PyAV（依赖 `av`）解码：静态图不启动外部程序，比启动 `ffmpeg`/`ffprobe` 快得多（本机两者各 227 MB，每启动一次 2.5–4 秒）；动图仍用 PATH 中的 `ffmpeg` 转 GIF，PyAV 缺失或解码失败时退回 `ffprobe` + `ffmpeg`。仅在分区和帧数可证明时转成 PNG 或保留动作的 GIF。复杂多分区/透明度关系未能证明时明确保留缺口，不把第一帧冒充完整动图。

## 安装

在 PowerShell 中使用主 CLI 的 Python 3.14 环境：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -c constraints-verified.txt -e .
```

可编辑安装让 `wechat-direct` 直接使用仓库源码，修复源码后无需重装；本地设置也从源码目录发现，无需在 `site-packages` 维护链接。移动项目或换机后重建虚拟环境，再执行上述安装命令。

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

`.wechatdirect.local.json` 允许 `config`、`export_root`，以及可选的 `aliases`（称呼表路径）、`plaintextCacheRoot`（见下文明文快照缓存）和 `helper_config`（原生 helper 本机配置）五个路径键。可复制 `local-settings.example.json` 后填入本机值；该文件只应留在本机，不要提交或分享。

`accounts.example.json` 只展示结构和占位符。真实配置至少填写一个槽位；键名只允许 `primary`、`secondary`，两者均可配置。`auto` 和 `changes --account both` 只检查已配置槽位，显式点名缺失槽位返回 `wechat_account_not_configured`，不会转到另一账号。每个已配置槽位须填写：

- `config_path`：加密账号配置载体的位置；
- `local_state_path`：该账号的本地状态位置；
- `expected_source_identity_sha256`：来源身份承诺；
- `expected_moments_author_sha256`：朋友圈作者身份承诺。

示例中的路径和 `sha256:<...>` 都不是可用值，必须替换为自己的实际值。配置不保存微信密钥明文；仍应使用仅当前 Windows 用户可读的 ACL，并把本地配置、数据库路径、身份承诺、消息和媒体视为敏感资料。不要把真实配置、导出目录或终端回执发布到公开仓库。

`config_path` 指向已有的本地来源配置 JSON，而不是 `accounts.json`。它至少含 `dbPath`（账号目录或其父目录）、`decryptKey`（64 位十六进制数据库主密钥）和 `myWxid`（原生账号 ID）；可选的 `wxidConfigs.<myWxid>.imageAesKey/imageXorKey` 用于图片。上述字符串可为明文或 `safe:` 加 base64 封装；封装内容为 `v10`、12 字节 nonce 和 AES-GCM 密文。`local_state_path` 指向同一来源配置使用的 Chromium 风格 `Local State` JSON，其 `os_crypt.encrypted_key` 是 base64 编码、以 `DPAPI` 开头的受 Windows 当前用户保护的密钥。两份载体必须属于同一账号与 Windows 用户。WeChatDirect 读取已有载体，不生成数据库密钥或这些载体；项目没有核实可公开指定的生成程序及版本，因此不要用昵称、其他账号文件或任意导出工具的相似字段替代。

已有这两份载体时，可在原本有权读取它们的 Windows 用户下计算配置承诺；命令只输出两个 SHA-256 值，不输出原生 ID 或密钥：

```powershell
wechat-direct identity-commitments --config-path "<source-config.json>" --local-state-path "<Local State>"
```

把输出的 `expected_source_identity_sha256` 和 `expected_moments_author_sha256` 分别填入该槽位。前者是 UTF-8 编码 `myWxid` 的 SHA-256；后者先严格去掉原生 ID 末尾的 `_` 加四位十六进制存储后缀（若存在），再取 UTF-8 SHA-256。运行时仍会独立校验来源身份，不会因能计算承诺就自动信任或改写配置。

图片解码按当前来源身份从同一配置载体的 `wxidConfigs` 读取 `imageAesKey`、`imageXorKey`，缺少账号专属项时才使用身份一致的当前顶层项；沿用原有 DPAPI/safe 解封流程，仅在进程内使用。不会从昵称或配置目录名猜账号，不要求重新粘贴已保存的密钥，也不把密钥写入阅读包、终端或 Git。媒体字段缺失不影响普通文字读取。

### 明文快照缓存

读取加密的本地数据库时，解密后的明文库会长期留在本机缓存中；之后只重新解密主库变化的页，并重新叠加当前 WAL，不再每次整库解密。源文件完好时，结果与逐次整库解密逐字节一致；与旧做法的差别见 [恢复与结果边界](RECOVERY.md)。

- 位置优先级：环境变量 `WECHAT_DIRECT_CACHE_ROOT` > `.wechatdirect.local.json` 中的 `plaintextCacheRoot` > `%TEMP%\WeChatDirect\plain-cache`。其下按账号身份承诺和数据库相对路径分条目存放。
- 关闭：设置环境变量 `WECHAT_DIRECT_CACHE=0`，即回到每次在任务临时目录整库解密的旧做法。
- 删除：没有读取在进行时，可随时整个删除缓存目录，下次读取自动重建（首次重建耗时与旧做法相当）。缓存不归 `temp-status` 管理，`--clean` 不会碰它。
- 同一数据库的缓存正被另一个读取进程使用时，本次读取对所有被占用的库合计最多等待约 2 秒，仍拿不到就对这些库退回旧做法，结果不变。
- 缓存中的明文不另加密、遮盖或定期清理，按与导出文件同等的敏感本机资料对待，不要提交或分享。

## 命令

下面的 `primary`、`secondary` 是示例配置中的隔离槽位名，不代表真实账号；`<contact>`、`<group>`、`<locator>` 和 `<output>` 都必须替换为当前结果中的值。除 `--help` 的帮助文本外，正常完成和可由 Python 捕获的命令/参数失败会在 stdout 输出一份 JSON 回执或结果；进程被强制终止时不能保证回执。

### `context`：读取一段聊天上下文

```powershell
wechat-direct context --account primary --contact "<contact-or-group>" --lookback-days 7
wechat-direct context --account auto --contact "<contact-or-group>" --contains "<keyword>"
```

这是有界读取，不会先做全账号同步。`auto` 合并已配置账号的匹配，无法唯一定位时会停止并返回候选；称呼表有下节所述的默认私聊规则。结果包含消息、发送者方向、可影响含义的媒体关系和缺口。

`messages[]` 每条消息的 `time` 是固定 `+08:00` 的 ISO 北京时间（缺少原生时间时为 `null`），原始 Unix 秒 `createTime` 保留；`time` 仅供展示，不参与消息或清单哈希，也不写入 `sync-contact` 存档。

已知要找群时加 `--chat-type group`，只找私聊则加 `--chat-type private`（包含文件传输助手，不含公众号）。`context`、`export-context` 和 `preserve` 先查称呼表，再依次尝试完整名称或原生 ID、忽略空白和标点符号后的相等名称、不少于两字的部分名称、群成员称呼；每一层会合并所选账号的命中，有有效命中便停止。`sync-contact` 也接受称呼表和 `--chat-type`，之后只用完整名称或原生 ID、宽松相等名称两层，已有档案仍核对原绑定。

非精确选中的 `contact.resolution` 会说明 `matchKind` 和原查询，按成员选群时还有 `matchedMembers`。重名会返回 `status=needs_choice` 和按最近时间排序的 `candidates`，最多列 20 个，更多时看 `candidateTotal`；群成员候选的 `memberCount` 是该群当前可见成员数。没找到时，`suggestions` 最多提供 5 个相似名称供重新选择，`similarity` 越接近 1 越相似，工具不会自动替你选。

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

### 称呼表

微信备注、昵称或群名里找不到本人常用的称呼时，AI 先跟本人确认对应的私聊或群，再用 `alias add` 记下来；以后直接用这个称呼。登记时的 `--contact` 可以填完整名称或原生 ID，仍有歧义时会返回 `needs_choice`，找不到时返回 `contact_not_found`，由 AI 用确认后的原生 ID 重试。登记目标时不会用旧称呼去解析新称呼。

```powershell
wechat-direct alias add --name "<称呼>" --contact "<private-native-id>" --account primary --chat-type private
wechat-direct alias add --name "<称呼>" --contact "<group-native-id>" --account primary --chat-type group
wechat-direct context --contact "<称呼>"
wechat-direct context --contact "<称呼>" --chat-type group
```

同一个称呼可以对应一个人的私聊和多个群。没加 `--chat-type` 时，若有效命中中恰好有一个私聊，就默认选私聊；要找群时加 `--chat-type group`，多个群仍会返回候选。可用 `--account auto|primary|secondary` 限定账号，`auto` 只查已配置账号；称呼比较会忽略空白和标点符号。称呼表是匹配的第一层，优先于微信里的同名标签；`context`、`export-context`、`preserve`、`sync-contact` 都支持，朋友圈作者也可用该账号登记的私聊称呼。

称呼表保存在 `.wechatdirect.local.json` 的可选 `aliases` 路径指定的 UTF-8 JSON 文件中；没有这个键时，使用当前生效的 `accounts.json` 所在目录下的 `aliases.json`。表内的称呼、账号和原生 ID 都是私人资料，文件只留在本机，不要提交到公开仓库。重复登记同一宽松称呼、账号和原生 ID 会更新显示名与登记时间，不会新增重复条目。

`alias list` 会为每条登记附上当前显示名 `current` 和是否失效的 `stale`。已登记的原生 ID 不在该账号当前会话列表里时，该条称呼失效，不会被选中；解析结果的 `staleAliases` 会列出称呼、账号和登记时显示名，并继续尝试后面的匹配层。失效登记仍保留，确认不用后可以删除：

```powershell
wechat-direct alias list --name "<称呼>" --account auto
wechat-direct alias remove --name "<称呼>" --account primary --native-id "<native-id>"
```

`list` 可省略 `--name` 以列出所选账号的全部登记；`remove` 必须给 `--name`，省略 `--native-id` 会删除该称呼在所选账号中的全部登记，一条都没匹配到会明确报错。这几个命令只读联系人和会话库，不读取消息分库。文件损坏时会报错并保留原文件。

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

第一次只在全新空目录中为点名对象建立本机可见档案。已有匹配 `manifest.json` 和 `state.json` 的完成态档案时，重复同一命令才会使用来源指纹、游标和有界重叠窗口增量刷新；需要重新核对全部本地历史时，可再次显式使用 `--full-reconcile`。来源未变时仍核验已有档案、媒体和并发变化，但跳过整树复制、暂存副本验证和目录改名；有变化时先在同级事务目录完成构建和校验，再发布。正常失败保留原档案；硬中断使用下文 `recover-export` 检查和恢复。它不是逐消息断点续跑，也不是全账号同步或常驻任务。

未指定 `--output` 时，默认目录为 `exports/contacts/<解析后的账号>/contact-<原生 ID 的 SHA-256 前 16 位>`，同一个联系人换微信名称或登记称呼仍用同一目录。默认目录不存在时，先查该账号目录及旧版 `auto` 目录下完成态档案的 `manifest.json`，沿用清单账号相同且 `contact.nativeId` 相同的目录；多个匹配时选 `messageCount` 最大的，并在结果中返回 `existingArchiveReused: true`；没有匹配才新建。显式 `--output` 的行为不变，沿用档案仍执行原有账号、身份绑定和完整性校验。

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

目标输出必须不存在，包括发布前被另一进程创建的情况。图片会交付标准可读格式，表情保留静态或动画；语音普通调用保留 SILK，`--voice-wav` 使用上文的 Python 3.11 + `pilk` 派生 WAV。stdout 回执给出实际类型、质量、来源、字节数和 SHA-256。

图片和表情的文件后缀跟实际内容走：`--output emo` 或 `--output emo.jpg` 而内容是 GIF 时，实际写成 `emo.gif`，以回执的 `output` 为准。回执的 `view.path` 是该打开的那一个文件，不用自己抽帧、改后缀：

- 静态 PNG/JPEG/WebP：就是原文件。单帧 GIF 或每帧都一样的“动图”：同目录的 `<名字>.still.png`，`animated=false`。
- 真动图：同目录的 `<名字>.frames.png`，一张拼图，不同的帧按时间排好并标出帧号和秒数，透明背景铺白。`distinctFrames` 是不同的帧数，`framesShown` 是拼进去的帧数（最多 25，首尾都在），`complete=false` 表示有帧没拼进去。
- `view=null` 和 `viewError` 表示没做出来，原文件仍已交付，按原文件处理。

准确优先、速度其次：图片/表情必须实际打开让当前 AI 看见内容，文件存在或解码成功不代表已经看过含义；动图看 `view.path` 的拼图，`complete=false` 且动作/文字快变或结尾反转处看不清时，再按需从保留动画的原文件补密，不能用首帧代表完整动作，未覆盖的片段如实说明。语音先用 `media-open --voice-wav` 取得 WAV，再按 `chinese-asr` 当前云优先配置取得真实转写，复用同段音频已有有效结果省时，转写疑点仍保留不确定，不能把提示音或静音当成语音转写成功。

helper 新发的语音也可按同一消息精确读回。优先匹配原生服务器 ID；语音记录尚无服务器 ID 时，只补查当前账号中同聊天、本地消息 ID 与时间的唯一记录，不改源库。仍须实际读取媒体验收，发送回执的 `wechatAccepted` 本身不代表已读回。

本机加密月缓存中的新表情可由 `media-open`、导出或保全直接物化，保留动画并核对同消息的 MD5 与长度。这条补充读取路线需要已安装 helper，且所选账号当前登录；它只读校验实际微信核心与前后账号身份，缓存密钥仅留内存，不注入 DLL、不启动原生服务或会话。普通 `context` 仅报告可物化，不读取微信进程；条件不满足就保留真实缺口，原有标准缓存读取和原生 CDN 规则不变。

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

### `helper`：原生按次动作与任务内会话

这是可选接入层，固定适配 Prometheus-first/helper 提交 `0a0cf6e09650fd2838af2042c4d7472c071977ee` 的 `NativeTextService`。上游须单独放置并保留其 `LICENSE`、`THIRD_PARTY_NOTICES.md` 与 MinHook 许可；上游限免费个人非商业使用，并非本项目的 MIT 许可。不要运行上游 `main.py`、`web.py` 或 `PluginManager`：其默认插件包含收到撤回后补发等写动作。这里直接使用原生服务，不启动机器人业务插件、LLM、定时任务或网页端口。

主 CLI 的 64 位 Python 3.14 环境可选安装 `.[helper]`（增加 `pefile`）。helper 缺失不影响现有读取和 `doctor`。本机另建被忽略的 `helper-config.json`，只含两个绝对路径：

```json
{
  "runtime_path": "<absolute-path-to-pinned-helper-runtime>",
  "state_root": "<absolute-path-to-local-helper-media-state>"
}
```

配置优先级为显式 `--helper-config` > `WECHAT_DIRECT_HELPER_CONFIG` > `.wechatdirect.local.json` 的 `helper_config`。安装时在资料存储实际可用期间创建配置的 `state_root`；运行时不存在就明确失败，不从失效或未挂载的逻辑根递归建目录。其下按 primary/secondary 分开保存接收媒体及上游发送快照；使用时解析目录联接到真实绝对路径，配置仍保留稳定逻辑路径，子目录由上游在实际写入时创建。不扫描表情资料库或保存全账号聊天。状态命令只检查依赖与文件，不导入原生模块、不读微信账号、不注入 DLL，也不证明当前微信核心版本或功能已实测：

```powershell
wechat-direct helper status --helper-config "<helper-config.json>"
wechat-direct helper capabilities --helper-config "<helper-config.json>"
```

动作必须明确 `--account primary|secondary`，不支持 `auto`；继承原有 `--config` 配置解析。先核来源身份，再将原生登录 ID 的 SHA-256 与该槽位 `expected_moments_author_sha256` 核对；不会与含存储后缀的来源承诺混比。每次动作前再次读原生登录身份，退出或切号立即阻止后续动作。

```powershell
wechat-direct helper send-text --account secondary --target filehelper --text "明确要求发送的文字"
wechat-direct helper send-image --account secondary --target filehelper --path "<absolute-image-path>"
wechat-direct helper send-file --account secondary --target filehelper --path "<absolute-file-path>"
wechat-direct helper send-emoticon --account secondary --target filehelper --path "<absolute-gif-path>"
wechat-direct helper send-voice --account secondary --target filehelper --path "<absolute-silk-path>"
```

发送目标用显式原生 `--target`，或 `--contact` 精确名称/已登记称呼；后者复用原有解析，但拒绝宽松/部分匹配，不猜接收者。查群可加 `--chat-type group`。文本支持 `--text-file` UTF-8 文件，原生单条上限为 1023 UTF-8 字节，中文通常每字 3 字节；空文本、NUL 或超限返回结构化错误，不截断、不自动分段重发。AI 需要分段时自行按字节边界组织，并逐条解释结果。

发给真人的文字用 `send-paced`，像打字很快的人那样一条一条发：

```powershell
wechat-direct helper send-paced --account secondary --contact "<称呼>" --since "<读到的最后一条的时间>" --bubble "第一条" --bubble "第二条"
```

不加 `--send` 只打印计划，不连原生服务（约 3 秒）：`now` 是现在几点，`lastFromOther`、`lastFromSelf` 是双方最后一条和过了多少分钟，`plan` 是每条的思考和打字秒数，`firstBubbleAt`、`lastBubbleAt` 是预计发出时间。第一条的思考、打字、等待秒数都是0，通过发送前检查后立即发；之后每条按默认每分钟100字打字，加0.5–2秒思考，微信自带表情（如 `[旺柴]`）每个算1.5秒，每条的打字时间再随机乘0.7–1.3，字数相同也不等时，可用 `--cpm`、`--think` 调整（`--cpm` 是平均速度）。加 `--not-before <时间>` 则到点立即开始首条发送前检查，不再人为等待；本机读取和原生调用仍有实际耗时，要等较久时放后台跑。

确认后加 `--send`。整组共用一次原生连接，发送前和每条之间都读本机记录：`--since` 之后对方有新消息返回 `chat_moved` 和没发的 `unsent`；整组发送前和发送期间本人自己发了新消息同样算，第一条之后按时间顺序用本组已发出的气泡逐条抵消同内容的本人消息，每条气泡只抵消一次，多出的本人消息仍停发，即使内容与后面尚未发送的气泡相同。读不到记录返回 `check_failed`，不盲发。回执不明返回 `send_uncertain`，`uncertain` 是那一条，不自动重发。发完读回，结果在 `readback`、`readbackComplete`，发送期间对方的新消息在 `newAfterSend`。`filehelper` 不做新消息检查，可用来试样子。

对方常秒回时加 `--wait-after`（与 `--send` 一起用，放后台跑）：发完不断开，同一条连接接着等回复，省去一次重连，发送和等待之间没有空档。结果多一个 `wait`，内容与 `wait-new` 的输出相同，但 `new` 不含本组刚发的气泡，`wait.nextSince` 已排在这些气泡之后；可用 `--settle-seconds`、`--max-minutes`、`--include-self`、`--stop-file`，含义同 `wait-new`。`--stop-file` 已存在时返回 `stop_file_exists`，什么都不发；发送途中或 `--not-before` 等待时出现则不再发后面的气泡，返回 `stopped` 和 `unsent`（不加 `--wait-after` 也一样）。没发完（`chat_moved`、`send_uncertain` 等）不进入等待。

**等待挂着也能直接发。** 一个微信进程同时只能有一条原生连接（上游的进程级占用锁 `Local\WxBotBridge.<pid>`；第二条连接若不被拒绝，会把正在用的桥接当遗留物卸载），所以发送和等待不能真的同时连着。发送命令会自己处理：它占住本账号的“发送轮次”，正在等待的 `wait-new` 或 `--wait-after` 在1秒左右看到后断开，等发送结束再重连接着等，同一进程不退出、不叫醒 AI，回报里 `steppedAsideForSend` 记让路次数。使用方不用建停止文件、等退出、再重挂。发送方中途崩溃时系统自动收回轮次，等待不会卡住。让路期间靠发前和每条之间的本机记录检查发现新消息（`chat_moved`），等待重连后也会读记录，3秒后补读一次，所以她这段时间发的不漏；本组刚发的气泡是本人消息，不会叫醒，等她回复时它们和回复一起出现在 `new` 里，`role=self`。实测主号文件传输助手：等待挂着时第一条约 7.3 秒发出，没挂时约 5.5 秒。

加 `--wait-after` 时不用先判断有没有等待：这个聊天已有等待在挂，发完 `wait.status=existing_waiter` 并立即退出，由原来的等待接着等，`wait.nextSince` 已排在本组气泡之后；没有就自己接着等。等的是同号另一个聊天时，发完本命令接着占连接，那个等待重连不上，60 秒后报 `connection_lost` 交回 AI。

60 秒内连不上（另一组发送还没结束、`helper session` 占着连接，或等待是这版之前启动的、不会让路）返回 `status=bridge_busy`，什么都没发，全部气泡在 `unsent`。`helper send-text` 等一次性动作同样会请等待让路，连不上时返回 `helper_bridge_busy`。常驻的 `helper session` 不会让路。

账号已退出登录时不发送（微信升级后读不出账号也会这样报，那时重新登录解决不了），返回 `login_required` 和 `requiredAction=owner_relogin`，`sent` 是已发的，`unsent` 是还没发的；发送途中退出的那条若已调用原生接口，仍按 `send_uncertain` 处理，不自动重发。

若已登记称呼失效，`--contact` 不会沿同名联系人继续发送，而在启动原生服务前返回 `helper_stale_alias_requires_resolution`；应先核对并显式指定 `--target`，或修复称呼后再执行。正常精确名称、仍有效的称呼及显式 `--target` 保持可用，不自动修改或删除称呼。

图片与动态 GIF 表情分别对应 `send-image` 和 `send-emoticon`。文件、语音沿用上游 `prepare_file`/`prepare_voice` 的上下文快照和清理，不直接绕过媒体生命周期。语音支持 Tencent SILK；单声道 16-bit PCM WAV 要求上游可选编码器 `pysilk`（发行包 `silk-python`），缺少时明确报错，可先用已有的 Python 3.11/pilk 路线转为 SILK。现有阅读端语音解码配置不会自动变成 helper 的 WAV 编码器。

其他已接入动作是 `send-at-text --target <群ID> --text <文字> --member <成员ID>`（可重复 member，1–8 人）、`group-members --target <群ID>`、`invite-group-members --target <群ID> --member <成员ID>` 和 `create-group --member <成员ID>`（至少两人）。群成员仅是已加载缓存快照，缺失返回 `members=null`，不冒充空群；群艾特、邀请、建群只完成接口接线，未做真实群操作验收。

需在同一 AI 任务连续收发、获取实时申请再显式同意时，使用按需 stdio（标准输入输出）会话，输入输出均为 UTF-8 JSON Lines（每行一个 JSON）：

```powershell
wechat-direct helper session --account secondary --event-target filehelper
```

```json
{"action":"send-text","target":"filehelper","text":"明确要求发送的文字"}
{"action":"receive","seconds":2,"limit":20}
{"action":"friend-requests"}
{"action":"accept-friend","request_id":"fr_<本会话实际返回的32位十六进制ID>"}
{"action":"close"}
```

每条请求对应一条回执；最后一条示例申请 ID 是占位符，不能直接使用。会话只接受固定动作及原生 ID，不接受任意上游方法名或昵称。来自显式 `--event-target`（默认 filehelper）的事件在当前会话内至多缓冲 200 条，保留最新事件，溢出的最旧事件计入 `dropped`；只留内存，退出即清空。`receive` 按上限 1–100 条取走积累事件，可有界等待 0–60 秒接收新事件，后续调用不会重复交付已取走的事件；`bufferedRemaining` 给出剩余条数，发生溢出返回 partial。原生 Hook 本身仍观察当前账号的全局消息和好友申请，媒体接收白名单只约束允许目标，不能声称已在原生层缩成文件助手专用 Hook；适配层不转交范围外消息、不持久化全账号消息。

媒体回调异步完成，后发 GIF 可能早于先发图片就绪；同一 `native_message_id` 从 pending 变为 ready/failed 是同一消息的状态更新，不是重复消息。用该 ID 对接同账号、同会话的 reader 记录，`sequence` 只辅助当前连接排序，不能把事件抵达顺序当作原生聊天顺序。`.13` 桥接仅带语音元数据而没有音频时，`receive` 可沿现有 reader 按所选账号、会话和服务器消息 ID 精确补取，核对发送者、收发方向与类型后保存到接收账号的媒体目录，成功返回 ready 及 `voice_source=local_database`，源内容缺失仍返回 failed。这只补全已观察到的实时事件，不补接收前历史、不常驻，也不自动重发。

实时上下文须显式开启，普通 session 的行为保持原样：

```powershell
wechat-direct helper session --account secondary --event-target filehelper --realtime --asr-root "<ChineseASR绝对根目录>"
```

启动先准备本地识别并返回就绪回执，再监听消息；读取消息时只使用已就绪模型，不触发冷加载。通过原生消息 ID 读取同一账号、同一对象的精确上下文：

```json
{"action":"read-context","target":"filehelper","native_message_id":"<本会话观察到的消息ID>"}
{"action":"context-results","request_id":"<read-context返回的request_id>"}
```

`read-context` 返回精确消息、引用、原媒体路径、缺口与首个完整的语音转写候选。同一消息已有微信原生转写缓存时直接读取；没有缓存时，已就绪本地模型与云端双路并行，保留来源，后到候选用同一 `request_id` 调 `context-results` 读取，不重新发送云请求；云请求有界完成。

`receive.events[].received_at` 是本机收到该次回调的整数秒 Unix 时间戳，上游未提供发送时间；精确消息的原生发送时间见 `read-context.messages[].createTime`（同为秒级时间戳），不能用收到时刻代替。

`observedAt` 从原生事件首次观测起计时，`elapsed`、`onTime`、`late` 如实记录；15 秒完整上下文仍须真实链路验收，接口接线与模拟测试不代表已达标。图片和动图的 `materialComplete=true`、`ready_for_vision` 只表示材料齐备可供阅读；`visualRead=false` 时不得声明 `complete` 或 `onTime=true`，AI 必须实际打开内容看过，再判断图意与完整上下文是否按时获得。

只处理显式目标，不做全账号同步或自动回复，不开服务端口；`close` 或 stdin EOF 清理本任务启动的识别及辅助进程。

好友申请仅来自当前活连接，不补历史、不自动同意。申请票据只留上游内存，不写 JSON 回执、日志或普通配置；用公开 `request_id` 在同一会话显式 `accept-friend`。当前目录已标记 accepted 的申请再次提交时返回 `outcome=previously_accepted`、`nativeInvoked=false`；`wechatAccepted=null` 表示本次没有新的原生完成回执，之前通过的证据来自当前活连接目录，会话仍可继续。close、EOF 或连接结束会清空申请窗口；退出后不能靠旧 ID 恢复票据。按次 CLI 的 `friend-requests`/`accept-friend` 在读取配置或加载原生模块前直接返回 `helper_live_session_required`；应通过 session 内的同名 JSON 动作完成，不能跨进程复用申请 ID。

普通动作执行后在 finally 中 stop；会话 close/EOF 也清理并卸载桥接，EOF 额外输出最终 close 清理回执。会话启动、输入循环或卸载失败时进程退出码非零；可恢复的单条请求失败仍以逐条 JSON 回执为准，不累计为整个连接失败，正常 EOF/close 且卸载成功可退出 0。没有全局 daemon（常驻服务）、HTTP 端口、命令队列或任务结束后保留的服务。当前 `.13` 的主动加好友、完整联系人扫描及 helper 朋友圈读/发/互动明确返回不可用；原有数据库 `moments`/`sync-moments` 保持可用。

回执中 `invocationCompleted` 表示适配调用是否返回，`wechatAccepted` 只在上游原生完成回执明确成功时为 true，`actualReadback=not_checked` 表示还没有从微信数据库或客户端验收实际结果。超时、异常或回执丢失返回 `status=unknown`、`retryable=false`，会话阻止继续发送，应先精确读回目标再决定后续；不自动重试。`cleanup` 单独报告卸载结果，发送已受理但清理失败为 partial，不能当作全成功。失败时上游可能留下其 `.runtime/native_requests` 报告或桥接，应按上游清理流程核实，不能强杀后直接重发。

## 后台等新消息

AI 需要等指定聊天来消息后再开始处理时，把下面的命令交给宿主的后台执行功能；宿主在命令结束后通知原对话。命令只等待和报告，不思考、不发送，也不自动重挂：

```powershell
wechat-direct wait-new --account secondary --target "<原生ID>" --since "2026-01-01T10:00:00+08:00" --stop-file "<绝对路径>/wait.stop"
```

`--account` 必须明确选 primary 或 secondary；`--target` 和 `--contact` 二选一，称呼只允许精确联系人或已登记别名，可加 `--chat-type group|private`。`--config` 指定账号配置。`--since` 是上次读到的最后一条消息时间，不带时区按北京时间解释。默认 `--settle-seconds 15`：收到对方消息后等连续15秒没有更新再返回，从首条触发起最多等 `max(60, settle_seconds)` 秒。等静默期间本人自己回了，这一轮不叫醒，继续等对方下一条。本人正在自己聊时用 `--settle-seconds 120`，对方说完两分钟本人没接才叫醒。`--max-minutes` 默认1440，到点结束一次，由人决定是否再挂。`--include-self` 也允许本人事件触发，仅用于从手机发到文件传输助手验收。

命令使用一个经过登录身份核对的原生连接，接收范围仅为指定原生ID，不加载语音识别。连接后先读一次本机记录补齐 `--since` 到连上之间的消息，3秒后再补读一次，防止连上前到达、当时还没写进记录的消息漏掉；之后只接收实时事件，到点准备叫醒前再读一次，记录最后一条是本人发的就不叫醒（私聊里本人的消息没有实时事件）；退出先关闭连接，再读一次记录取得发送时间和全部新增消息（`connection_lost`、`login_required` 也读，读失败记在 `finalReadError`，`stopped` 不读）。没有定时读记录或断线降级。补读失败不中断等待，记在 `recheckError`。`--since` 按秒比较，与它同一秒的新消息不算新；发完马上等回复请用 `helper send-paced --wait-after`。

账号退出登录时返回 `status=login_required`、`requiredAction=owner_relogin`，并带回本机记录里已到的消息，这时只有本人能重新登录。调用方通知本人后，加 `--wait-login` 重挂：账号未登录时只读核对登录状态（每5秒一次，不向微信加载任何东西），本人登录后立刻返回 `login_restored` 和期间的新消息，调用方按 `nextSince` 正常重挂并接着处理没发的内容；账号本来就在线则与普通等待相同。到 `--max-minutes` 仍未登录再返回一次 `login_required`，收到 `--stop-file` 返回 `stopped`。

标准输出是一份JSON：`status` 为 `new_messages`、`timed_out`、`stopped`、`login_restored` 时退出码0（正常结束，具体看 `status`），`locked`、`read_failed`、`connection_lost`、`login_required` 时退出码2。`now`、`since`、`nextSince` 和每条 `new[].time` 都是北京时间；`waitedSeconds` 为本次耗时；`new` 含期间双方全部消息的 `role`、`type`、`content`、`minutesAgo`；带媒体的消息另有 `media[]`（`kind`、`locator` 等），可直接交给 `media-open`，不用再跑 `context` 查编号；语音已有微信自己的转文字时 `content` 是该文字，`contentSource=wechat_native_cache`。没有新消息时 `nextSince` 等于原时间。`pushAvailable`、`connectedSeconds` 说明连接情况；有事件时附首事件收到时间及与本机记录发送时间的秒差，尚无法匹配时差为null。`cleanup.dllUnloaded` 单独说明原生桥接是否卸载成功。图片和语音仍需原有媒体命令阅读，不把文字投影当完整媒体语境。

可选本机设置 `personal_data_access` 必须是检查模块的绝对路径：`wait-new` 在命令开始、关闭实时连接后最终读记录之前各查一次 `check_access("factor")`，接收事件期间不反复检查；`helper send-paced` 只在命令开始检查一次。只有字典中的 `status=pass` 放行；文件缺失、检查异常、锁定均返回 `locked` 和 `required_action`。最终读取前锁定时不输出消息正文，`new` 为空、`nextSince` 保持原 `since`。不配置时不检查，便于其他使用者使用。本机真实路径不写入仓库。

后台安全停止：在另一个命令中创建 `--stop-file` 指定的文件，等待命令将在下一次接收检查时返回 `stopped` 并关闭连接；该文件由调用方维护，复用前先清理。前台可按Ctrl-C。正常结束、到点结束和Ctrl-C都会调用关闭。上游有独立隐藏清理进程，在持有者被单独强杀后尝试卸载，下一次启动也会清理遗留桥接；清理可能失败，不能保证任意强杀后绝无残留，避免强杀整个进程树而同时杀掉清理进程。

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

## 许可证

本项目使用 MIT License，见 [LICENSE](../LICENSE)。

归档事务、定点修复与诊断细节见 [恢复与结果边界](RECOVERY.md)。
