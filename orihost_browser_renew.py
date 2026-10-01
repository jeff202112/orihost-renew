#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Orihost 免费服务器自动续期（Playwright + 帐号密码登录 + CDP 过 Turnstile）
#
# 面板真实流程（扒自 assets/bundle.*.js）：
#   1. /auth/login 填帐号密码 → 必须先过 Cloudflare Turnstile（SiteConfiguration.turnstile.login=true）
#      → POST /auth/login {user, password, fingerprint, cf-turnstile-response} → jexactyl_session
#   2. /server/{id} 点 “Renew”（挂机被停则是 “Renew Server”）→ 弹窗 “Renew your server”
#   3. 弹窗点 “Read Article” → window.open 新标签读文章，面板内倒计时 dwell_seconds
#      ⚠️ 文章标签没读完就关掉会被重置成 confirm 状态（“Keep the article tab open…”），Claim 永远不出现
#   4. 倒计时走完 → 弹窗内出现 Turnstile → 过验证 → “Claim Renewal”
#      → GET /api/client/renewal/complete?cf-turnstile-response=xxx（+10 天）→ 页面 reload
#   5. 帐号 adFree 时无文章/Turnstile，弹窗直接是 “Renew Now” → POST /api/client/servers/{uuid}/renew
#
# 过盾方案参考 katabump-main：hook attachShadow 抓 Turnstile 里的 checkbox，
# 换算成页面绝对坐标后用 CDP Input.dispatchMouseEvent 发原生鼠标事件。
#
# 广告遮罩：orihost 页面会注入全屏“假人机验证”弹窗（closed shadow DOM）挡住 Renew /
# Read Article / Claim 按钮，点了就等于点广告，倒计时状态机断开 → 永远等不到 Claim Renewal。
# 本脚本用【请求白名单】（只放行面板本体 + Cloudflare，其余第三方脚本全拦）从源头干掉广告，
# 并提前 hook attachShadow 清理残留遮罩；关键按钮只认真实鼠标点击（保证 window.open 能弹文章）。

import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests as tg_lib
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

PANEL = (os.environ.get("ORIHOST_PANEL") or "https://panel.orihost.com").rstrip("/")
SHOT_DIR = os.environ.get("SHOT_DIR") or "shots"

# 白名单：只放行面板本体和 Cloudflare Turnstile，其它第三方请求（广告/统计/弹窗脚本）一律拦掉。
# orihost 的广告网络会在页面上注入全屏“假人机验证”遮罩，把 Renew / Read Article / Claim 按钮全挡住，
# 用黑名单永远漏，直接白名单最稳。
ALLOW_HOSTS = {(urlparse(PANEL).hostname or "panel.orihost.com").lower(),
               "api.ip.sb", "api.ipify.org", "ipinfo.io", "ifconfig.me", "ipapi.co"}

# 各类超时（秒）
LOGIN_TIMEOUT = int(os.environ.get("LOGIN_TIMEOUT") or "120")
TS_ATTEMPTS = int(os.environ.get("TS_ATTEMPTS") or "6")  # Turnstile 单个验证最多点几次
CLAIM_TIMEOUT = int(os.environ.get("CLAIM_TIMEOUT") or "300")  # 等 Claim Renewal 上限
COOLDOWN_WAIT = int(os.environ.get("COOLDOWN_WAIT") or "600")  # 弹窗里等冷却走完的上限
REREAD_TIMES = int(os.environ.get("REREAD_TIMES") or "3")  # 倒计时被重置后重读文章次数
VERIFY_TIMEOUT = int(os.environ.get("VERIFY_TIMEOUT") or "90")  # Claim 后等结果
CLAIM_READY_TIMEOUT = int(os.environ.get("CLAIM_READY_TIMEOUT") or "120")  # 等 Claim 按钮变可点
TS_APPEAR_TIMEOUT = int(os.environ.get("TS_APPEAR_TIMEOUT") or "90")  # 等弹窗内 Turnstile 出现
DWELL_MIN = int(os.environ.get("DWELL_MIN") or "10")  # Read Article 后至少等多久（面板约 10s 才生效）

# 浏览器
# 有图形界面（DISPLAY / WAYLAND_DISPLAY）时默认开有头：Cloudflare Turnstile 在
# 有头真 Chrome 下几乎必过，在 headless（尤其轻量 Chromium）里大概率直接卡死。
_HAS_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
# 默认有头：Cloudflare Turnstile 在无头浏览器里基本过不去。没有图形界面时，
# main() 会自动用 xvfb-run 起一个虚拟显示（前提是装了 xvfb）。
HEADLESS = (os.environ.get("HEADLESS", "0").lower() not in ("0", "false", "no", "off"))
CHROME_PATH = (os.environ.get("CHROME_PATH") or "").strip()


# ---------- Telegram ----------
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""
if (not TG_BOT_TOKEN or not TG_CHAT_ID) and os.environ.get("TG_BOT"):
    try:
        _cid, _tok = os.environ["TG_BOT"].split(",", 1)
        TG_CHAT_ID = TG_CHAT_ID or _cid.strip()
        TG_BOT_TOKEN = TG_BOT_TOKEN or _tok.strip()
    except Exception:
        pass


def now_bj():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def mask_email(email: str) -> str:
    if not email:
        return ""
    if "@" in email:
        name, domain = email.split("@", 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        return f"{name}@{domain}"
    return email[:2] + "****"


def send_tg(msg: str, screenshot_path: str = ""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        if screenshot_path and os.path.exists(screenshot_path):
            with open(screenshot_path, "rb") as f:
                img_data = f.read()
            boundary = f"----Boundary{abs(hash(msg))}"
            body_parts = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                f"{TG_CHAT_ID}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="caption"\r\n\r\n'
                f"{msg}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="photo"; filename="s.png"\r\n'
                f"Content-Type: image/png\r\n\r\n"
            ).encode() + img_data + f"\r--{boundary}--\r\n".encode()
            tg_lib.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data=body_parts,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                timeout=30,
            )
        else:
            tg_lib.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=15,
            )
    except Exception as e:
        print(f"  TG 发送失败: {e}")


# ---------- 代理 ----------
# 优先级：ORIHOST_PROXY 显式指定 > 工作流 sing-box（IS_PROXY/PROXY_SERVER，由 NODE_LINK 转出）
def _get_proxy():
    """返回 Playwright 的 proxy 配置 dict（没有就 None）"""
    explicit = (os.environ.get("ORIHOST_PROXY") or os.environ.get("ORIHOST_GOST_PROXY") or "").strip()
    raw = ""
    if explicit:
        scheme = explicit.split("://", 1)[0].lower() if "://" in explicit else ""
        if scheme in ("http", "https", "socks4", "socks5", "socks5h"):
            raw = explicit
        else:
            print(f"  ⚠️ ORIHOST_PROXY 格式不支持 ({scheme}://)，节点链接请填 NODE_LINK")
    elif os.environ.get("IS_PROXY", "").lower() == "true":
        raw = (os.environ.get("PROXY_SERVER") or "socks5://127.0.0.1:1080").strip()
        print(f"  🔗 使用 sing-box 代理: {raw}")
    if not raw:
        return None
    try:
        u = urlparse(raw if "://" in raw else "http://" + raw)
        cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            cfg["username"] = u.username
        if u.password:
            cfg["password"] = u.password
        return cfg
    except Exception:
        print(f"  ⚠️ 代理地址解析失败: {raw}")
        return None


