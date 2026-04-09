# ⭐ Star 星星走起 动动发财手点点 ⭐

## ClawCloud 官网(GitHub注册送5美元地址)：[run.claw.cloud](https://console.run.claw.cloud/signin?link=M9P7GXP3M3W5)

---

> 自动登录保活，当前支持：
> - ClawCloud（GitHub 登录，支持设备验证 + 两步验证）
> - Lunes Host（邮箱密码登录，优先复用已登录会话）
> - Daily Tech Digest（每天抓取 AI / 云原生 / 框架 / 免费平台热点，输出 Markdown 并推送 Telegram）

![设备验证](./3.png)

---

## ⚠️ 注意事项

- 支持 **Mobile验证** 和 **2FA验证** `建议:Mobile验证 直接下载github app 只需要首次验证之后，后面不需要再次验证。`
- 首次运行：需要设备验证，收到 TG 通知后 **30 秒内** 批准
- REPO_TOKEN：需要有 `repo` 权限才能自动更新 Cookie
- Cookie 有效期：每次运行都会更新，保持最新

---

## 🔐 Secrets 配置

| Secret 名称 | 必需 | 说明 |
|-------------|------|------|
| `GH_USERNAME` | ✅ | GitHub 用户名 |
| `GH_PASSWORD` | ✅ | GitHub 密码 |
| `REPO_TOKEN` | ✅ | GitHub PAT（用于自动更新 Cookie） |
| `TG_BOT_TOKEN` | ✅ | Telegram Bot Token |
| `TG_CHAT_ID` | ✅ | Telegram Chat ID |
| `GH_SESSION` | ❌ | 自动生成，无需手动添加 |
| `GH_TOTP_SECRET` | 推荐 | GitHub Authenticator App 的 TOTP 密钥，配置后可自动填写 2FA 验证码 |
| `LUNES_EMAIL` | Lunes 可选 | Lunes Host 登录邮箱 |
| `LUNES_PASSWORD` | Lunes 可选 | Lunes Host 登录密码 |
| `LUNES_STORAGE_STATE` | Lunes 可选 | 自动生成的浏览器会话，建议保留 |
| `AI_API_KEY` | Digest 可选 | OpenAI-compatible 总结接口密钥，不配则回退为规则摘要 |

> `ClawCloud` 使用 `GH_*` 这组 Secret；`Lunes Host` 使用 `LUNES_*` 这组 Secret。  
> `Daily Tech Digest` 使用 `TG_*` + `AI_*` + `DIGEST_*` 这组配置。  
> 三个工作流互相独立，可以只配置你需要的那一组。

### Daily Tech Digest 需要的 Variables

建议在仓库 `Settings -> Secrets and variables -> Actions -> Variables` 里新增下面这些变量：

| Variable 名称 | 默认值 | 说明 |
|---------------|--------|------|
| `AI_BASE_URL` | 空 | OpenAI-compatible 接口根地址，例如 `https://your-gateway.example.com/v1` |
| `AI_MODEL` | 空 | 用于总结的模型名 |
| `AI_ENABLED` | `true` | 是否启用 AI 总结 |
| `AI_TIMEOUT_SECONDS` | `60` | AI 请求超时秒数 |
| `DIGEST_MAX_ITEMS` | `12` | 每日最多保留多少条高信号内容 |
| `DIGEST_DEDUPE_DAYS` | `7` | 跨天去重窗口 |
| `DIGEST_OFFICIAL_FEEDS` | 空 | 额外 RSS/Atom 源，支持 JSON 数组或换行分隔 URL |
| `DIGEST_ARXIV_FEEDS` | 空 | 自定义 arXiv RSS 覆盖项，不填则使用内置默认源 |
| `DIGEST_BROWSER_TARGETS_FILE` | `config/browser_targets.json` | 浏览器爬虫目标配置文件 |
| `DIGEST_BROWSER_TIMEOUT_SECONDS` | `240` | 浏览器爬虫总超时 |

`Daily Tech Digest` 默认每天北京时间 `09:00` 执行，对应 workflow: [`.github/workflows/daily-tech-digest.yml`](./.github/workflows/daily-tech-digest.yml)。

