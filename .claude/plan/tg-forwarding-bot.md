# 实施计划：TG 转发机器人 (v3)

## 任务类型
- [x] 后端 (-> Codex)
- [ ] 前端
- [ ] 全栈

---

## 技术方案

### 核心决策：Fork tgcf + 定制化

**为什么不是 Use as-is？** tgcf 缺少 Preview/Review 能力、自我转发（re-storage）功能、配置校验、per-route 规则粒度，且依赖老旧（Telethon 1.26.0）。

**为什么不是从零构建？** 重复造轮子，tgcf 已解决 auth/session/event 边界问题，fork 后可复用。

### 技术栈

| 组件 | 选型 | 理由 |
|------|------|------|
| TG 客户端 | Telethon (Codeberg 活跃分支) | Pyrogram 已归档，Telethon MTProto 覆盖最全 |
| 运行时 | Python 3.11+ / asyncio | tgcf 基础，生态成熟 |
| 配置 | YAML + Pydantic v2 校验 | YAML 生态通用 + Pydantic 类型安全 |
| 状态存储 | SQLite (轻量) | 消息去重、转发映射、检查点（几 MB 级别） |
| 媒体存储 | **Telegram 云端**（零本地存储） | copy 后消息自动存在 TG 云端，Bot 不保存任何媒体文件 |
| 媒体处理 | Pillow + ffmpeg (仅受保护内容提取时) | 水印去除、受保护媒体下载重传 |
| 部署 | Docker Compose on VPS | 一键部署，会话持久化 |
| 监控 | 结构化日志 + TG 审计频道 | 无额外基础设施依赖 |

### VPS 推荐配置

> **关键认知**：Bot 使用 `copyMessage()` 后消息自动存在 Telegram 云端，Bot 本身**不存储任何媒体文件**。本地仅需 SQLite（几 MB）+ Session 文件（几 KB）+ 日志。因此存储需求极低。

| 档位 | CPU | RAM | 存储 | 带宽 | 适用场景 | 月费参考 |
|------|-----|-----|------|------|----------|----------|
| **推荐基准** | **1 vCPU** | **2 GB** | **20 GB SSD** | **≥1 TB/月** | 5-20 源，纯 copy 模式（主要场景） | Hetzner CX22 ~$3.49 |
| 含受保护提取 | 2 vCPU | 4 GB | 40 GB SSD | ≥1 TB/月 | 需要频繁下载+重传受保护媒体 | Hetzner CX23 ~$4.99 |

**为什么能降配？**
- `copyMessage()` 在 Telegram 服务器端完成，Bot 零媒体 I/O
- 仅受保护内容提取时才需要下载→重传（临时缓存，处理完即删）
- Python + asyncio 单线程，CPU 和 RAM 开销极低
- SQLite 数据库 + 日志 < 100 MB

**云服务商推荐（2026年3月）**：

| 服务商 | 配置 | 月费 | 备注 |
|--------|------|------|------|
| **Hetzner CX22** | 2 vCPU / 4 GB / 40 GB | **~$3.49/月** | 性价比最高（4/1 起涨至 $4.99） |
| **Hetzner CX11** | 1 vCPU / 2 GB / 20 GB | **~$3.29/月** | 纯 copy 模式足够 |
| DigitalOcean | 1 vCPU / 2 GB / 50 GB | ~$12/月 | 稳定，文档好 |
| Vultr | 1 vCPU / 2 GB | ~$12/月 | 全球节点多 |

**推荐**：先用 **Hetzner CX11**（1C/2G/$3.29），不够再升 CX22。

### 架构数据流

