# 恢复与结果边界

## 可靠性、恢复与有界交付

`recover-export` 的检查模式只回报当前状态；执行 `complete` 或 `rollback` 后，`phase` 分别为 `completed` 或 `rolled_back`，`observedPhase` 保留操作前阶段，目录存在性字段反映操作后状态。

阅读包或保全包的 `packageCreated=true` 只表示已生成文件；`status=partial` 仍代表消息、引用附件或语音 WAV 有缺口，可能以退出码 0 交付。`delivery` 分列消息、引用、媒体出现次数、可读数、缺失 WAV、缺口类别与 `hasMore`；`verify-export` 通过只证明现有文件和关系可核对，不证明本机源历史完整。

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

联系人同步、朋友圈快照和定点媒体修复均在独立同级事务目录构建完整新版本，通过离线验证后再发布，最后才输出成功回执。原档案在普通构建失败后保持可用。该做法需要容纳一个暂存副本的磁盘空间；没有常驻服务或新中央数据库。事务期间用文件大小、修改时间和文件系统变更时间判断档案是否被并发改动，不再为此整树读取和哈希；复制、校验、改名或清理时档案文件被其他程序占用，统一返回可重试的 `archive_output_in_use`，关闭占用程序后对同一输出重试即可（若已发布、仅清理受阻，重试会提示先用 `recover-export` 完成清理）。

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

`constraints-verified.txt` 记录已验证的主要依赖组合，便于复现，不禁止未来兼容版本。静态检查规则由 `pyproject.toml` 显式固定为项目既有的 E4/E7/E9/F 正确性检查，不依赖 Ruff 版本的默认规则集合；新工具默认风格要求不自动变成业务验收要求。Windows CI 同时验证该组合和当前依赖，使用合成消息、完整加密 SQLite 数据库及加密 WAL、真实 SQLite WAL、人工加密页和故障注入，不携带真实账号、聊天、密钥或导出。每个测试模块先导入 `tests/isolation.py`，统一屏蔽本机 `.wechatdirect.local.json`、真实 `%LOCALAPPDATA%` 和全部 `WECHAT_DIRECT_*` 环境变量，测试结果不随开发机设置变化。CI 现在安装 ffmpeg，并同时要求合成语音与 WXGF 转码 smoke 通过。主程序环境中的 `tools/smoke_runtime.py --voice-python <python311.exe> --require-voice --require-wxgf` 可用合成音频与图像验证真实解码链；安装验收应从源码目录之外调用，防止源码导入冒充安装成功。
