"""
Lunes Host 自动登录保活
- 使用 patchright (playwright 反检测分支) + 住宅代理绕过 Cloudflare
- 优先复用上次成功登录后的浏览器会话
- 会话失效时，尝试使用邮箱/密码重新登录
- 成功后自动更新浏览器会话到 GitHub Secret
- Telegram 通知
"""

import base64
import json
import os
import random
import sys
import time
import traceback
from urllib.parse import urlparse

import requests

try:
    from patchright.sync_api import sync_playwright
    _ENGINE = "patchright"
except ImportError:
    from playwright.sync_api import sync_playwright
    _ENGINE = "playwright"

# ==================== 配置 ====================
BASE_URL = os.environ.get("LUNES_BASE_URL", "https://betadash.lunes.host").rstrip("/")
LOGIN_URL = f"{BASE_URL}/login?next=/"
STORAGE_SECRET_NAME = os.environ.get("LUNES_STORAGE_STATE_NAME", "LUNES_STORAGE_STATE")
PROXY_DSN = os.environ.get("LUNES_PROXY_DSN", os.environ.get("PROXY_DSN", "")).strip()
TURNSTILE_WAIT = int(os.environ.get("LUNES_TURNSTILE_WAIT", "25"))
POST_LOGIN_WAIT = int(os.environ.get("LUNES_POST_LOGIN_WAIT", "20"))
CF_TIMEOUT = int(os.environ.get("LUNES_CF_TIMEOUT", "45"))


class Telegram:
    def __init__(self):
        self.token = os.environ.get("TG_BOT_TOKEN")
        self.chat_id = os.environ.get("TG_CHAT_ID")
        self.ok = bool(self.token and self.chat_id)

    def send(self, msg):
        if not self.ok:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=30,
            )
        except Exception:
            pass

    def photo(self, path, caption=""):
        if not self.ok or not os.path.exists(path):
            return
        try:
            with open(path, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption[:1024]},
                    files={"photo": f},
                    timeout=60,
                )
        except Exception:
            pass


class SecretUpdater:
    def __init__(self):
        self.token = os.environ.get("REPO_TOKEN")
        self.repo = os.environ.get("GITHUB_REPOSITORY")
        self.ok = bool(self.token and self.repo)
        if self.ok:
            print("✅ Secret 自动更新已启用")
        else:
            print("⚠️ Secret 自动更新未启用（需要 REPO_TOKEN）")

    def update(self, name, value):
        if not self.ok:
            return False
        try:
            from nacl import encoding, public

            headers = {
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
            }
            r = requests.get(
                f"https://api.github.com/repos/{self.repo}/actions/secrets/public-key",
                headers=headers, timeout=30,
            )
            if r.status_code != 200:
                return False
            key_data = r.json()
            pk = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
            encrypted = public.SealedBox(pk).encrypt(value.encode())
            r = requests.put(
                f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                headers=headers,
                json={
                    "encrypted_value": base64.b64encode(encrypted).decode(),
                    "key_id": key_data["key_id"],
                },
                timeout=30,
            )
            return r.status_code in [201, 204]
        except Exception as e:
            print(f"更新 Secret 失败: {e}")
            return False