```
=== 路径 A: 自动转发（监听源频道） ===
[Telegram Channel Updates]
  -> [Ingress Adapter: Userbot/Bot 模式切换]
  -> [Normalizer + Message UID 生成]
  -> [Dedupe Check (SQLite)]
  -> [Rule Engine (per source->target pair)]
      -> 关键词白名单/黑名单
      -> 发送者/媒体类型条件
  -> [Modifier Pipeline]
      -> 文本正则替换/格式化
      -> 水印/广告文本去除
  -> [Dispatch Scheduler (限流 + FloodWait 退避)]
  -> [Sender: copyMessage()]
      -> 成功 → [State Store + Audit Logger]
      -> CHAT_FORWARDS_RESTRICTED?
          -> 受保护提取插件已启用?
              -> 是 → [Protected Extractor: 读取+下载+重传]
              -> 否 → [Audit: 记录跳过原因]
  -> [State Store + Audit Logger]

=== 路径 B: 自我转发（用户手动触发，核心功能） ===
[用户手动转发消息到 Bot DM]
  -> [Bot 接收: 检测 forwarded message]
  -> [Album Batcher (grouped_id 聚合，~2s 窗口)]
  -> [可选: Modifier Pipeline (去水印/正则)]
  -> [Sender: copyMessage() / copyMessages()]
      -> 成功 → [回复用户 ✅ + Audit Logger]
      -> CHAT_FORWARDS_RESTRICTED?
          -> 受保护提取插件已启用?
              -> 是 → [Protected Extractor] → [回复用户 ✅ + Audit Logger]
              -> 否 → [回复用户 ❌ "内容受保护，无法保存"]
  -> [State Store + Audit Logger]
```

---

## 自我转发（Re-storage）技术说明

### 原理验证

| 问题 | 答案 |
|------|------|
| `copyMessage()` 创建的是独立消息吗？ | **是**。Bot API 明确说明 copy 发送的消息"without a link to the original"，文本和媒体都是独立副本 |
| 原频道被封/删除后消息还在吗？ | **是**。copyMessage 创建的消息完全独立，不受源频道任何影响 |
| 受保护内容（Restrict Saving Content）能 copy 吗？ | `copyMessage()` **不能**，返回 `CHAT_FORWARDS_RESTRICTED`。但 Userbot 模式可通过**读取+下载+重传**绕过（可选插件） |
| 相册/MediaGroup 怎么处理？ | 使用 `copyMessages()` 批量复制，按 `grouped_id` 聚合后 2 秒窗口批处理 |
| Sticker/语音/视频笔记等特殊类型？ | 大部分可 copy。Poll（投票）有限制，service message 和 invoice 不可 copy |

### 技术方案：Hybrid Copy-First + Protected Extractor

1. **默认引擎**: `copyMessage()` / `copyMessages()` — 最快、最省资源、零本地存储
2. **相册处理**: 按 `grouped_id` 聚合 ~2s 窗口，批量 copy
3. **受保护内容**: 可选插件 `protected_extractor`（默认关闭）
   - **关闭时**: fail-fast + 告知用户"内容受保护"
   - **开启时**: 自动 fallback → 读取文本/实体 → 下载媒体字节 → 重新上传发送
4. **源信息归属**: 默认去除「Forwarded from」标记。可选在消息末尾追加纯文本来源标注
5. **目标支持**: 私人频道（推荐）/ Saved Messages（需 userbot MTProto `inputPeerSelf`）
6. **防循环**: 如果转发目标也是被监听的源，需要 loop guard
7. **存储模型**: Telegram 云端存储所有消息，Bot 零媒体持久化

---

## 受保护内容提取插件（Protected Extractor）

### 技术原理
Telegram 的 `has_protected_content` 标志仅阻止 `forwardMessages` / `copyMessage` API 调用，但**不阻止已授权成员读取消息内容和下载媒体**。因此 Userbot 可以：
1. 通过 MTProto 正常读取消息文本、caption、formatting entities
2. 通过 `client.download_media()` 下载媒体字节
3. 通过 `client.send_message()` / `client.send_file()` 作为**全新消息**发送

### 参考实现
开源项目 `telemirror`（khoben/telemirror）已包含 `RestrictSavingContentBypassFilter`，验证了该方案可行性。