说明：
- `AI_ENABLED` 不手动配置也可以，默认按 `true` 处理。
- `AI_BASE_URL` 和 `AI_MODEL` 优先读 `Variables`，也兼容放在 `Secrets`。
- `AI_API_KEY` 默认从 `Secrets` 读取。
- 浏览器爬虫默认读取 `config/browser_targets.json`，你也可以通过 `DIGEST_BROWSER_TARGETS_FILE` 指向别的配置文件。

### Daily Tech Digest 输出内容

- 默认抓取稳定源：`arXiv RSS`、`Hacker News`、`GitHub Trending`、以及你额外配置的官方 RSS/Atom。
- 已集成 `Lightpanda + Puppeteer/Playwright` 轻量浏览器爬虫层，可抓取任何你在配置里定义的网站。
- 简报内容默认中文优先，专有名词、项目名、模型名保留原文。
- 输出文件会自动写到独立 `reports` 分支下的 `reports/digests/YYYY/MM/DD.md`。
- Telegram 会同时发送：`摘要消息 + 完整 Markdown 文件`。
- 目前 `X/Twitter` 只预留了适配接口，方便后续接你自己的爬虫结果。

### 浏览器爬虫配置

- 默认配置文件：[`config/browser_targets.json`](./config/browser_targets.json)
- 示例配置文件：[`config/browser_targets.example.json`](./config/browser_targets.example.json)
- 仓库已经预置一组 starter targets：`Bun`、`Deno`、`Cloudflare`、`Vercel`、`Railway`、`Fly.io`、`Render`、`Hugging Face`
- 默认只启用当前验证较稳定的 `Bun / Deno / Cloudflare`；其余目标保留在配置里，但默认 `enabled: false`，需要你逐站开启和微调
- 每个 target 支持：
  - `engine`: `puppeteer` 或 `playwright`
  - `steps`: 打开页面、等待元素、点击、输入、滚动、执行页面内脚本
  - `extract.mode`:
    - `script`: 直接在页面上下文返回标准化数组，适合复杂站点
    - `selector`: 用选择器提取列表，适合结构稳定的站点

标准化输出字段：

- `source`
- `title`
- `url`
- `published_at`
- `raw_summary`
- `category_hint`
- `signals`
- `metadata`

---

## 🆕 Lunes Host 登录保活说明

目标站点：`https://betadash.lunes.host/`

### 登录方式差异

`Lunes Host` 不是 GitHub OAuth，它是：

- 邮箱 + 密码登录
- 登录页带 Cloudflare Turnstile
- 页面提示：超过 6 个月不活跃会重置密码

所以这个项目对 `Lunes` 的实现策略不是“每次都从零登录”，而是：

1. 优先加载上次成功登录后保存的浏览器会话 `LUNES_STORAGE_STATE`
2. 遇到 Cloudflare challenge 时，先等待页面稳定，再自动点击验证框
3. 访问首页完成保活
4. 成功后自动刷新 `LUNES_STORAGE_STATE`
5. 如果会话失效，再尝试用 `LUNES_EMAIL` 和 `LUNES_PASSWORD` 重新登录

`Lunes Host 登录保活` workflow 当前定时为：每 7 天 UTC `00:00` 运行一次。

### 需要添加的 Secrets

如果你只是想把 `Lunes Host` 跑起来，最低只需要这 3 个：

| Name | 是否必填 | 用途 |
|------|----------|------|
| `LUNES_EMAIL` | ✅ 首次建议必填 | Lunes 登录邮箱 |
| `LUNES_PASSWORD` | ✅ 首次建议必填 | Lunes 登录密码 |
| `REPO_TOKEN` | ✅ | 用来自动回写 `LUNES_STORAGE_STATE`，否则每次都很难稳定续期 |

推荐再加上通知：

| Name | 是否必填 | 用途 |
|------|----------|------|
| `TG_BOT_TOKEN` | 推荐 | Telegram 通知 token |
| `TG_CHAT_ID` | 推荐 | Telegram 接收通知的 chat id |

下面这个 Secret 不需要你一开始手填，脚本首次成功后会自动生成：