PROXY_CFG = _get_proxy()

# CI 直连提醒：GitHub runner 是机房 IP，Cloudflare Turnstile 拦截率高，
# 强烈建议配 NODE_LINK（sing-box）或 ORIHOST_PROXY，否则很可能卡在登录盾。
if PROXY_CFG is None and os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
    print("⚠️ CI 未检测到代理，将直连：GitHub runner 机房 IP 很可能过不了 Cloudflare Turnstile。")
    print("   建议在仓库 Secrets 配置 NODE_LINK（节点链接）或 ORIHOST_PROXY（http/socks 代理）。")

# ---------- 注入脚本：hook attachShadow 抓 Turnstile 的 checkbox ----------
TS_HOOK_JS = r"""
(function() {
    if (window.self === window.top) return;
    // 伪造屏幕坐标（Turnstile 会读）
    try {
        function rnd(a, b) { return Math.floor(Math.random() * (b - a + 1)) + a; }
        var sx = rnd(900, 1400), sy = rnd(500, 900);
        Object.defineProperty(MouseEvent.prototype, 'screenX', { get: function() { return sx; } });
        Object.defineProperty(MouseEvent.prototype, 'screenY', { get: function() { return sy; } });
    } catch (e) {}
    try {
        var orig = Element.prototype.attachShadow;
        Element.prototype.attachShadow = function(init) {
            var root = orig.call(this, init);
            if (root) {
                var check = function() {
                    var cb = root.querySelector('input[type="checkbox"]');
                    if (!cb) return false;
                    var r = cb.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && window.innerWidth > 0 && window.innerHeight > 0) {
                        window.__ts = {
                            xRatio: (r.left + r.width / 2) / window.innerWidth,
                            yRatio: (r.top + r.height / 2) / window.innerHeight
                        };
                        return true;
                    }
                    return false;
                };
                if (!check()) {
                    var ob = new MutationObserver(function() { if (check()) ob.disconnect(); });
                    ob.observe(root, { childList: true, subtree: true });
                }
            }
            return root;
        };
    } catch (e) {}
})();
"""

# 很多广告遮罩是注入到 closed shadow DOM 里的全屏“假人机验证”弹窗，querySelector 根本抓不到，
# 所以要在页面脚本运行前先 hook attachShadow，把 shadow root 收集起来，清理时一起扫。
OVERLAY_HOOK_JS = r"""
(function() {
    try {
        window.__shadowRoots = [];
        var orig = Element.prototype.attachShadow;
        Element.prototype.attachShadow = function(init) {
            var root = orig.call(this, init);
            try { window.__shadowRoots.push(root); } catch (e) {}
            return root;
        };
    } catch (e) {}
})();
"""

# 移除广告遮罩。因为广告脚本已被请求白名单拦掉，这里只做保守清理，
# 关键是【有弹窗时什么都不动】：面板的 Renew 弹窗是用 portal 挂到 body 下的（不在 #app 里），
# 一旦把大块 fixed 元素删掉，就会把弹窗一起删掉，导致 Read Article / Claim 点不到。
_REMOVE_OVERLAY_JS = r"""
(function() {
    var n = 0;
    try {
        if (document.querySelector('[role="dialog"], [aria-modal="true"]')) return 0;
    } catch (e) {}
    document.querySelectorAll('ins').forEach(function(el) { el.remove(); n++; });
    document.querySelectorAll('iframe').forEach(function(f) {
        var src = f.src || '';
        if (!src) return;
        if (src.indexOf(location.origin) === 0 || src.indexOf('challenges.cloudflare.com') !== -1) return;
        f.remove(); n++;
    });
    // 只清理“纯遮挡层”：全屏 fixed、无文字、无任何可交互子元素
    Array.prototype.forEach.call(document.body ? document.body.children : [], function(el) {
        if (el.id === 'app' || el.id === 'modal-portal') return;
        try {
            var s = getComputedStyle(el);
            if (s.position !== 'fixed') return;
            var r = el.getBoundingClientRect();
            if (r.width < innerWidth * 0.9 || r.height < innerHeight * 0.9) return;
            if ((el.innerText || '').trim().length) return;
            if (el.querySelector('button,input,a,select,textarea,[role="dialog"]')) return;
            el.remove(); n++;
        } catch (e) {}
    });
    return n;
})()
"""


def remove_overlays(page):
    try:
        return page.evaluate(_REMOVE_OVERLAY_JS)
    except Exception:
        return 0


# ---------- Turnstile ----------
def _ts_token(page) -> str:
    """取主页面里 react-turnstile 注入的隐藏域名的值"""
    try:
        return page.evaluate(
            "() => { const i = document.querySelector('input[name=cf-turnstile-response]');"
            " return (i && i.value) ? i.value : ''; }"
        ) or ""
    except Exception:
        return ""


def _ts_has_widget(page) -> bool:
    try:
        return bool(page.evaluate(
            "() => { if (document.querySelector('input[name=cf-turnstile-response]')) return true;"
            " return Array.from(document.querySelectorAll('iframe')).some(f => (f.src||'').includes('challenges.cloudflare.com')); }"
        ))
    except Exception:
        return False


def _ts_click_coords(page):
    """返回 (x, y)：优先用 attachShadow hook 抓到的 checkbox 比例坐标"""
    try:
        for fr in page.frames:
            try:
                data = fr.evaluate("window.__ts || null")
            except Exception:
                continue
            if not data:
                continue
            try:
                box = fr.frame_element().bounding_box()
            except Exception:
                continue
            if not box:
                continue
            return box["x"] + box["width"] * data["xRatio"], box["y"] + box["height"] * data["yRatio"]
    except Exception:
        pass
    # 兜底：直接点 Cloudflare iframe 左侧 checkbox 位置
    try:
        el = page.locator("iframe[src*='challenges.cloudflare.com']").first
        box = el.bounding_box()
        if box:
            return box["x"] + 30, box["y"] + box["height"] / 2
    except Exception:
        pass
    return None


def _native_click(cdp, x, y):
    """CDP 原生鼠标事件（比 page.mouse 更接近真人）"""
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x - 35, "y": y - 22, "buttons": 0})
    time.sleep(random.uniform(0.08, 0.20))
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "buttons": 0})
    time.sleep(random.uniform(0.05, 0.15))
    cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y,
                                          "button": "left", "buttons": 1, "clickCount": 1})
    time.sleep(random.uniform(0.05, 0.13))
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y,
                                          "button": "left", "buttons": 0, "clickCount": 1})


def _ts_success(page) -> bool:
    """Cloudflare 通过后 iframe 里会出现 “Success!”，作为 token 之外的辅助判断"""
    try:
        for fr in page.frames:
            if "cloudflare" not in (fr.url or ""):
                continue
            try:
                if fr.get_by_text("Success", exact=False).is_visible(timeout=250):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _ts_passed(page) -> bool:
    return len(_ts_token(page)) > 20 or _ts_success(page)