### 实现方案（伪代码）
```python
async def relay_message(msg, dst):
    try:
        return await copy_message(msg, dst)          # 快速路径
    except ChatForwardsRestrictedError:
        if not config.protected_extractor.enabled:
            raise                                     # 未启用，向上抛出
        return await clone_protected(msg, dst)        # fallback 路径

async def clone_protected(msg, dst):
    text = msg.message or ""
    entities = msg.entities or []

    if msg.grouped_id:
        # 相册：聚合同 grouped_id 消息，批量处理
        return await enqueue_album(msg, dst)

    if not msg.media:
        # 纯文本
        return await client.send_message(dst, text, formatting_entities=entities)

    # 有媒体：下载字节 → 重新上传
    blob = await client.download_media(msg, file=bytes)
    if blob is None:
        return await client.send_message(dst, text, formatting_entities=entities)

    return await client.send_file(
        dst, file=blob,
        caption=text, formatting_entities=entities,
        # 按媒体类型设置 attributes/mime/voice_note/video_note
    )
```

### 配置
```yaml
# 受保护内容提取插件（可选，默认关闭）
protected_extractor:
  enabled: false                # ⚠️ 默认关闭，需手动开启
  mode: userbot                 # 仅 userbot 模式支持（Bot Token 无法访问受保护频道）
  max_file_size_mb: 100         # 单文件最大下载大小限制
  temp_dir: /tmp/tg-extract     # 临时缓存目录（处理完自动清理）
  rate_limit: 5                 # 每分钟最大提取次数（降低封号风险）
```

### 风险声明
- 仅限**个人存档**用途，不用于分发或商业目的
- 存在账号临时限制/封禁风险（Telegram 可能检测异常行为）
- 低频使用 + 私人目标频道 → 风险极低
- 插件默认关闭，需用户明确开启并知晓风险

---

## 实施步骤

### Step 1: 项目初始化 & tgcf 评估
- **产物**: 项目骨架、依赖锁定、tgcf 源码分析报告
- **执行器**: Codex
- **文件**: `pyproject.toml`, `README.md`, `.gitignore`, `Dockerfile`, `docker-compose.yml`
- **约束**: Python 3.11+, 不引入不必要依赖
- **验收**:
  - [ ] 项目可 `pip install -e .` 本地安装
  - [ ] Docker 容器可启动
  - [ ] tgcf 核心模块分析完成（哪些复用、哪些重写）

### Step 2: 配置系统
- **产物**: YAML 配置模型 + Pydantic 校验 + 示例配置
- **执行器**: Codex
- **文件**: `config/schema.py`, `config/loader.py`, `config.example.yaml`
- **约束**: 支持 5-20 个 source-target pair，每对独立过滤规则；包含自我转发配置
- **验收**:
  - [ ] Pydantic 模型覆盖所有配置字段
  - [ ] 错误配置给出清晰错误信息
  - [ ] 示例配置可通过校验

**配置示例（预览）:**
```yaml
# 全局设置
sessions:
  userbot:
    api_id: ${TG_API_ID}
    api_hash: ${TG_API_HASH}
    phone: "+123456789"
  bot:
    token: ${BOT_TOKEN}

# ★ 自我转发（Re-storage）配置
self_forward:
  enabled: true
  target: -100666666666          # 保存目标：私人频道 ID
  # target: "saved"              # 或 "saved" 表示 Saved Messages（需 userbot）
  strip_attribution: true        # 去除转发来源标记
  append_source: false           # 可选：在消息末尾追加纯文本来源
  source_format: "\n\n📌 来源: {source_name}"  # 来源格式模板
  apply_modifications: false     # 是否对手动保存的消息也应用正则/过滤规则
  album_wait_seconds: 2          # 相册聚合等待窗口

# 可复用过滤模板
templates:
  clean_ads:
    replacements:
      - regex: "(?i)join\\s+us\\s+at.*"
        replace: ""
      - regex: "https?://t\\.me/\\S+"
        replace: "[链接已移除]"

# 自动转发任务
jobs:
  - name: "加密新闻"
    source: -100111111111
    target: -100222222222
    mode: userbot
    filters:
      whitelist: ["BTC", "ETH"]
      blacklist: ["scam", "airdrop"]
    use_template: clean_ads

  - name: "开发动态"
    source: -100333333333
    target: -100444444444
    mode: bot
    modifications:
      - regex: "v(\\d+)"
        replace: "Version $1"

# 运维
monitoring:
  audit_channel: -100555555555  # 审计日志频道
  admin_id: 123456789           # 管理员 ID（接收告警）
```