| Name | 是否必填 | 用途 |
|------|----------|------|
| `LUNES_STORAGE_STATE` | 否，自动生成 | 保存浏览器登录态。后续保活优先靠它，不再频繁重新登录 |

完整说明如下：

| Name | 必需 | 说明 |
|------|------|------|
| `LUNES_EMAIL` | 首次建议 ✅ | Lunes 登录邮箱 |
| `LUNES_PASSWORD` | 首次建议 ✅ | Lunes 登录密码 |
| `LUNES_STORAGE_STATE` | 否 | 首次成功后自动生成，后续保活优先用它 |
| `REPO_TOKEN` | ✅ | 用于自动刷新 `LUNES_STORAGE_STATE` |
| `TG_BOT_TOKEN` | 推荐 | 失败/成功通知 |
| `TG_CHAT_ID` | 推荐 | 失败/成功通知 |

### 默认不用配置的变量

如果你不改代码，下面这些参数都有默认值，不需要额外在 GitHub Secrets / Variables 里配置：

| 变量名 | 默认值 | 作用 |
|--------|--------|------|
| `LUNES_BASE_URL` | `https://betadash.lunes.host` | Lunes 站点地址 |
| `LUNES_STORAGE_SECRET_NAME` | `LUNES_STORAGE_STATE` | 登录态保存到哪个 Secret 名称 |
| `LUNES_PROXY_DSN` | 空 | 浏览器代理 |
| `LUNES_TURNSTILE_WAIT` | `25` | 等待 Turnstile token 的秒数 |
| `LUNES_POST_LOGIN_WAIT` | `20` | 登录提交后等待跳转的秒数 |
| `LUNES_CLOUDFLARE_CLICK_DELAY` | `6` | 发现 Cloudflare challenge 后，点击前等待的秒数 |
| `LUNES_CLOUDFLARE_MAX_ATTEMPTS` | `3` | 自动点击 Cloudflare challenge 的最大尝试次数 |

### 首次初始化建议

1. 先添加 `LUNES_EMAIL`、`LUNES_PASSWORD`、`REPO_TOKEN`
2. 建议同时添加 `TG_BOT_TOKEN`、`TG_CHAT_ID`，方便看失败截图和通知
3. 手动运行 `Lunes Host 登录保活` workflow
4. 如果站点允许本次自动通过 Turnstile，脚本会自动生成并更新 `LUNES_STORAGE_STATE`
5. 后续定时任务就会优先复用这个会话，稳定性会比每次重新登录更高

### 一句话结论

对 `Lunes Host` 来说，你实际需要关心的只有：

- 必填：`LUNES_EMAIL`、`LUNES_PASSWORD`、`REPO_TOKEN`
- 推荐：`TG_BOT_TOKEN`、`TG_CHAT_ID`
- 自动生成：`LUNES_STORAGE_STATE`

### 本地初始化登录态

如果 `GitHub Actions` 一直卡在 Cloudflare 验证页，推荐改用本地初始化一次登录态：

1. 本地安装并确认 `opencli` 已经可以复用你的真实浏览器会话：

```bash
opencli web read --url https://betadash.lunes.host/
```

如果上面的命令可以成功返回页面内容，说明 Browser Bridge 已经打通。

2. 先在你的真实浏览器里手动登录 `https://betadash.lunes.host/`

3. 运行初始化脚本：

```bash
python scripts/lunes_init_session.py
```

4. 脚本会做两件事：

- 先执行一次 `opencli web read --url https://betadash.lunes.host/` 触发浏览器桥接
- 再从 `opencli daemon` 导出 `betadash.lunes.host` 的 cookies，并转换成 `LUNES_STORAGE_STATE`

5. 成功后，仓库目录会生成 `LUNES_STORAGE_STATE.txt`

6. 打开这个文件，把里面整段内容复制到 GitHub 仓库 Secret：

- Secret 名称：`LUNES_STORAGE_STATE`

这样后续 GitHub Actions 就不需要每次从零登录，而是优先复用你本地真实浏览器初始化出来的会话。

### 重要限制

