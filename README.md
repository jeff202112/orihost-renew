# Orihost 免费服务器自动续期（帐号密码版）

基于 Jexactyl 面板的自动续期脚本，解决免费容器 7 天过期删机问题。

**用邮箱 + 密码登录**（自动过 Cloudflare Turnstile），不再需要手动抓 `remember_web` token 或 Cookie —— 和 [katabump](https://github.com/) 的用法一致：本地放一个 `orihost_login.json`，Actions 里填一条 JSON Secret `ORIHOST_ACCOUNTS`（多账号就是 JSON 数组）。

## 文件结构

```text
orihost-renew/
├── orihost_browser_renew.py    # 唯一脚本：API预检 + 帐号密码登录 + 浏览器续期
├── orihost_login.json          # 本地帐号文件（含密码，已在 .gitignore 里，切勿提交）
├── requirements.txt            # 依赖：playwright + requests
├── .github/workflows/renew-browser.yml  # GitHub Actions 定时任务（每 5 天）
└── README.md
```

## 续期原理

面板前端扒出来的真实流程（`assets/bundle.*.js`）：

1. `/auth/login` 填邮箱密码 → 先过 Cloudflare Turnstile（`SiteConfiguration.turnstile.login=true`）→ 登录拿到会话
2. `/server/{id}` 点 `Renew` → 弹窗 `Renew your server`
3. 弹窗点 `Read Article` → `POST /api/client/servers/{id}/renew/begin` 返回文章链接 + `dwell_seconds`，`window.open` 新标签打开文章
4. 回到面板后需等约 10s（`dwell_seconds`）：倒计时走完 → 弹窗内出现 Turnstile → 过验证 → `Claim Renewal` 由 `disabled` 变为可点
5. 点 `Claim Renewal` → `GET /api/client/renewal/complete?cf-turnstile-response=xxx` 完成续期（+7 天）→ 页面 reload（此时按钮常变成 `Renew Limit Reached`，属正常，以续期次数是否增加为准）

`complete` 强制要 Turnstile token，无 token 直接 500，所以必须跑**浏览器版**（真浏览器点验证，过盾方案移植自 katabump）。

> **为什么以前一直失败？**（本版已全部实测修好）
> 1. **Cloudflare Turnstile 过不去**：用 Playwright 的 `launch()` 起浏览器会带上自动化特征，Cloudflare 一眼识破，checkbox 怎么点都不通过。本版改成 **原生启动 Chrome + CDP 连接**（同 katabump 的思路），实测 1 次点击即过。
> 2. **找不到 Claim Renewal**：orihost 的广告网络会注入全屏“假人机验证”弹窗挡住按钮。本版用 **请求白名单**（只放行 `panel.orihost.com` + Cloudflare）从源头掉广告脚本。
> 3. **清理遮罩误删弹窗**：Renew 弹窗是用 portal 挂到 `body` 下的（不在 `#app` 里），粗暴删大块 fixed 元素会把弹窗一起删掉，`Read Article` 就点不到了。本版清理遮罩时 **有弹窗就什么都不动**。
> 4. **接口路径**：这个面板的服务器列表端点是 `/api/client`（`/api/client/servers` 会 404），登录判定也不能只看 URL。

## 一、本地运行（Windows / macOS / Linux）

```bash
pip install -r requirements.txt
python -m playwright install chromium     # 装 Chrome for Testing（就是真 Chrome 内核），够用
```

> **不用**执行 `python -m playwright install chrome`。那条命令只认 Ubuntu / Debian，在 Linux Mint 等发行版会直接报
> `ERROR: cannot install on linuxmint distribution - only Ubuntu and Debian are supported`，属于正常现象，不影响使用。
> 脚本会自动在 Playwright 缓存里找到这份 Chrome for Testing 并直接启动，无需系统再装 Google Chrome。
> 想指定别的浏览器就用环境变量 `CHROME_PATH` 指到可执行文件即可。

在脚本同目录建一个 `orihost_login.json`（格式和 katabump 的 `login.json` 一样）：

```json
[
    {
        "username": "your_email@example.com",
        "password": "your_password"
    }
]
```

服务器 ID 可以不用填（脚本会自动续该账号下所有服务器）；要指定的话，进面板点开服务器，地址栏 `/server/` 后面那段 8 位短 ID 就是：

```text
https://panel.orihost.com/server/d5e9678b
                                  └─ 8 位短 ID ─┘
```

多台用英文逗号分隔，加个 `servers` 字段即可：

```json
[{ "username": "a@b.com", "password": "pwd", "servers": "id1,id2" }]
```

运行（**默认有头**：headless 过不了盾。有桌面直接跑；纯 SSH/服务器装个 `xvfb`，脚本会自动套虚拟显示）：

```bash
python orihost_browser_renew.py                            # 有桌面 / 已装 xvfb（自动套）
HEADLESS=0 xvfb-run -a python orihost_browser_renew.py     # 手动显式用 xvfb
```

> 无桌面又没装 `xvfb` 的话，只能退回 headless —— 此时 Cloudflare 几乎必拦。先 `sudo apt-get install -y xvfb`。

走代理（本机能连上的 http/socks 代理）：

```bat
set ORIHOST_PROXY=http://127.0.0.1:7890
python orihost_browser_renew.py
```

## 二、GitHub Actions 部署（推荐）

1. 新建仓库，把本目录文件推上去（`orihost_browser_renew.py` 放仓库根目录）
2. 进仓库 `Settings → Secrets and variables → Actions`，点 `Secrets` 页签 → `New repository secret`：

| 名称 | 必填 | 说明 |
|---|---|---|
| `ORIHOST_ACCOUNTS` | 是 | 帐号 JSON 数组，**一个 Secret 装下所有账号**（示例见下方「多账号」）；某账号要限定机器就加 `"servers":"id1,id2"` |
| `TG_BOT_TOKEN` | 否 | Telegram 机器人 token |
| `TG_CHAT_ID` | 否 | Telegram 聊天 ID |
| `NODE_LINK` | 否 | 代理节点完整分享链接（vless/vmess/trojan/hysteria2/tuic/anytls/socks5），不填则直连 |
| `ORIHOST_PROXY` | 否 | 手动指定的 http(s)/socks 代理，如 `http://127.0.0.1:1081`；节点链接填 `NODE_LINK` |

3. 去 `Actions → Orihost Browser Renew → Run workflow` 手动跑一次，TG 能收到推送即正常
4. 定时默认 `0 10 1,6,11,16,21,26 * *`（每 5 天，北京时间 18:00），7 天有效期提前续是故意的，不要改成 7 天
   - 用「每月 1/6/11/16/21/26 日」而不是 `*/5`：cron 的 day-of-month 从 1 起算，`*/5` 会落在 31 日，月末 31→次月 1 会连着跑两次（无害但会多跑一轮）
   - 跨月最长间隔 6 天（如 3/26 → 4/1），仍小于 7 天有效期，不会断档

### 多账号

不再需要一堆 `ORIHOST_EMAIL_1` / `ORIHOST_PASSWORD_1`：只建**一条** Secret `ORIHOST_ACCOUNTS`，值填 JSON 数组，一个账号一个对象（单账号同样用它，数组里放一个对象即可）：

```json
[
  {"email": "a@b.com", "password": "pwd"},
  {"email": "c@d.com", "password": "pwd2"}
]
```

- 某个账号只想续部分机器，就在它的对象里加 `"servers": "id1,id2"`（服务器短 ID，逗号分隔）；不加则自动续该账号下全部服务器
- 把这份 JSON 直接粘进 `ORIHOST_EMAIL` 也认（老 Secret 只改值、不改名就能用）
- 本地/环境变量仍兼容老写法：`ORIHOST_EMAIL` + `ORIHOST_PASSWORD`（可带 `_1.._N` 后缀做多账号）

## 三、环境变量全表

| 变量 | 默认 | 说明 |
|---|---|---|
| `ORIHOST_ACCOUNTS` | 空 | 帐号 JSON 数组（Actions 里建同名 Secret），值形如 `[{"email":"a@b.com","password":"pwd"}]`；同一份 JSON 粘进 `ORIHOST_EMAIL` 也认 |
| `ORIHOST_EMAIL` / `ORIHOST_PASSWORD` | 空 | 兼容老写法：面板登录帐号密码（可带 `_1.._N` 后缀做多账号），本地跑常用 |
| `ORIHOST_LOGIN_FILE` | `orihost_login.json` | 本地帐号文件路径 |
| `ORIHOST_SERVER_IDS` | 空 | 服务器短 ID，逗号分隔；空则自动列账号下全部 |
| `ORIHOST_PANEL` | `https://panel.orihost.com` | 面板地址 |
| `HEADLESS` | `0` | `0` = 有头（默认，过盾必需）；无桌面时自动套 xvfb；设 `1` 强制无头（过盾率低） |
| `CHROME_PATH` | 空 | 直接指定 Chrome 可执行文件路径（不填自动找 google-chrome / Playwright 的 Chrome for Testing） |
| `CHROME_NO_SANDBOX` | 空 | `1` = 给 Chrome 加 `--no-sandbox --disable-dev-shm-usage`（CI/容器里 sandbox 起不来时用；`GITHUB_ACTIONS=true` 时自动开） |
| `ORIHOST_DEBUG_PORT` | `9222` | 原生 Chrome 的远程调试端口（被占会自动往后找） |
| `ORIHOST_USER_DATA_DIR` | `ChromeData_Orihost` | 浏览器 profile 目录（保留登录态 / 信任度） |
| `CLAIM_TIMEOUT` | `300` | 等 `Claim Renewal` 出现的最长秒数 |
| `DWELL_MIN` | `10` | `Read Article` 后至少等待秒数（面板约 10s 后才让 Claim 生效；有 `dwell_seconds` 时取更大值） |
| `CLAIM_READY_TIMEOUT` | `120` | 等 `Claim Renewal` 真正可点（去掉 disabled）的最长秒数 |
| `TS_APPEAR_TIMEOUT` | `90` | 等弹窗内 Turnstile 组件出现的最长秒数 |
| `COOLDOWN_WAIT` | `600` | 弹窗冷却倒计时最长等待秒数 |
| `REREAD_TIMES` | `3` | 倒计时被重置后重读文章的次数 |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 空 | Telegram 通知 |
| `ORIHOST_PROXY` / `ORIHOST_GOST_PROXY` | 空 | http(s)/socks 代理 |
| `GH_TOKEN` | 空 | 带 `repo`+`workflow` 的 classic PAT，cron 回写推 workflow 文件用 |
| `DRY_RUN` | 空 | `true` 时跳过 cron 回写 |

## 四、常见问题

- **GitHub Actions 里卡在登录盾 / 直连失败**：runner 是机房 IP，Cloudflare 拦截率高。**强烈建议配置代理**：仓库 Secrets 里加 `NODE_LINK`（节点链接，workflow 会自动起 sing-box 并让 Chrome 走 `socks5://127.0.0.1:1080`），或直接设 `ORIHOST_PROXY`（优先于 sing-box）。脚本在 CI 里检测到未走代理会打印提醒
- **登录报 Turnstile 未通过**：
  - 日志出现 `(headless)` 说明在跑无头 —— 必过不去。脚本默认有头；无桌面请装 `xvfb`（脚本会自动 `xvfb-run`）
  - 换个出口 IP（把节点链接填 `NODE_LINK` 走代理）；主机 IP 被 Cloudflare 拉黑时，怎么点都过不去
- **该帐号开了两步验证**：脚本暂不支持 2FA，请在面板里关掉
- **卡在 “找不到 Claim Renewal” / 点了没反应**：本版已修。关键三点：① `Read Article` 后面板约 10s（`dwell_seconds`）才让 Claim 生效，脚本会先等这段时间；② 点 Claim 前必须先过弹窗里的 Turnstile（脚本轮询 `challenges.cloudflare.com` iframe 并 CDP 点击）；③ 按钮一开始是 `disabled` 的，脚本等它变可点再点，避免点在禁用态上白点。若还偶发，看 `shots/`（或 Actions 的 Artifacts）截图是不是面板改版换了按钮文案
- **浏览器报 “Chrome 调试端口未就绪” / profile 被锁**：上次的 Chrome 没退干净，脚本会自动复用已在运行的那个；实在不行 `pkill -f "chrome-linux64/chrom[e]"`（用 `[e]` 是为了别把执行命令的 shell 自己也杀掉）再跑
- **面板显示 Renew Limit Reached / complete 报 500**：续期次数已满（免费服常见上限约 7 次一档）。注意：**成功续期后按钮也会立即变成 `Renew Limit Reached`**，所以脚本不会只凭这句文案判跳过，而是再用 API 核对续期次数是否增加——增加了就是 `✅ 续期成功`，没增加才判 `⏭️ 跳过`。等天数消耗、空出次数后定时任务会自动再续
- **冷却中 xxxs 本轮跳过**：面板限流，超过 `COOLDOWN_WAIT` 脚本主动放弃，等下一轮
- **TG 收不到**：确认 `TG_BOT_TOKEN` 与 `TG_CHAT_ID` 都填了，且机器人已和你开过会话（先给机器人发一句话）
- **汇总 0 成功 0 跳过 N 失败**：看日志开头，确认 `pip install` 与 `playwright install` 步骤都成功

## 安全提醒

- 密码等同于面板登录态，只放 GitHub Secrets 或本地 `orihost_login.json`（已在 `.gitignore`），**不要**提交到代码、README 或 Actions 日志里
- 本仓库若为公开仓库，切勿把邮箱 / 密码写进任何被跟踪的文件