### Step 3: 核心转发引擎
- **产物**: 异步消息转发管道（ingress -> filter -> modify -> dispatch）
- **执行器**: Codex
- **文件**: `core/engine.py`, `core/ingress.py`, `core/dispatcher.py`, `core/dedup.py`
- **约束**:
  - asyncio 事件循环
  - 支持 Userbot + Bot 双模式
  - 消息去重 (src_chat, msg_id, dst_chat) 三元组
  - FloodWait 指数退避
  - loop guard（防止源→目标→源循环）
- **验收**:
  - [ ] 单源→单目标转发正常
  - [ ] 多源→多目标转发正常
  - [ ] FloodWait 重试机制生效
  - [ ] 断线重连自动恢复
  - [ ] loop guard 阻止循环转发

### Step 4: 自我转发引擎 + 受保护提取插件（核心功能）
- **产物**: 手动转发→独立保存 + 受保护内容 fallback 提取
- **执行器**: Codex
- **文件**: `features/self_forward.py`, `core/album_batcher.py`, `core/copier.py`, `plugins/protected_extractor.py`
- **约束**:
  - 使用 `copyMessage()` / `copyMessages()` 为主（Hybrid Copy-First）
  - 相册按 `grouped_id` 聚合，2 秒窗口批处理
  - 受保护内容：启用插件时 fallback 到 读取+下载+重传；未启用时 fail-fast
  - 受保护提取仅 Userbot 模式可用
  - 防循环检测
  - 临时下载文件处理完即删（零持久化）
- **验收**:
  - [ ] 用户转发文本消息 → Bot 独立 copy 到目标频道 ✓
  - [ ] 用户转发图片/视频 → Bot 独立 copy ✓
  - [ ] 用户转发相册（多张图） → Bot 批量 copy，保持顺序 ✓
  - [ ] 受保护频道（插件关闭） → Bot 回复"内容受保护" ✓
  - [ ] 受保护频道（插件开启） → Bot 提取并重传成功 ✓
  - [ ] Sticker/语音/视频笔记 → 正确 copy ✓
  - [ ] Bot 回复确认："✅ 已保存 N 条消息到 [目标频道]" ✓
  - [ ] 审计频道记录操作 ✓

**交互流程设计：**
```
用户操作:
1. 在任意频道/群组看到想保存的消息
2. 长按 → 转发 → 选择自己的 Bot
3. Bot 收到后自动处理

Bot 回复:
┌──────────────────────────────────────┐
│ ✅ 已保存 1 条消息                    │
│ 目标: 我的收藏频道                    │
│ 类型: 图片 + 文字                     │
│ 来源: @crypto_news                   │
└──────────────────────────────────────┘

或（受保护内容 - 插件已开启）:
┌──────────────────────────────────────┐
│ ✅ 已保存 1 条消息（受保护提取）      │
│ 目标: 我的收藏频道                    │
│ 类型: 视频 + 文字                     │
│ 来源: @protected_ch                  │
│ ⚠️ 通过提取模式保存                   │
└──────────────────────────────────────┘

或（受保护内容 - 插件未开启）:
┌──────────────────────────────────────┐
│ ❌ 保存失败                          │
│ 原因: 源频道已开启内容保护            │
│ 该频道禁止转发/复制内容               │
└──────────────────────────────────────┘

或（相册）:
┌──────────────────────────────────────┐
│ ✅ 已保存 5 条消息（相册）            │
│ 目标: 我的收藏频道                    │
│ 类型: 5 张图片                       │
└──────────────────────────────────────┘
```

### Step 5: 过滤 & 内容修改引擎
- **产物**: 关键词过滤器 + 正则修改器 + 水印去除器
- **执行器**: Codex
- **文件**: `plugins/keyword_filter.py`, `plugins/regex_modifier.py`, `plugins/watermark_remover.py`
- **约束**:
  - 中间件模式：便宜的过滤先执行，昂贵的媒体处理最后
  - 每个 job 独立规则
  - 正则错误不应崩溃整个管道
- **验收**:
  - [ ] 白名单/黑名单过滤准确
  - [ ] 正则替换正确（含 Unicode/中文）
  - [ ] 水印文本去除有效
  - [ ] 错误正则不影响其他消息处理