- 因为 `Lunes Host` 登录页带 Turnstile，`GitHub Actions` 环境下不保证每次都能重新登录成功
- 一旦 `LUNES_STORAGE_STATE` 已建立，后续保活通常只需要复用会话，不会频繁触发重新登录
- 如果后续日志里持续提示还停留在登录页，通常就是会话过期且本次 Turnstile 未通过，需要重新手动触发一次初始化

---

# 🚀 完整操作指南 - 分步骤详解

---

## 第一步：Fork 仓库

```
1. 打开原仓库页面
2. 点击右上角 "Fork" 按钮
3. 点击 "Create fork"
4. 等待跳转到你的仓库副本
```

---

## 第二步：创建 Telegram Bot

### 2.1 创建 Bot

```
1. Telegram 搜索 @BotFather
2. 发送 /newbot
3. 输入名称: ClawCloud Alert
4. 输入用户名: clawcloud_xxx_bot（需唯一）
5. 保存获得的 Token: 6123456789:AAHxxxxx...
```

### 2.2 获取 Chat ID

```
1. 找到刚创建的 Bot，发送: hello
2. 浏览器访问: https://api.telegram.org/bot<你的Token>/getUpdates
3. 找到 "chat":{"id":123456789}
4. 保存这个数字: 123456789
```

---

## 第三步：启用 GitHub 验证方式

> 选择你要使用的验证方式进行设置

### 方式 A：启用 GitHub Mobile 验证

#### A.1 安装 GitHub Mobile App

```
iOS: App Store 搜索 "GitHub"
Android: Google Play 搜索 "GitHub"

安装后登录你的 GitHub 账号
```

#### A.2 首次开启两步验证

```
1. 浏览器打开: https://github.com/settings/security
2. 找到 "Password and authentication"
3. 点击绿色 "Enable two-factor authentication"
4. 选择 "GitHub Mobile" 选项
5. 点击旁边的 "Show" 查看设置
6. 按提示在手机 App 上确认绑定
```

#### A.3 设置为首选验证方式（如已开启2FA）

```
1. 浏览器打开: https://github.com/settings/security
2. 找到 "Two-factor methods"
3. 找到 "GitHub Mobile"
4. 点击 "Set as preferred" 设为首选
```

![设置Mobile优先验证](./2.png)

#### A.4 验证设置成功

```
确认显示:
GitHub Mobile ✓ Preferred
```

---

### 方式 B：启用 2FA (TOTP) 验证

#### B.1 下载 Authenticator App

```
推荐应用（任选其一）:
- Google Authenticator
- Microsoft Authenticator
- Authy
- 1Password
```

#### B.2 首次开启两步验证

```
1. 浏览器打开: https://github.com/settings/security
2. 找到 "Password and authentication"
3. 点击绿色 "Enable two-factor authentication"
4. 选择 "Authenticator app" 选项
5. 用 Authenticator App 扫描二维码
6. 输入 App 显示的 6 位验证码确认
7. 保存恢复码（Recovery codes）到安全位置
```

#### B.3 验证设置成功

```
确认 Security 页面显示:
Authenticator app ✓
```