class LunesKeepAlive:
    def __init__(self):
        self.email = os.environ.get("LUNES_EMAIL", "").strip()
        self.password = os.environ.get("LUNES_PASSWORD", "").strip()
        self.storage_state_b64 = os.environ.get(STORAGE_SECRET_NAME, "").strip()
        self.tg = Telegram()
        self.secret = SecretUpdater()
        self.shots = []
        self.logs = []
        self.n = 0

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line, flush=True)
        self.logs.append(line)

    def shot(self, page, name):
        self.n += 1
        filename = f"lunes_{self.n:02d}_{name}.png"
        try:
            page.screenshot(path=filename, full_page=True)
            self.shots.append(filename)
        except Exception:
            pass
        return filename

    def decode_storage_state(self):
        if not self.storage_state_b64:
            return None
        try:
            decoded = base64.b64decode(self.storage_state_b64).decode("utf-8")
            state = json.loads(decoded)
            if isinstance(state, dict) and "cookies" in state:
                self.log("已加载 LUNES_STORAGE_STATE", "SUCCESS")
                return state
        except Exception as e:
            self.log(f"解析 LUNES_STORAGE_STATE 失败: {e}", "WARN")
        return None

    def save_storage_state(self, context):
        state = context.storage_state()
        raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        value = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        if self.secret.update(STORAGE_SECRET_NAME, value):
            self.log(f"已自动更新 {STORAGE_SECRET_NAME}", "SUCCESS")
            self.tg.send(f"🔑 <b>Lunes 会话已更新</b>\n\nSecret <b>{STORAGE_SECRET_NAME}</b> 已刷新")
        else:
            self.log(f"自动更新 {STORAGE_SECRET_NAME} 失败", "WARN")

    # ==================== 页面状态检测 ====================

    def is_cf_challenge(self, page):
        try:
            title = page.title()
            if "just a moment" in title.lower():
                return True
        except Exception:
            pass
        try:
            body = page.locator("body").inner_text(timeout=1500)
            for p in ["Just a moment", "Checking your browser", "Verify you are human"]:
                if p in body:
                    return True
        except Exception:
            pass
        return False

    def is_login_page(self, page):
        try:
            path = urlparse(page.url).path.rstrip("/") or "/"
            if path == "/login":
                return True
        except Exception:
            pass
        for sel in ['input[name="email"]', 'input[name="password"]']:
            try:
                if page.locator(sel).first.is_visible(timeout=1200):
                    return True
            except Exception:
                pass
        return False

    def is_authenticated(self, page):
        if self.is_cf_challenge(page):
            return False
        # 页面在 chrome-error:// 或非目标域名 → 导航失败，不算已认证
        try:
            url = page.url
            if url.startswith("chrome-error://") or url.startswith("chrome://"):
                return False
            host = urlparse(url).hostname or ""
            target = urlparse(BASE_URL).hostname or ""
            if host != target:
                return False
        except Exception:
            return False
        return not self.is_login_page(page)

    # ==================== Cloudflare 处理 ====================

    def wait_cf_clear(self, page, timeout=None):
        """等待 Cloudflare 全页验证自动通过。只点击一次复选框。"""
        if timeout is None:
            timeout = CF_TIMEOUT
        if not self.is_cf_challenge(page):
            return True

        self.log(f"检测到 Cloudflare 验证，最长等待 {timeout} 秒", "WARN")
        self.shot(page, "cf_detected")

        start = time.time()
        clicked = False

        while time.time() - start < timeout:
            elapsed = int(time.time() - start)

            if not self.is_cf_challenge(page):
                self.log(f"Cloudflare 验证已通过（{elapsed}秒）", "SUCCESS")
                return True

            # 10 秒后只点一次
            if not clicked and elapsed >= 10:
                clicked = True
                if self._click_cf_checkbox(page):
                    self.log("已点击验证控件，等待通过...", "STEP")
                    time.sleep(8)
                    if not self.is_cf_challenge(page):
                        self.log(f"Cloudflare 验证通过（{elapsed}秒）", "SUCCESS")
                        return True
                else:
                    self.log("未找到验证控件，继续等待自动通过", "WARN")

            # 模拟鼠标移动
            if elapsed > 0 and elapsed % 5 == 0:
                try:
                    page.mouse.move(200 + random.randint(-100, 100), 200 + random.randint(-100, 100))
                except Exception:
                    pass

            if elapsed > 0 and elapsed % 10 == 0:
                self.log(f"  验证等待中... ({elapsed}/{timeout}秒)")

            time.sleep(2)

        self.log(f"Cloudflare 验证超时 ({timeout}秒)", "WARN")
        return False

    def _click_cf_checkbox(self, page):
        """点击 Cloudflare 复选框（iframe 内外都尝试）"""
        # 页面直接可见
        for sel in ['label.cb-lb', 'label.ctp-checkbox-label', '[role="checkbox"]']:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=800):
                    el.click(force=True, timeout=2000)
                    return True
            except Exception:
                pass

        # Cloudflare iframe 内
        for frame in page.frames:
            if "challenges.cloudflare.com" not in frame.url and "turnstile" not in frame.url:
                continue
            for sel in ['label.cb-lb', 'label.ctp-checkbox-label', '[role="checkbox"]', 'input[type="checkbox"]']:
                try:
                    el = frame.locator(sel).first
                    if el.is_visible(timeout=800):
                        el.click(force=True, timeout=2000)
                        return True
                except Exception:
                    pass

        # iframe 坐标点击
        for sel in ['iframe[src*="challenges.cloudflare.com"]', 'iframe[src*="turnstile"]']:
            try:
                iframe = page.locator(sel).first
                if not iframe.is_visible(timeout=800):
                    continue
                box = iframe.bounding_box()
                if box:
                    x = box["x"] + min(max(box["width"] * 0.18, 24), 42)
                    y = box["y"] + box["height"] / 2
                    page.mouse.click(x, y)
                    return True
            except Exception:
                pass
        return False

    # ==================== 核心流程 ====================

    def safe_goto(self, page, url, timeout=60000):
        """导航到 URL，遇到非 2xx 或连接错误时重试"""
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                page.goto(url, timeout=timeout, wait_until="commit")
                break
            except Exception as e:
                err = str(e)
                if "ERR_HTTP_RESPONSE_CODE_FAILURE" in err or "net::ERR_CONNECTION" in err or "net::ERR_ABORTED" in err:
                    self.log(f"导航失败（第 {attempt} 次）: {err[:80]}", "WARN")
                    if attempt < max_retries:
                        time.sleep(3)
                        continue
                    # 最后一次还是失败，但不抛异常，让后续逻辑处理
                else:
                    raise
        try:
            page.wait_for_load_state("domcontentloaded", timeout=min(timeout, 30000))
        except Exception:
            pass
        time.sleep(2)

        # 如果停留在 chrome-error 页，尝试刷新
        if page.url.startswith("chrome-error://") or page.url.startswith("chrome://"):
            self.log("页面停留在错误页，尝试刷新...", "WARN")
            try:
                page.goto(url, timeout=timeout, wait_until="commit")
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass

    def open_root(self, page):
        self.log("访问首页", "STEP")
        self.safe_goto(page, f"{BASE_URL}/")
        self.wait_cf_clear(page)
        self.log(f"当前 URL: {page.url}")
        self.shot(page, "home")

    def login(self, page):
        if not self.email or not self.password:
            self.log("缺少 LUNES_EMAIL 或 LUNES_PASSWORD", "ERROR")
            return False

        self.log("尝试账号密码登录", "STEP")
        self.safe_goto(page, LOGIN_URL)

        if not self.wait_cf_clear(page):
            self.log("登录页 Cloudflare 未通过", "WARN")

        self.shot(page, "login")

        # 等待登录表单
        for attempt in range(1, 4):
            if self.is_cf_challenge(page):
                self.wait_cf_clear(page)
            try:
                page.locator('input[name="email"]').first.wait_for(state="visible", timeout=10000)
                page.locator('input[name="password"]').first.wait_for(state="visible", timeout=10000)
                self.log(f"登录表单已就绪（第 {attempt} 次）", "SUCCESS")
                break
            except Exception:
                self.log(f"第 {attempt} 次等待登录表单失败", "WARN")
                if attempt == 3:
                    self.log("多次尝试后仍未等到登录表单", "ERROR")
                    return False
                self.safe_goto(page, LOGIN_URL)

        # 输入凭据
        try:
            email = page.locator('input[name="email"]').first
            pwd = page.locator('input[name="password"]').first

            email.click()
            time.sleep(random.uniform(0.2, 0.5))
            email.fill("")
            email.type(self.email, delay=random.randint(30, 80))

            time.sleep(random.uniform(0.3, 0.8))

            pwd.click()
            time.sleep(random.uniform(0.2, 0.5))
            pwd.fill("")
            pwd.type(self.password, delay=random.randint(30, 80))
            self.log("已输入登录凭据", "SUCCESS")
        except Exception as e:
            self.log(f"输入凭据失败: {e}", "ERROR")
            return False

        self.shot(page, "login_filled")
        time.sleep(2)

        # 检查是否有 Turnstile 验证码
        if self.is_cf_challenge(page):
            self.log("登录表单包含 Cloudflare Turnstile，等待解决")
            if not self.wait_cf_clear(page, timeout=60):
                self.log("Turnstile 验证超时", "ERROR")
                self.shot(page, "turnstile_timeout")
                return False

        # 提交
        try:
            page.locator('button[type="submit"], .submit-btn').first.click()
        except Exception as e:
            self.log(f"提交失败: {e}", "ERROR")
            return False

        # 等待登录完成
        deadline = time.time() + POST_LOGIN_WAIT
        while time.time() < deadline:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            time.sleep(1)

            if self.is_cf_challenge(page):
                self.wait_cf_clear(page)

            if self.is_authenticated(page):
                self.log("Lunes 登录成功", "SUCCESS")
                self.shot(page, "login_success")
                return True

        self.shot(page, "login_failed")
        self.log("登录后仍停留在登录页", "ERROR")
        return False

    def keepalive(self, page):
        self.log("执行保活访问", "STEP")
        targets = [(f"{BASE_URL}/", "首页"), (page.url, "当前页面")]
        cf_blocked = False
        seen = set()
        for url, name in targets:
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                self.safe_goto(page, url, timeout=30000)
                if self.is_cf_challenge(page):
                    self.log(f"访问 {name} 被 Cloudflare 拦截", "WARN")
                    cf_blocked = True
                    continue
                self.log(f"已访问: {name} ({page.url})", "SUCCESS")
            except Exception as e:
                self.log(f"访问 {name} 失败: {e}", "WARN")

        if cf_blocked:
            self.log("保活被 Cloudflare 拦截，本次可能无效", "WARN")
        self.shot(page, "done")

    def notify(self, ok, err=""):
        if not self.tg.ok:
            return
        msg = f"""<b>🤖 Lunes Host 登录保活</b>

<b>状态:</b> {"✅ 成功" if ok else "❌ 失败"}
<b>站点:</b> {BASE_URL}
<b>账号:</b> {self.email or "未配置"}
<b>时间:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}"""
        if err:
            msg += f"\n<b>错误:</b> {err}"
        msg += "\n\n<b>日志:</b>\n" + "\n".join(self.logs[-6:])
        self.tg.send(msg)
        if self.shots:
            if ok:
                self.tg.photo(self.shots[-1], "Lunes 保活完成")
            else:
                for s in self.shots[-3:]:
                    self.tg.photo(s, s)

    def run(self):
        print("\n" + "=" * 50, flush=True)
        print("🚀 Lunes Host 自动登录保活", flush=True)
        print("=" * 50 + "\n", flush=True)

        self.log(f"站点: {BASE_URL}")
        self.log(f"账号: {self.email or '未配置'}")
        self.log(f"会话状态: {'有' if self.storage_state_b64 else '无'}")
        self.log(f"引擎: {_ENGINE}")
        self.log(f"代理: {'有' if PROXY_DSN else '无'}")

        if not self.storage_state_b64 and (not self.email or not self.password):
            self.log("缺少凭据", "ERROR")
            self.notify(False, "缺少凭据")
            sys.exit(1)

        with sync_playwright() as p:
            launch_args = {
                "headless": False,
                "args": ["--no-sandbox", "--disable-gpu", "--disable-http2"],
            }

            if PROXY_DSN:
                try:
                    parsed = urlparse(PROXY_DSN)
                    # Playwright proxy server scheme: socks5/socks4 kept as-is, all others → http
                    scheme = parsed.scheme.lower()
                    if scheme not in ("socks5", "socks4"):
                        scheme = "http"
                    proxy_config = {"server": f"{scheme}://{parsed.hostname}:{parsed.port}"}
                    if parsed.username:
                        proxy_config["username"] = parsed.username
                    if parsed.password:
                        proxy_config["password"] = parsed.password
                    launch_args["proxy"] = proxy_config
                    self.log(f"启用代理: {parsed.hostname}:{parsed.port} (scheme→{scheme})")
                except Exception as e:
                    self.log(f"代理配置失败: {e}", "WARN")

            browser = p.chromium.launch(**launch_args)

            context_kwargs = {
                "viewport": {"width": 1600, "height": 900},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
            }

            state = self.decode_storage_state()
            if state:
                context_kwargs["storage_state"] = state

            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            try:
                self.open_root(page)

                if not self.is_authenticated(page):
                    self.log("当前未登录", "WARN")
                    if not self.login(page):
                        self.notify(False, "Lunes 登录失败")
                        sys.exit(1)
                elif self.is_cf_challenge(page):
                    self.log("首页在 Cloudflare 验证页，尝试登录", "WARN")
                    if not self.login(page):
                        self.notify(False, "Lunes 登录失败（Cloudflare）")
                        sys.exit(1)
                else:
                    self.log("已通过历史会话登录", "SUCCESS")

                self.keepalive(page)
                self.save_storage_state(context)
                self.notify(True)
                print("\n✅ 成功！\n", flush=True)
            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
                self.shot(page, "exception")
                traceback.print_exc()
                self.notify(False, str(e))
                sys.exit(1)
            finally:
                browser.close()


if __name__ == "__main__":
    LunesKeepAlive().run()