### Step 6: Preview & Review 系统
- **产物**: `/preview` 命令 + 审计频道日志
- **执行器**: Codex
- **文件**: `features/preview.py`, `features/audit.py`, `bot/commands.py`
- **约束**: 核心差异化功能

**Preview 设计（沙箱测试）:**
```
用户发送: /preview <消息链接> [job名称]
Bot 回复:
┌─────────────────────────────┐
│ 📋 Preview 结果              │
│ Job: 加密新闻               │
│ 状态: ✅ 通过 / ❌ 已过滤    │
│                             │
│ 📝 原文:                    │
│ "BTC突破10万！加入我们..."    │
│                             │
│ ✏️ 修改后:                   │
│ "BTC突破10万！"              │
│ (应用规则: clean_ads)        │
└─────────────────────────────┘
```

**Review 设计（审计日志）:**
```
→ 审计频道自动记录:
[✅ 转发] Job: 加密新闻 | Source: @crypto_ch #452
  修改: 2处正则替换
[✅ 保存] 自我转发 | Source: @some_ch #789
  类型: 图片+文字 | 目标: 我的收藏频道
[❌ 过滤] Job: 开发动态 | Source: @dev_ch #189
  原因: 黑名单匹配 ("scam")
[❌ 保存失败] 自我转发 | Source: @protected_ch #101
  原因: 内容受保护 (CHAT_FORWARDS_RESTRICTED)
[⚠️ 错误] Job: 加密新闻 | FloodWait 30s, 已排队重试
```

- **验收**:
  - [ ] `/preview` 命令正确展示处理结果
  - [ ] 审计频道记录所有操作（转发/过滤/保存/错误）
  - [ ] 管理员收到严重错误 DM 通知

### Step 7: Bot 管理命令
- **产物**: 运维管理命令集
- **执行器**: Codex
- **文件**: `bot/admin.py`
- **命令列表**:
  - `/status` - 运行状态（在线时长、各 job 统计、自我转发统计）
  - `/reload` - 热重载配置（无需重启容器）
  - `/preview <msg_link> [job]` - 沙箱预览
  - `/pause [job]` / `/resume [job]` - 暂停/恢复指定 job
  - `/stats` - 转发统计（今日/本周/总计，含自我转发计数）
- **约束**: 仅 admin_id 可执行管理命令
- **验收**:
  - [ ] 所有命令正常响应
  - [ ] 非管理员无法执行管理命令
  - [ ] `/reload` 不中断现有连接

### Step 8: Docker 部署 & 运维
- **产物**: 生产级 Docker 部署配置
- **执行器**: Codex
- **文件**: `Dockerfile`, `docker-compose.yml`, `.env.example`, `scripts/healthcheck.py`
- **Docker Compose 关键配置**:
  ```yaml
  services:
    forwarder:
      build: .
      restart: unless-stopped
      volumes:
        - ./config.yaml:/app/config.yaml:ro
        - sessions:/app/sessions
        - data:/app/data
      env_file: .env
      healthcheck:
        test: ["CMD", "python", "scripts/healthcheck.py"]
        interval: 60s
        timeout: 10s
        retries: 3
      deploy:
        resources:
          limits:
            memory: 512M   # 从 256M 提升，留出媒体处理余量
      logging:
        driver: "json-file"
        options:
          max-size: "10m"
          max-file: "3"
  volumes:
    sessions:  # TG 会话文件持久化
    data:      # SQLite + 检查点持久化
  ```
- **验收**:
  - [ ] `docker compose up -d` 一键启动
  - [ ] 容器重启后会话不丢失
  - [ ] 健康检查正常工作
  - [ ] 日志轮转配置生效

### Step 9: 测试 & 文档
- **产物**: 单元测试 + 集成测试 + 使用文档
- **执行器**: Codex
- **文件**: `tests/`, `docs/setup.md`, `docs/config.md`
- **验收**:
  - [ ] 核心逻辑单元测试覆盖率 > 80%
  - [ ] 自我转发功能端到端测试（含相册、受保护内容边界）
  - [ ] 端到端集成测试通过（mock TG API）
  - [ ] 部署文档完整（从零到运行）

---