> 💡 **使用方法见 [第六步：响应验证请求](#第六步响应验证请求)**

---

## 第四步：配置 GitHub Secrets

### 4.1 进入 Secrets 页面

```
你的仓库 → Settings → Secrets and variables → Actions
```

### 4.2 添加 GitHub PAT

```
1. 打开: https://github.com/settings/tokens
2. Generate new token (classic)
3. Note: ClawCloud
4. Expiration: No expiration
5. 勾选: ✅ repo
6. Generate token → 复制 Token
7. 回到 Secrets 添加:
   Name: REPO_TOKEN
   Secret: ghp_xxxxxxxxxxxx
```

### 4.3 添加其他 Secrets

点击 "New repository secret" 依次添加：

| Name | Secret（填入的值） |
|------|-------------------|
| `GH_USERNAME` | 你的 GitHub 用户名 |
| `GH_PASSWORD` | 你的 GitHub 密码 |
| `TG_BOT_TOKEN` | 第二步的 Token: `6123456789:AAHxxxxx...` |
| `TG_CHAT_ID` | 第二步的 Chat ID: `123456789` |
| `REPO_TOKEN` | 第四步的4.2 GitHub PAT: `ghp_xxxxxxxxxxxx` |

### 4.4 确认完成

```
应该有 5 个 Secrets:
✅ GH_USERNAME
✅ GH_PASSWORD
✅ REPO_TOKEN
✅ TG_BOT_TOKEN
✅ TG_CHAT_ID
```

---

## 第五步：启用 Actions 并运行

### 5.1 启用 Actions

```
1. 点击仓库顶部 "Actions"
2. 点击 "I understand my workflows, go ahead and enable them"
```

### 5.2 手动运行测试

```
1. 左侧点击 "ClawCloud 自动登录保活"
2. 点击 "Run workflow"
3. 点击绿色 "Run workflow" 按钮
```

### 5.3 查看运行日志

```
点击新出现的运行记录 → 点击 "auto-login" 查看实时日志
```

---

## 第六步：响应验证请求

> 运行时根据你设置的验证方式进行操作

### 如果使用 Mobile 验证

```
1. 收到 Telegram 通知: "🔐 需要 GitHub Mobile 验证"
2. 30秒内打开手机 GitHub App
3. 输入通知中显示的数字
4. 完成 ✅
```

![Mobile验证](./1.png)

---

### 如果使用 2FA 验证

```
1. 收到 Telegram 通知: "🔐 需要两步验证码"
2. 打开 Authenticator App 查看 6 位验证码
3. 在 Telegram 发送: /code 847293
4. 完成 ✅
```

![2FA验证](./4.png)

---

## ✅ 完成检查

```
✅ Fork 完成
✅ Telegram Bot 创建完成
✅ GitHub 验证方式已设置（Mobile 或 2FA）
✅ 5 个 Secrets 已添加
✅ Actions 已启用
✅ 首次运行成功
```

## **🎉 配置完成！**

---

## 📊 流程图

```
┌─────────────────────────────────────────────────────────┐
│  1. 打开 ClawCloud 登录页                                │
│         ↓                                               │
│  2. 点击 "GitHub" 登录按钮                               │
│         ↓                                               │
│  3. GitHub 认证                                         │
│     ├── 输入用户名/密码                                  │
│     ├── 设备验证 (如需要) → 等待30秒/邮件批准             │
│     └── 两步验证 (如需要)                                │
│         ├── GitHub Mobile → 等待手机批准                 │
│         └── TOTP → 通过 Telegram /code 123456 输入       │
│         ↓                                               │
│  4. OAuth 授权 (如需要)                                  │
│         ↓                                               │
│  5. 等待重定向回 ClawCloud                               │
│         ↓                                               │
│  6. 保活操作 (访问控制台/应用页面)                        │
│         ↓                                               │
│  7. 提取新 Cookie 并保存/通知                            │
└─────────────────────────────────────────────────────────┘
```

---

## 📁 文件结构

```
.
├── .github/
│   └── workflows/
│       └── auto_login.yml    # GitHub Actions 配置
├── scripts/
│   └── auto_login.py         # 自动登录脚本
├── 1.png                      # Mobile 验证截图
├── 2.png                      # 设置截图
├── 3.png                      # 主截图
├── 4.png                      # 2FA 截图
└── README.md
```

---

## 🐛 常见问题

### Q: 设备验证超时怎么办？
A: 确保 Telegram 通知已配置，收到通知后立即在邮箱或 GitHub App 批准。

### Q: 2FA 验证码怎么输入？
A: 在 Telegram 发送 `/code 123456`（替换为你的 6 位验证码）。

### Q: Cookie 更新失败？
A: 检查 `REPO_TOKEN` 是否有 `repo` 权限。

### Q: 为什么需要 GitHub 密码？
A: 用于 Cookie 失效时重新登录，密码存储在 GitHub Secrets 中，安全可靠。

---

## 📄 License

MIT License

---

## 🤝 贡献

[感谢：axibayuit-a11y佬](https://github.com/axibayuit-a11y) 优化：支持了2fa验证

欢迎提交 Issue 和 Pull Request！

⭐ 如果对你有帮助，请点个 Star 支持一下！
