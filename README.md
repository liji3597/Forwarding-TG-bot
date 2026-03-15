# TG Forwarder

Telegram 消息自动转发工具，基于 [Telethon](https://github.com/LonamiWebs/Telethon) 构建。支持 Userbot 和 Bot 双模式，通过 YAML 配置多条转发规则，开箱即用。

## 功能

- **多任务转发** — 在 YAML 中定义多条 job，各自指定来源/目标频道、过滤规则和文本替换
- **关键词过滤** — 白名单 / 黑名单，按关键词筛选要转发的消息
- **正则替换** — 对转发内容做文本清洗（去广告链接、格式化版本号等），支持模板复用
- **自转发** — 将别人发给你的转发消息自动保存到 Saved Messages 或指定频道，支持去署名、追加来源
- **受保护内容提取** — 对设置了"禁止转发"的频道，自动下载媒体后重新发送
- **相册处理** — 自动聚合同一相册的多条消息，批量转发
- **去重** — SQLite 持久化已转发记录，防止重复发送
- **速率控制** — Token Bucket 限流，自动处理 FloodWait 并指数退避重试
- **Bot 管理命令** — `/status` `/reload` `/preview` `/pause` `/resume` `/stats`
- **审计日志** — 转发事件自动推送到监控频道
- **热重载** — 运行中修改配置后 `/reload` 即时生效，无需重启
- **Docker 部署** — 多阶段构建，开箱即用

## 快速开始

### 前置条件

- Python 3.11+
- 一个 Telegram 账号（获取 `api_id` / `api_hash`：https://my.telegram.org）
- （可选）一个 Bot Token（通过 [@BotFather](https://t.me/BotFather) 创建）

### 安装

```bash
git clone https://github.com/liji3597/Forwarding-TG-bot.git
cd Forwarding-TG-bot
pip install .
```

### 配置

复制示例配置并填写你的凭据：

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`：

```yaml
sessions:
  userbot:
    api_id: 12345678
    api_hash: "your_api_hash"
    phone: "+8613800138000"
  bot:
    token: "123456:ABC-DEF..."

jobs:
  - name: "加密新闻"
    source: -100111111111     # 来源频道 ID
    target: -100222222222     # 目标频道 ID
    mode: userbot
    filters:
      whitelist: ["BTC", "ETH"]
      blacklist: ["scam", "airdrop"]
    use_template: clean_ads   # 引用模板

templates:
  clean_ads:
    replacements:
      - regex: "(?i)join\\s+us\\s+at.*"
        replace: ""
      - regex: "https?://t\\.me/\\S+"
        replace: "[链接已移除]"
```

也可使用环境变量：创建 `.env` 文件写入 `TG_API_ID`、`TG_API_HASH`、`BOT_TOKEN`，在 YAML 中用 `${TG_API_ID}` 引用。

### 运行

```bash
tg-forwarder --config config.yaml
```

首次运行会要求输入手机验证码完成 Telethon 登录，session 文件保存在 `data/` 目录。

### Docker 部署

```bash
# 编辑好 config.yaml 和 .env 后
docker compose up -d
```

## 配置说明

| 字段 | 说明 |
|------|------|
| `sessions.userbot` | Userbot 凭据（api_id / api_hash / phone） |
| `sessions.bot` | Bot Token（可选，启用 Bot 模式和管理命令需要） |
| `jobs[]` | 转发任务列表 |
| `jobs[].source` / `target` | 频道 ID（以 `-100` 开头） |
| `jobs[].mode` | `userbot` 或 `bot` |
| `jobs[].filters` | 关键词白/黑名单 |
| `jobs[].modifications` | 单任务正则替换规则 |
| `jobs[].use_template` | 引用 `templates` 中定义的共享替换模板 |
| `templates` | 可复用的文本替换模板 |
| `self_forward` | 自转发配置（保存转发消息到 Saved Messages） |
| `protected_extractor` | 受保护内容提取设置 |
| `monitoring` | 审计频道和管理员 ID |

完整字段参考 [`config.example.yaml`](config.example.yaml)。

## 项目结构

```
tg_forwarder/
├── __main__.py          # 入口
├── config/
│   ├── loader.py        # YAML 加载 + 环境变量替换
│   └── schema.py        # Pydantic 配置模型
├── core/
│   ├── engine.py        # 转发引擎主循环
│   ├── ingress.py       # 消息监听适配器
│   ├── dispatcher.py    # 调度器 + Token Bucket 限流
│   ├── copier.py        # 消息复制
│   ├── album_batcher.py # 相册聚合
│   └── dedup.py         # SQLite 去重
├── features/
│   ├── self_forward.py  # 自转发
│   ├── preview.py       # 消息预览
│   └── audit.py         # 审计日志
├── plugins/
│   ├── keyword_filter.py      # 关键词过滤
│   ├── regex_modifier.py      # 正则替换
│   ├── protected_extractor.py # 受保护内容提取
│   └── watermark_remover.py   # 水印清理
└── bot/
    ├── commands.py      # Bot 命令路由
    └── admin.py         # 管理命令实现
```

## 技术栈

- [Telethon](https://github.com/LonamiWebs/Telethon) — Telegram MTProto 客户端
- [Pydantic](https://docs.pydantic.dev/) v2 — 配置校验
- [aiosqlite](https://github.com/omnilib/aiosqlite) — 异步 SQLite（去重存储）
- [PyYAML](https://pyyaml.org/) — YAML 解析

## License

[MIT](LICENSE)