## 关键文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `pyproject.toml` | 新建 | 项目依赖和元数据 |
| `config/schema.py` | 新建 | Pydantic 配置模型（含 self_forward 配置） |
| `config/loader.py` | 新建 | YAML 加载 + 校验 + 环境变量替换 |
| `config.example.yaml` | 新建 | 示例配置（含自我转发配置） |
| `core/engine.py` | 新建 | 主转发引擎 |
| `core/ingress.py` | 新建 | Telegram 事件监听 |
| `core/dispatcher.py` | 新建 | 限流调度器 |
| `core/dedup.py` | 新建 | 消息去重 |
| `core/copier.py` | 新建 | copyMessage/copyMessages 封装 |
| `core/album_batcher.py` | 新建 | 相册聚合批处理器 |
| `features/self_forward.py` | 新建 | **自我转发核心逻辑** |
| `plugins/protected_extractor.py` | 新建 | **受保护内容提取插件**（默认关闭） |
| `plugins/keyword_filter.py` | 新建 | 关键词过滤器 |
| `plugins/regex_modifier.py` | 新建 | 正则修改器 |
| `plugins/watermark_remover.py` | 新建 | 水印去除 |
| `features/preview.py` | 新建 | Preview 沙箱功能 |
| `features/audit.py` | 新建 | 审计日志 |
| `bot/commands.py` | 新建 | Bot 命令路由 |
| `bot/admin.py` | 新建 | 管理命令 |
| `Dockerfile` | 新建 | 容器镜像 |
| `docker-compose.yml` | 新建 | 编排配置 |
| `scripts/healthcheck.py` | 新建 | 健康检查脚本 |

---

## 风险与缓解

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|----------|
| Telegram FloodWait/限流 | 高 | 高 | Token-bucket 限流器 + 指数退避 + per-chat 节奏控制 |
| Userbot 账号封禁 | 中 | 高 | 保守吞吐量、避免 spam 模式、Bot 模式降级 |
| 会话/Token 泄露 | 中 | 高 | .env 管理密钥、加密卷、最小权限 |
| 断线后重复转发 | 中 | 中 | 持久化去重键 (src_chat, msg_id, dst_chat) + 检查点 |
| 消息丢失 | 中 | 高 | 持久化队列 + 启动时追赶策略 |
| 受保护内容无法保存 | 高 | 中 | 可选 `protected_extractor` 插件自动 fallback（读取+下载+重传）；未启用时 fail-fast 告知用户 |
| 受保护提取引发封号 | 低 | 高 | 默认关闭，低频限速（5次/分钟），仅 userbot，私人目标频道，个人存档用途 |
| 相册消息顺序错乱 | 中 | 低 | grouped_id 聚合 + 2s 窗口批处理 |
| 正则错误导致崩溃 | 低 | 中 | try-except 隔离 + 错误正则告警 |
| 循环转发（目标也是源） | 低 | 高 | loop guard：发送者 ID 白名单 + 消息来源检测 |
| tgcf 上游变化 | 低 | 低 | Fork 独立维护，按需 cherry-pick |

---

## 消息类型支持矩阵（自我转发）

| 消息类型 | copyMessage 支持 | 特殊处理 |
|----------|-----------------|----------|
| 文本 | ✅ | 无 |
| 图片 | ✅ | 无 |
| 视频 | ✅ | 无 |
| 文件/文档 | ✅ | 无 |
| Sticker | ✅ | 无 |
| 语音消息 | ✅ | 无 |
| 视频笔记 | ✅ | 无 |
| 相册 | ✅ | 需 grouped_id 聚合 + copyMessages 批处理 |
| 联系人/位置 | ✅ | 无 |
| Poll/投票 | ⚠️ 部分 | Quiz 模式有限制 |
| Service Message | ❌ | 不可 copy，忽略 |
| Invoice/支付 | ❌ | 不可 copy，忽略 |
| 受保护内容 | ❌ copyMessage 不支持 | 可选插件：读取+下载+重传（Userbot only） |

---

## SESSION_ID（供 /ccg:execute 使用）
- CODEX_SESSION: 019cf0f2-4e3d-7f91-bf02-84aceda264a4
- GEMINI_SESSION: 76f8fc59-433a-442a-affd-1ab9f706f923