def solve_turnstile(page, cdp, label="", attempts=None, per_try=10) -> bool:
    """点过 Cloudflare Turnstile。成功返回 True。"""
    attempts = attempts or TS_ATTEMPTS
    if not _ts_has_widget(page):
        print("  ℹ️ 当前页面没有 Turnstile 组件")
        return True
    print(f"🔍 处理 Cloudflare Turnstile{('（' + label + '）') if label else ''}...")
    try:
        page.wait_for_selector("input[name='cf-turnstile-response']", timeout=15000)
    except Exception:
        pass
    for attempt in range(1, attempts + 1):
        if _ts_passed(page):
            print("  ✅ Turnstile 已通过")
            return True
        coords = _ts_click_coords(page)
        if coords:
            x, y = coords
            # 每次点不同像素，别死磕同一个点
            x += random.uniform(-3, 3)
            y += random.uniform(-3, 3)
            print(f"  🖱️ 第 {attempt}/{attempts} 次 CDP 点击 Turnstile ({x:.0f},{y:.0f})")
            try:
                _native_click(cdp, x, y)
            except Exception as e:
                print(f"    CDP 点击异常，改用 mouse API: {e}")
                try:
                    page.mouse.move(x, y)
                    page.mouse.down()
                    time.sleep(0.08)
                    page.mouse.up()
                except Exception:
                    pass
        else:
            print(f"  ⚠️ 第 {attempt}/{attempts} 次没找到 Turnstile checkbox（可能正在加载）")
        end = time.time() + per_try
        while time.time() < end:
            if _ts_passed(page):
                print(f"  ✅ Turnstile 通过（第 {attempt} 次点击）")
                return True
            time.sleep(0.5)
        # 组件可能被刷成新的 iframe，强制刷新状态
        remove_overlays(page)
    print("  ❌ Turnstile 未通过")
    if HEADLESS:
        print("  💡 headless 下 Cloudflare Turnstile 基本过不去，请改用有头模式（脚本会自动找 Chrome for Testing）：")
        print("     python -m playwright install chromium    # 只此一条，不要在非 Ubuntu/Debian 上跑 install chrome")
        print("     HEADLESS=0 xvfb-run -a python orihost_browser_renew.py   # 无桌面环境时")
    return False


# ---------- 页面小工具 ----------
def short_id(s: str) -> str:
    return (s or "").strip().split("-")[0][:8]


def _btn_enabled(loc) -> bool:
    """按钮是否真的可点：既看 disabled 属性，也看常见的禁用样式。
    面板里 Claim Renewal 会先渲染出来但 disabled，等倒计时走完才生效。"""
    if loc is None:
        return False
    try:
        if not loc.is_enabled():
            return False
    except Exception:
        return False
    try:
        blocked = loc.evaluate(
            "el => el.disabled === true"
            " || (el.getAttribute('aria-disabled') === 'true')"
            " || el.classList.contains('pointer-events-none')"
            " || el.classList.contains('cursor-not-allowed')"
            " || el.classList.contains('btn-disabled')"
        )
        return not blocked
    except Exception:
        return True


def find_btn(page, *names, timeout=20):
    """按可见按钮文本找元素（role → text-is → has-text 三级兜底）"""
    end = time.time() + timeout
    while time.time() < end:
        for n in names:
            for loc in (
                page.get_by_role("button", name=n, exact=True),
                page.locator(f"button:text-is('{n}')"),
                page.locator(f"button:has-text('{n}')"),
            ):
                try:
                    if loc.count() and loc.first.is_visible():
                        return loc.first
                except Exception:
                    continue
        time.sleep(0.5)
    return None


def safe_click(loc, page=None, tries=3) -> bool:
    """真实鼠标点击 → dispatchEvent → JS 点击。
    不要用 force=True：它按坐标硬点，遮罩存在时会把点击送给广告层，看起来“成功”其实没点到按钮。"""
    if loc is None:
        return False
    for _ in range(tries):
        try:
            if page is not None:
                remove_overlays(page)
            try:
                loc.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            loc.click(timeout=8000)
            return True
        except Exception:
            try:
                if page is not None:
                    remove_overlays(page)
                loc.dispatch_event("click")   # 直接把 click 事件派发给元素，绕过遮挡的命中测试
                return True
            except Exception:
                try:
                    loc.evaluate("el => el.click()")
                    return True
                except Exception:
                    time.sleep(1)
    return False


def shot(page, name: str) -> str:
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        path = os.path.join(SHOT_DIR, f"{name}.png")
        page.screenshot(path=path, full_page=True)
        print(f"  📸 截图: {path}")
        return path
    except Exception as e:
        print(f"  ⚠️ 截图失败: {e}")
        return ""


def api_get(page, path: str):
    """在页面内 fetch，复用浏览器登录态与 XSRF（全是 GET，不需要 CSRF 头）"""
    try:
        return page.evaluate(
            """async (path) => {
                const r = await fetch(path, {
                    credentials: 'same-origin',
                    headers: {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
                });
                if (!r.ok) return {__status: r.status, __ok: false};
                const t = await r.text();
                try { return {__status: r.status, __ok: true, data: JSON.parse(t)}; }
                catch (e) { return {__status: r.status, __ok: true, data: t}; }
            }""",
            path,
        )
    except Exception:
        return None


def is_logged_in(page) -> bool:
    # 注意：这个面板的服务器列表端点是 /api/client，不是 /api/client/servers（后者直接 404）
    for path in ("/api/client/account", "/api/client"):
        r = api_get(page, path)
        if r and r.get("__ok"):
            return True
    return False


def _page_logged_in(page) -> bool:
    """页面侧判断是否已登录（刚跳转时 API 会因为 context 被销毁而误判）"""
    try:
        if "/auth/login" in (page.url or ""):
            return False
    except Exception:
        pass
    try:
        return bool(page.evaluate(
            "() => !!Array.from(document.querySelectorAll('a,button'))"
            ".find(e => /logout/i.test(e.textContent || ''))"
        ))
    except Exception:
        return False


def server_info(page, server: str):
    """查单台服务器：renewal / expires_at / renewable，返回 dict"""
    for ident in (short_id(server), server):
        r = api_get(page, f"/api/client/servers/{ident}")
        if r and r.get("__ok"):
            d = r.get("data") or {}
            attrs = d.get("attributes") if isinstance(d, dict) else None
            attrs = attrs if isinstance(attrs, dict) else (d if isinstance(d, dict) else {})
            return {
                "uuid": attrs.get("uuid", ""),
                "identifier": attrs.get("identifier", short_id(server)),
                "name": attrs.get("name", ""),
                "renewal": attrs.get("renewal"),
                "renewable": attrs.get("renewable"),
                "expires_at": str(attrs.get("expires_at") or ""),
            }
    return None


def list_servers(page):
    """列出帐号下所有服务器（短 ID）。端点：/api/client（不是 /api/client/servers）"""
    out = []
    r = api_get(page, "/api/client")
    if not r or not r.get("__ok") or not isinstance(r.get("data"), dict):
        return out
    items = r["data"].get("data") or r["data"].get("items") or []
    for it in items:
        attrs = it.get("attributes") if isinstance(it, dict) else None
        attrs = attrs if isinstance(attrs, dict) else (it if isinstance(it, dict) else {})
        ident = (attrs.get("identifier") or attrs.get("id") or "").strip()
        if ident:
            out.append({"id": short_id(ident), "name": attrs.get("name", "")})
    return out


def remaining_days(expires_at: str):
    if not expires_at:
        return None
    try:
        dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return round((dt - datetime.now(timezone.utc)).total_seconds() / 86400, 1)
    except Exception:
        return None


# ---------- 帐号解析 ----------
def _split_ids(raw: str):
    return [s.strip() for s in (raw or "").replace(";", ",").split(",") if s.strip()]


# 本地帐号文件（格式与 katabump 的 login.json 一致：[{"username","password","servers"?}]）
LOCAL_LOGIN_FILE = (os.environ.get("ORIHOST_LOGIN_FILE")
                    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "orihost_login.json"))


def load_accounts_file():
    if not os.path.exists(LOCAL_LOGIN_FILE):
        return []
    try:
        with open(LOCAL_LOGIN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"⚠️ 读取本地帐号文件失败（{LOCAL_LOGIN_FILE}）: {e}")
        return []
    out = []
    for i, u in enumerate(data if isinstance(data, list) else [], 1):
        email = (u.get("email") or u.get("username") or "").strip()
        pwd = (u.get("password") or "").strip()
        if not email or not pwd:
            print(f"⚠️ 本地帐号文件第 {i} 项缺少帐号或密码，跳过")
            continue
        out.append({"label": email, "email": email, "password": pwd,
                    "servers": _split_ids(u.get("servers", ""))})
    if out:
        print(f"ℹ️ 已从 {LOCAL_LOGIN_FILE} 读取 {len(out)} 个帐号")
    return out


def load_accounts():
    """优先 ORIHOST_ACCOUNTS（JSON）> ORIHOST_EMAIL/ORIHOST_PASSWORD（可带 _1.._N 后缀）> 本地 orihost_login.json"""
    accounts = []
    raw = (os.environ.get("ORIHOST_ACCOUNTS") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            for i, u in enumerate(data if isinstance(data, list) else [], 1):
                email = (u.get("email") or u.get("username") or "").strip()
                pwd = (u.get("password") or "").strip()
                if not email or not pwd:
                    print(f"⚠️ ORIHOST_ACCOUNTS 第 {i} 项缺少帐号或密码，跳过")
                    continue
                accounts.append({
                    "label": email,
                    "email": email,
                    "password": pwd,
                    "servers": _split_ids(u.get("servers", "")),
                })
        except Exception as e:
            print(f"⚠️ ORIHOST_ACCOUNTS 解析失败（{e}），改用 ORIHOST_EMAIL/PASSWORD")

    if not accounts:
        for i in range(1, 20):
            suf = "" if i == 1 else f"_{i}"
            email = (os.environ.get(f"ORIHOST_EMAIL{suf}") or os.environ.get(f"ORIHOST_USERNAME{suf}") or "").strip()
            pwd = os.environ.get(f"ORIHOST_PASSWORD{suf}") or ""
            ids = _split_ids(os.environ.get(f"ORIHOST_SERVER_IDS{suf}") or "")
            if not email and not pwd and not ids:
                if i == 1:
                    continue
                break
            if not email or not pwd:
                print(f"⚠️ 账号{i}（{mask_email(email) or '未填邮箱'}）帐号或密码不完整，跳过")
                continue
            accounts.append({"label": email, "email": email, "password": pwd, "servers": ids})

    if not accounts:
        accounts = load_accounts_file()
    return accounts


# ---------- 登录 ----------
def do_login(page, cdp, acc) -> bool:
    """帐号密码 + Turnstile 登录，成功返回 True"""
    email, pwd = acc["email"], acc["password"]
    print(f"🔑 登录 {mask_email(email)} ...")
    for attempt in range(1, 4):
        page.goto(f"{PANEL}/auth/login", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        if is_logged_in(page) or _page_logged_in(page):
            print("  ✅ 已有有效登录态")
            return True

        # 若页面上还有别的登录框残留，先清干净
        try:
            page.fill("input[name='username']", email)
            page.fill("input[name='password']", pwd)
        except Exception:
            try:
                page.fill("input[placeholder='Username or Email']", email)
                page.fill("input[type='password']", pwd)
            except Exception as e:
                if _page_logged_in(page):
                    print("  ✅ 已登录（当前页面没有登录框）")
                    return True
                print(f"  ❌ 找不到登录输入框: {e}")
                shot(page, f"login_form_missing_{short_id(email)}")
                return False
        page.wait_for_timeout(600)
        remove_overlays(page)

        # Turnstile（面板 login=true，必须先过）
        if not solve_turnstile(page, cdp, "登录"):
            shot(page, f"login_turnstile_fail_{attempt}")
            print(f"  ⚠️ 第 {attempt} 次登录 Turnstile 未通过，重试")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            continue

        submit = find_btn(page, "Sign In", timeout=15)
        if submit is None:
            submit = page.locator("button[type='submit']").first
        if not safe_click(submit, page):
            print("  ❌ 点不到 Sign In 按钮")
            shot(page, f"login_submit_fail_{attempt}")
            return False

        # 等待跳转 / 报错（导航中 context 会被销毁，API 判定要多试几次）
        end = time.time() + 45
        logged = False
        while time.time() < end:
            page.wait_for_timeout(1500)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            if is_logged_in(page) or _page_logged_in(page):
                logged = True
                break
        if logged:
            print("  ✅ 登录成功")
            return True
        # 失败原因
        try:
            body = (page.inner_text("body") or "")[:400].replace("\n", " ")
        except Exception:
            body = ""
        if "checkpoint" in (page.url or "") or "Two-Factor" in body or "two-factor" in body:
            print("  ❌ 该帐号开了两步验证，脚本暂不支持（请在面板里关掉或改用带 2FA 的方式）")
            shot(page, "login_2fa")
            return False
        print(f"  ❌ 登录失败（第 {attempt} 次）: {body[:180]}")
        shot(page, f"login_fail_{attempt}")
        time.sleep(3)
    print("  ❌ 3 次登录均失败")
    return False


# ---------- 读文章 ----------
def open_article(ctx, page, read_btn, cdp=None, sid=None):
    """点 Read Article，等新标签（文章页）。
    ⚠️ 必须用「受信任」的真实鼠标点击：面板里是 window.open 开文章，
       若用 JS el.click() 触发，弹窗可能被拦、面板倒计时状态机也会断，Claim Renewal 永远不出来。

    弹窗捕获用 page 事件而不是轮询 ctx.pages：事件在标签创建瞬间就触发，不会漏掉那些
    开了又被重定向（甚至变成 chrome-error://）的极快弹窗。文章标签「存在」即可，内容不重要。

    返回 (article, dwell_seconds)；没拿到 dwell 时 dwell 为 None。"""
    popups = []
    begin_url = {}

    def _on_page(p):
        popups.append(p)

    def _on_response(resp):
        try:
            if "/renew/begin" in resp.url:
                data = resp.json() or {}
                d = data.get("data") or {}
                u = d.get("url") or data.get("url")
                if u:
                    begin_url["url"] = u
                dwell = d.get("dwell_seconds") or data.get("dwell_seconds")
                if dwell:
                    try:
                        begin_url["dwell"] = float(dwell)
                    except Exception:
                        pass
        except Exception:
            pass

    before = set(ctx.pages)
    ctx.on("page", _on_page)
    page.on("response", _on_response)

    def _pick():
        """优先用事件捕获到的弹窗；没抓到就找 ctx.pages 里新出现的标签"""
        if popups:
            return popups[0]
        for p in ctx.pages:
            if p not in before and p is not page:
                return p
        return None

    def _wait_extra(seconds):
        end = time.time() + seconds
        while time.time() < end:
            got = _pick()
            if got is not None:
                return got
            page.wait_for_timeout(300)
        return None

    article = None
    try:
        for attempt in range(4):
            remove_overlays(page)
            try:
                try:
                    read_btn.scroll_into_view_if_needed(timeout=1500)   # 弹窗里的元素滚动常超时，尽力即可
                except Exception:
                    pass
                read_btn.click(timeout=8000)   # click 本身会自动滚动并做命中测试
            except Exception:
                # 遮挡导致命中测试失败时：先派发 click 事件，再退到 CDP 原生鼠标点
                try:
                    read_btn.dispatch_event("click")
                except Exception:
                    if cdp is not None:
                        try:
                            box = read_btn.bounding_box()
                            if box:
                                _native_click(cdp, box["x"] + box["width"] / 2,
                                              box["y"] + box["height"] / 2)
                        except Exception:
                            pass
            article = _wait_extra(8)
            if article is not None:
                break
            # 兜底：begin 已返回文章地址，说明点击确实生效、只是弹窗被拦 → 自己开一个标签
            if begin_url.get("url"):
                try:
                    article = ctx.new_page()
                    try:
                        article.goto(begin_url["url"], wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    print("  ℹ️ 弹窗被拦，已按 renew/begin 返回的地址自行打开文章页")
                    break
                except Exception:
                    article = None
    finally:
        try:
            ctx.remove_listener("page", _on_page)
        except Exception:
            pass
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    if article is not None:
        try:
            article.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
    # dwell_seconds：面板要求文章标签停留的秒数（拿到后 Read Article→Claim 之间要等这么久）
    return article, begin_url.get("dwell")


def close_page(p):
    try:
        if p is not None and not p.is_closed():
            p.close()
    except Exception:
        pass


# ---------- 单台续期 ----------
def renew_one(ctx, page, cdp, server: str, precheck=None) -> dict:
    sid = short_id(server)
    print(f"\n  🖥 [{sid}] 打开服务器页...")
    before = precheck or {}

    if before:
        print(f"  📅 预检: 续期次数 {before.get('renewal')} / 剩余 {before.get('days')} 天"
              + (f" / 冷却 {before.get('cooldown')}s" if before.get("cooldown") else ""))
        if before.get("renewable") is False:
            return {"status": "⏭️ 跳过", "message": "面板标记为不可续期（renewable=false）",
                    "expires_at": before.get("expires_at", "")}
        if (before.get("cooldown") or 0) > COOLDOWN_WAIT:
            return {"status": "⏭️ 跳过", "message": f"冷却中 {before.get('cooldown')}s，超过 {COOLDOWN_WAIT}s 不等",
                    "expires_at": before.get("expires_at", "")}

    page.goto(f"{PANEL}/server/{sid}", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    remove_overlays(page)

    # 1. 打开续期弹窗
    print("  🔍 找 Renew 按钮...")
    if find_btn(page, "Renew Limit Reached", timeout=25):
        return {"status": "⏭️ 跳过", "message": "已达续期上限（Renew Limit Reached）"}
    renew_btn = find_btn(page, "Renew Server", "Renew", timeout=30)
    if renew_btn is None:
        shot(page, f"no_renew_btn_{sid}")
        return {"status": "❌ 续期失败", "message": "没找到 Renew / Renew Server 按钮（页面结构可能变了）"}
    if not safe_click(renew_btn, page):
        return {"status": "❌ 续期失败", "message": "Renew 按钮点不动（多半被广告挡住）"}
    page.wait_for_timeout(2500)

    # 2. 弹窗里的冷却
    cool = wait_cooldown(page)
    if cool == "skip":
        return {"status": "⏭️ 跳过", "message": "弹窗提示冷却中且等不到，放弃本轮"}
    if cool == "limit":
        return {"status": "⏭️ 跳过", "message": "已达续期上限（Renew Limit Reached）"}

    # 3. adFree 帐号：直接 Renew Now
    renew_now = find_btn(page, "Renew Now", timeout=8)
    if renew_now is not None:
        print("  🖱️ adFree 帐号，点 Renew Now...")
        if not safe_click(renew_now, page):
            return {"status": "❌ 续期失败", "message": "Renew Now 按钮点不动"}
        return wait_renew_result(ctx, page, sid, before, adfree=True)

    # 4. 点 Read Article 开新标签读文章
    print("  🖱️ 点 Read Article...")
    read_btn = find_btn(page, "Read Article", timeout=25)
    article = None
    dwell = 0.0
    if read_btn is not None:
        article, dwell = open_article(ctx, page, read_btn, cdp=cdp, sid=sid)
        if article is None:
            shot(page, f"article_not_open_{sid}")
            return {"status": "❌ 续期失败", "message": "文章页没弹出来（面板要允许弹窗）"}
        print(f"  📰 文章页已打开：{(article.url or '')[:80]}")
        # 面板要求文章标签停留 dwell_seconds 后才允许 Claim，至少等 DWELL_MIN 秒
        dwell = max(dwell or 0.0, float(DWELL_MIN))
    else:
        print("  ℹ️ 没找到 Read Article，可能已在倒计时/就绪态，直接找 Claim Renewal")

    # 5. 等倒计时走完出 Claim Renewal
    #    ⚠️ 文章标签必须一直开着：面板检测到 y.current.closed 会把状态重置回 confirm，
    #       倒计时归零重来，Claim Renewal 永远不会出现（原脚本就栽在这）。
    print(f"  ⏳ 等 Claim Renewal 出现（上限 {CLAIM_TIMEOUT}s，期间不关文章页）...")
    claim_btn = None
    article_gone_logged = False
    deadline = time.time() + CLAIM_TIMEOUT
    reread = 0
    while time.time() < deadline:
        remove_overlays(page)
        claim_btn = find_btn(page, "Claim Renewal", timeout=2)
        if claim_btn is not None:
            break
        # 状态被重置了 → 重读文章
        again = find_btn(page, "Read Article", timeout=1)
        if again is not None and reread < REREAD_TIMES:
            reread += 1
            print(f"  ⚠️ 倒计时被重置（第 {reread} 次重读文章）")
            close_page(article)
            article, dwell = open_article(ctx, page, again, cdp=cdp, sid=sid)
            dwell = max(dwell or 0.0, float(DWELL_MIN))
            if article is None:
                shot(page, f"article_reopen_fail_{sid}")
                return {"status": "❌ 续期失败", "message": "重新打开文章页失败"}
            article_gone_logged = False
            continue
        if article is not None and article.is_closed() and not article_gone_logged:
            article_gone_logged = True
            print("  ⚠️ 文章页自己关了（面板会重置倒计时）")
        time.sleep(1)

    if claim_btn is None:
        close_page(article)
        shot(page, f"no_claim_btn_{sid}")
        return {"status": "❌ 续期失败", "message": f"{CLAIM_TIMEOUT}s 没等到 Claim Renewal（倒计时没走完或被重置）"}

    # 6. Read Article 后面板约 dwell 秒（~10s）才让 Claim 生效；期间弹窗会先加载 Turnstile。
    #    必须：① 弹窗内 Turnstile 过掉 ② Claim 真正变为可点，才去点它。
    #    早点在 disabled 按钮上等于没点（旧版就栽在这，返回“未知结果”）。
    print(f"  ⏳ 等面板倒计时结束（约 {dwell:.0f}s）...")
    dwell_end = time.time() + dwell
    while time.time() < dwell_end:
        remove_overlays(page)
        if _ts_has_widget(page):
            break  # 验证组件已出现，可提前进入验证
        page.wait_for_timeout(500)

    # 6a. 弹窗内 Turnstile：先等它出现（有的帐号/场景可能没有）
    print("  ⏳ 检查弹窗内 Turnstile...")
    ts_deadline = time.time() + TS_APPEAR_TIMEOUT
    while time.time() < ts_deadline:
        remove_overlays(page)
        if _ts_has_widget(page):
            break
        # 按钮已可点且始终没验证组件 → 视为无需验证，不再空等
        if _btn_enabled(find_btn(page, "Claim Renewal", timeout=1)):
            break
        page.wait_for_timeout(500)
    if _ts_has_widget(page):
        if not solve_turnstile(page, cdp, "Claim"):
            close_page(article)
            shot(page, f"turnstile_fail_{sid}")
            return {"status": "❌ 续期失败", "message": "Turnstile 未通过，Claim 按钮不会生效"}
    else:
        print("  ℹ️ 未检测到验证组件（可能本帐号无需验证，或组件仍在加载）")

    # 6b. 等 Claim Renewal 真正可点（disabled 状态时点了没用）
    print(f"  ⏳ 等 Claim Renewal 可点（上限 {CLAIM_READY_TIMEOUT}s）...")
    ready_deadline = time.time() + CLAIM_READY_TIMEOUT
    claim_btn = None
    while time.time() < ready_deadline:
        remove_overlays(page)
        cand = find_btn(page, "Claim Renewal", timeout=2)
        if cand is not None and _btn_enabled(cand):
            claim_btn = cand
            break
        # 验证组件还在且没过 → 补点几次
        if _ts_has_widget(page) and not _ts_passed(page):
            solve_turnstile(page, cdp, "Claim", attempts=2, per_try=5)
        if article is not None and article.is_closed() and not article_gone_logged:
            article_gone_logged = True
            print("  ⚠️ 文章页自己关了（面板可能重置倒计时）")
        page.wait_for_timeout(500)

    # 7. 点 Claim Renewal（文章页保持打开，续期完成后再关）
    print("  🖱️ 点 Claim Renewal...")
    if claim_btn is None:
        close_page(article)
        shot(page, f"claim_disabled_{sid}")
        return {"status": "❌ 续期失败",
                "message": f"Claim Renewal {CLAIM_READY_TIMEOUT}s 内没变成可点（倒计时/验证没完成）"}
    if not safe_click(claim_btn, page):
        close_page(article)
        return {"status": "❌ 续期失败", "message": "Claim Renewal 按钮点不动"}
    result = wait_renew_result(ctx, page, sid, before)
    close_page(article)
    return result


def wait_cooldown(page) -> str:
    """弹窗里的冷却倒计时：等它走完。返回 '' / 'skip' / 'limit'"""
    end = time.time() + COOLDOWN_WAIT
    warned = False
    while time.time() < end:
        if find_btn(page, "Renew Limit Reached", timeout=1):
            return "limit"
        if not find_btn(page, "Read Article", "Renew Now", "Claim Renewal", "Wait", timeout=1):
            # 弹窗里没有任何续期按钮 → 可能弹窗没开出来
            if not warned:
                print("  ⚠️ 续期弹窗里没找到按钮")
                warned = True
        wait = find_btn(page, "Wait", timeout=1)
        if wait is not None:
            try:
                label = (wait.inner_text(timeout=2000) or "").strip()
            except Exception:
                label = ""
            if not warned:
                print(f"  ⏳ 冷却/倒计时中: {label}")
                warned = True
        if find_btn(page, "Read Article", "Renew Now", "Claim Renewal", timeout=1):
            return ""
        time.sleep(1)
    return "skip"


def wait_renew_result(ctx, page, sid, before, adfree=False) -> dict:
    """点完按钮后等结果：先看提示，再用 API 确认到期时间/续期次数变化"""
    flash = ""
    limit_seen = False
    end = time.time() + 20
    while time.time() < end:
        try:
            body = (page.inner_text("body") or "")
        except Exception:
            body = ""
        low = body.lower()
        if "renewed successfully" in low or "renewal successful" in low:
            flash = "面板提示续期成功"
            break
        if "captcha" in low and "complete" in low:
            flash = "面板提示需要先完成验证"
            break
        if "renew limit reached" in low:
            # 续期成功后按钮常会变成这个文案（表示本轮名额已用掉），
            # 不能据此直接判跳过——下面用 API 核对次数是否真的增加。
            limit_seen = True
            break
        page.wait_for_timeout(1000)

    # 面板成功后会 reload，等它加载完再用 API 核对
    deadline = time.time() + VERIFY_TIMEOUT
    after = None
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        after = server_info(page, sid)
        if not after:
            continue
        old_exp = before.get("expires_at") or ""
        old_renew = before.get("renewal")
        try:
            new_renew = int(after.get("renewal"))
            old_i = int(old_renew) if old_renew is not None else None
        except Exception:
            new_renew, old_i = None, None
        exp_up = bool(old_exp and after.get("expires_at") and after["expires_at"] > old_exp)
        renew_up = bool(new_renew is not None and old_i is not None and new_renew > old_i)
        if exp_up or renew_up:
            path = shot(page, f"renew_success_{sid}")
            days = remaining_days(after.get("expires_at"))
            return {
                "status": "✅ 续期成功",
                "message": flash or ("到期时间/续期次数已增加" if exp_up else "续期次数已增加"),
                "screenshot": path,
                "expires_at": after.get("expires_at", ""),
                "after_exp": (after.get("expires_at") or "")[:10],
                "days": days,
            }
        if adfree and flash:
            break

    if flash and "captcha" in flash.lower():
        shot(page, f"claim_captcha_{sid}")
        return {"status": "❌ 续期失败", "message": flash}
    if limit_seen:
        # API 没看到次数变化，且面板明确提示已达上限 → 这才是真正的跳过
        shot(page, f"claim_limit_{sid}")
        return {"status": "⏭️ 跳过", "message": "已达续期上限（Renew Limit Reached）"}
    shot(page, f"claim_unknown_{sid}")
    return {"status": "⚠️ 未知结果", "message": (flash + " " if flash else "")
            + "已点按钮但没读到明确成功提示，请人工看一眼面板"}


# ---------- cron 自我调度（参考 oyz/FreezeHost） ----------
def updateCronSchedule(after_expires_at: str):
    """续期成功后按 expires_at-1天 改写 workflow 的 cron 为一次性定时并 push"""
    if os.environ.get("DRY_RUN", "").lower() == "true":
        print("  ℹ️ DRY_RUN 演练，跳过 cron 回写")
        return False
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        print("  ℹ️ 非 CI 环境，跳过 cron 回写")
        return False
    gh_token = os.environ.get("GH_TOKEN", "")
    if not gh_token:
        print("  ℹ️ 未提供 GH_TOKEN，跳过 cron 回写")
        return False

    try:
        t = datetime.fromisoformat(after_expires_at.replace("Z", "+00:00"))
        next_run = t - timedelta(days=1)
        if next_run.timestamp() <= datetime.now(timezone.utc).timestamp():
            next_run = datetime.now(timezone.utc) + timedelta(hours=12)

        p2 = lambda n: str(n).zfill(2)
        new_cron = f"10 10 {next_run.day} {next_run.month} *"
        next_str = (f"{next_run.year}-{p2(next_run.month)}-{p2(next_run.day)} "
                    f"{p2(next_run.hour)}:{p2(next_run.minute)}")

        wf = os.path.join(os.getcwd(), ".github", "workflows", "renew-browser.yml")
        if not os.path.exists(wf):
            wf = os.path.join(os.getcwd(), ".github", "workflows", "renew.yml")
        if not os.path.exists(wf):
            print("  ⚠️ 未找到 workflow 文件，跳过 cron 回写")
            return False

        with open(wf, "r", encoding="utf-8") as f:
            old = f.read()
        m = re.search(r"^(\s*- cron: )'[^']*'(.*)$", old, re.M)
        if not m:
            print("  ⚠️ workflow 无 cron 行，跳过 cron 回写")
            return False

        updated = old.replace(m.group(0), f"{m.group(1)}'{new_cron}'  # auto: 下一次 {next_str} UTC")
        with open(wf, "w", encoding="utf-8") as f:
            f.write(updated)

        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "github-actions[bot]")
        env.setdefault("GIT_AUTHOR_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com")
        env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
        env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]

        # 目标分支/仓库：schedule 事件下 checkout 可能处于 detached HEAD，
        # 直接 `git push` 会报 "not currently on a branch"，所以显式 push HEAD 到分支
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        branch = os.environ.get("GITHUB_REF_NAME", "") or os.environ.get("GITHUB_HEAD_REF", "")
        if not branch:
            r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                               env=env, capture_output=True, text=True)
            branch = r.stdout.strip()
        if branch in ("", "HEAD"):
            branch = "main"

        # 先同步远端，减少 push 时因落后被拒的概率（失败不阻断，稍后仍尝试 push）
        if repo:
            subprocess.run(["git", "fetch", "origin", branch],
                           env=env, capture_output=True, timeout=30)

        subprocess.run(["git", "add", wf], env=env, capture_output=True, timeout=10)
        subprocess.run(["git", "commit", "-m", "自动调整下次续期时间", "-m", f"下次运行: {next_str} UTC"],
                       env=env, capture_output=True, timeout=10)

        if repo:
            push_url = f"https://x-access-token:{gh_token}@github.com/{repo}.git"
            push = subprocess.run(["git", "push", push_url, f"HEAD:refs/heads/{branch}"],
                                  env=env, capture_output=True, text=True, timeout=30)
        else:
            push = subprocess.run(["git", "push", "HEAD"],
                                  env=env, capture_output=True, text=True, timeout=30)
        if push.returncode != 0:
            print(f"  ⚠️ cron 已写入本地但 push 失败: {push.stderr.strip()[:300]}")
            return False
        print(f"  ✅ cron 已回写: {new_cron}（下一次 {next_str} UTC）")
        return True
    except Exception as e:
        print(f"  ⚠️ cron 回写失败: {e}")
        return False


# ---------- TG 报告 ----------
def fmt_msg(status, email, server, detail, before_exp="", after_exp=""):
    lines = ["🎰 Orihost 续期报告", "", status]
    if email:
        lines.append(f"📧 账号: {mask_email(email)}")
    lines.append(f"🆔 服务器: {short_id(server)}")
    if before_exp:
        lines.append(f"⏱ 续期前到期时间: {before_exp}")
    if after_exp:
        lines.append(f"⏱ 续期后到期时间: {after_exp}")
    if detail:
        lines.append(f"📌 {detail}")
    lines.append(f"⏰ {now_bj()}")
    return "\n".join(lines)


# ---------- 启动浏览器（原生 Chrome + CDP，参考 katabump） ----------
_CHROME_PROC = None


def _port_open(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_chrome_binary() -> str:
    """找真 Chrome：CHROME_PATH > 系统 google-chrome/chromium > Playwright 自带的 Chrome for Testing"""
    if CHROME_PATH and os.path.exists(CHROME_PATH):
        return CHROME_PATH
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "chrome", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    cache = os.path.expanduser("~/.cache/ms-playwright")
    if os.path.isdir(cache):
        for d in sorted(os.listdir(cache), reverse=True):
            for rel in ("chrome-linux64/chrome", "chrome-linux/chrome",
                        "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                        "chrome-win/chrome.exe"):
                p = os.path.join(cache, d, rel)
                if os.path.exists(p):
                    return p
    return ""


def launch_browser(pw):
    """直接用原生 Chrome 起进程，再用 CDP 连上。
    Playwright 的 launch() 会带上自动化特征，Cloudflare Turnstile 能识别出来，怎么点都不通过；
    原生启动 + connect_over_cdp 实测秒过（同 katabump 的思路）。"""
    global _CHROME_PROC
    chrome = _find_chrome_binary()
    if not chrome:
        raise RuntimeError("找不到 Chrome：请安装 google-chrome，或设置 CHROME_PATH")
    base = int(os.environ.get("ORIHOST_DEBUG_PORT") or "9222")
    # 上次没退干净的实例（调试端口还开着）直接复用，
    # 否则用同一个 user-data-dir 再起一个 Chrome 会因 profile 锁直接退出
    for p in range(base, base + 20):
        if _port_open(p):
            print(f"ℹ️ 复用 {p} 端口上已在运行的 Chrome")
            return pw.chromium.connect_over_cdp(f"http://127.0.0.1:{p}")
    port = base
    user_dir = (os.environ.get("ORIHOST_USER_DATA_DIR")
                or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ChromeData_Orihost"))
    os.makedirs(user_dir, exist_ok=True)
    args = [
        chrome, f"--remote-debugging-port={port}", f"--user-data-dir={user_dir}",
        "--no-first-run", "--no-default-browser-check", "--disable-popup-blocking",
        "--disable-notifications", "--disable-blink-features=AutomationControlled",
        "--window-size=1440,900",
        # 文章页在前台时主页面会被判定“被遮挡”，Chrome 会把倒计时节流，导致永远等不到 Claim Renewal
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]
    if PROXY_CFG:
        args.append(f"--proxy-server={PROXY_CFG['server']}")
        args.append("--proxy-bypass-list=<-loopback>")
    # CI / 容器里非特权 user namespace 常被禁用，Chrome sandbox 会直接把进程拉不起来；
    # GitHub Actions 默认开，也可用 CHROME_NO_SANDBOX=1 手动打开。
    if os.environ.get("CHROME_NO_SANDBOX", "").lower() in ("1", "true", "yes", "on") \
            or os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        args += ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    if HEADLESS:
        args.append("--headless=new")
    args.append("about:blank")
    print(f"🌐 原生启动 Chrome: {chrome}（{'headless' if HEADLESS else '有头'}）"
          f"{' 代理:' + PROXY_CFG['server'] if PROXY_CFG else ' 直连'}")
    _CHROME_PROC = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
    for _ in range(60):
        if _port_open(port):
            break
        time.sleep(0.5)
    else:
        try:
            _CHROME_PROC.terminate()
        except Exception:
            pass
        raise RuntimeError(f"Chrome 调试端口 {port} 未就绪")
    b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    print(f"🌐 浏览器: {b.version} ({'headless' if HEADLESS else '有头'}) "
          f"内核={os.path.basename(chrome)}")
    return b


def close_browser(browser):
    global _CHROME_PROC
    try:
        browser.close()
    except Exception:
        pass
    if _CHROME_PROC is not None:
        try:
            _CHROME_PROC.terminate()
        except Exception:
            pass
        _CHROME_PROC = None


_CTX_PREPARED = set()


def new_context(browser):
    """CDP 连接不能新建上下文，直接用浏览器默认上下文；防检测脚本 + 广告白名单只装一次。"""
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    if id(ctx) not in _CTX_PREPARED:
        _CTX_PREPARED.add(id(ctx))

        # 白名单放行：面板本体 + Cloudflare（Turnstile 必需），其余第三方脚本一律拦掉，
        # 从源头掐死广告弹窗脚本，按钮就不会再被“假人机验证”遮罩挡住。
        def _route(route):
            try:
                url = route.request.url
                if url.startswith("data:") or url.startswith("blob:") or url.startswith("about:"):
                    return route.continue_()
                host = (urlparse(url).hostname or "").lower()
                if host in ALLOW_HOSTS or host == "cloudflare.com" or host.endswith(".cloudflare.com"):
                    return route.continue_()
            except Exception:
                pass
            return route.abort()

        ctx.route("**/*", _route)
        ctx.add_init_script(OVERLAY_HOOK_JS)
        ctx.add_init_script(TS_HOOK_JS)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        try:
            ctx.set_default_timeout(30000)
        except Exception:
            pass
    return ctx


# ---------- 启动前：无显示环境自动套 xvfb ----------
def _maybe_reexec_xvfb():
    """没有 DISPLAY 又要跑有头浏览器时，自动用 xvfb-run 起一个虚拟显示"""
    global HEADLESS
    if HEADLESS or _HAS_DISPLAY or os.environ.get("_ORIHOST_XVFB") == "1":
        return
    import shutil
    if not shutil.which("xvfb-run"):
        print("⚠️ 无图形界面且没装 xvfb-run，只能退回 headless（Cloudflare 大概率拦截）。")
        print("   建议：sudo apt-get install -y xvfb（Chrome 用 python -m playwright install chromium，或设 CHROME_PATH）")
        HEADLESS = True
        return
    print("ℹ️ 无 DISPLAY，自动用 xvfb-run 起虚拟显示跑有头浏览器…")
    env = os.environ.copy()
    env["_ORIHOST_XVFB"] = "1"
    os.execvpe("xvfb-run", ["xvfb-run", "-a", "-s", "-screen 0 1440x900x24",
                              sys.executable, os.path.abspath(__file__)] + sys.argv[1:], env)


# ---------- 主入口 ----------
def main():
    _maybe_reexec_xvfb()
    print("#" * 46)
    print("  Orihost 免费服务器自动续期（帐号密码登录）")
    print("#" * 46)
    accounts = load_accounts()
    if not accounts:
        print("❌ 未配置帐号。请设置 ORIHOST_EMAIL + ORIHOST_PASSWORD"
              "（或 ORIHOST_ACCOUNTS='[{\"email\":\"...\",\"password\":\"...\"}]'），"
              "或在脚本同目录放一个 orihost_login.json")
        sys.exit(1)
    print(f"👤 帐号 {len(accounts)} 个")

    results = []
    with sync_playwright() as pw:
        print("🚀 启动浏览器...")
        browser = launch_browser(pw)
        ctx = new_context(browser)
        # 清掉可能被上次会话恢复出来的多余标签页（至少留一个，全关会把浏览器一起关掉）
        for _p in list(ctx.pages)[1:]:
            try:
                _p.close()
            except Exception:
                pass
        try:
            for acc in accounts:
                email = acc["email"]
                print(f"\n{'=' * 46}\n {mask_email(email)}（{email}）\n{'=' * 46}")
                # 同一浏览器上下文复用，切账号前清掉上一个账号的登录态
                try:
                    ctx.clear_cookies()
                except Exception:
                    pass
                page = ctx.new_page()
                try:
                    cdp = ctx.new_cdp_session(page)
                except Exception as e:
                    print(f"  ❌ CDP 会话创建失败: {e}")
                    try:
                        page.close()
                    except Exception:
                        pass
                    continue
                try:
                    # 出口 IP 查询用独立标签页，失败/卡住时不要污染主页面的导航状态
                    try:
                        probe = ctx.new_page()
                        probe.goto("https://api.ip.sb/ip", wait_until="domcontentloaded", timeout=20000)
                        print(f"📍 当前出口IP: {(probe.inner_text('body') or '').strip()}")
                        probe.close()
                    except Exception as e:
                        print(f"  ⚠️ 出口IP查询失败（忽略）: {str(e).splitlines()[0][:80]}")

                    if not do_login(page, cdp, acc):
                        for sv in (acc["servers"] or ["?"]):
                            info = {"label": email, "server": sv, "status": "❌ 登录失败",
                                    "message": "帐号密码登录失败"}
                            results.append(info)
                            send_tg(fmt_msg(info["status"], email, sv, info["message"]))
                        continue

                    # 服务器列表：没配就自动列全部
                    servers = [s["id"] for s in list_servers(page)] if not acc["servers"] else acc["servers"]
                    if not servers:
                        servers = acc["servers"]
                    if not servers:
                        print("  ⚠️ 帐号下没有服务器（也没配 ORIHOST_SERVER_IDS）")
                        continue
                    print(f"🖥 待续期 {len(servers)} 台: {', '.join(short_id(s) for s in servers)}")

                    for sv in servers:
                        pre = server_info(page, sv)
                        precheck = None
                        if pre:
                            precheck = dict(pre)
                            precheck["days"] = remaining_days(pre.get("expires_at"))
                            cd = api_get(page, f"/api/client/servers/{short_id(sv)}/renew/cooldown")
                            if cd and cd.get("__ok") and isinstance(cd.get("data"), dict):
                                precheck["cooldown"] = int(cd["data"].get("seconds", 0) or 0)
                        try:
                            r = renew_one(ctx, page, cdp, sv, precheck)
                        except PWTimeout as e:
                            r = {"status": "❌ 续期失败", "message": f"超时: {str(e).splitlines()[0][:100]}"}
                        except Exception as e:
                            r = {"status": "❌ 续期失败", "message": f"异常: {str(e)[:140]}"}
                        r.setdefault("before_exp", (pre or {}).get("expires_at", "")[:10])
                        r["email"] = email
                        r["server"] = sv
                        results.append(r)
                        print(f"  {r['status']} {r.get('message', '')}")
                        send_tg(fmt_msg(r["status"], email, sv, r.get("message", ""),
                                        r.get("before_exp", ""), r.get("after_exp", "")),
                                r.get("screenshot", ""))
                        if "成功" in r["status"] and r.get("expires_at"):
                            updateCronSchedule(r["expires_at"])
                        time.sleep(random.randint(2, 5))
                finally:
                    try:
                        if not page.is_closed():
                            page.close()
                    except Exception:
                        pass
        finally:
            close_browser(browser)

    ok = sum(1 for r in results if "成功" in r["status"])
    skip = sum(1 for r in results if "跳过" in r["status"])
    fail = len(results) - ok - skip
    print(f"\n{'=' * 46}\n📊 汇总：{ok} 成功 / {skip} 跳过 / {fail} 失败，共 {len(results)} 台\n{'=' * 46}")
    if fail:
        sys.exit(2)


if __name__ == "__main__":
    main()
