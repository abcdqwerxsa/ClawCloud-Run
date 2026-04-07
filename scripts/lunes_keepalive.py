"""
Lunes Host 自动登录保活
- 优先复用上次成功登录后的浏览器会话
- 会话失效时，尝试使用邮箱/密码重新登录
- 成功后自动更新浏览器会话到 GitHub Secret
- Telegram 通知

注意:
- 站点登录页带 Cloudflare Turnstile，自动重新登录不保证每次都能通过
- 最稳妥的方式是让脚本持续刷新已登录会话，减少重新登录次数
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
from playwright.sync_api import sync_playwright

# ==================== 配置 ====================
BASE_URL = os.environ.get("LUNES_BASE_URL", "https://betadash.lunes.host").rstrip("/")
LOGIN_URL = f"{BASE_URL}/login?next=/"
STORAGE_SECRET_NAME = os.environ.get("LUNES_STORAGE_SECRET_NAME", "LUNES_STORAGE_STATE")
PROXY_DSN = os.environ.get("LUNES_PROXY_DSN", os.environ.get("PROXY_DSN", "")).strip()
TURNSTILE_WAIT = int(os.environ.get("LUNES_TURNSTILE_WAIT", "25"))
POST_LOGIN_WAIT = int(os.environ.get("LUNES_POST_LOGIN_WAIT", "20"))
CLOUDFLARE_CLICK_DELAY = int(os.environ.get("LUNES_CLOUDFLARE_CLICK_DELAY", "6"))
CLOUDFLARE_MAX_ATTEMPTS = int(os.environ.get("LUNES_CLOUDFLARE_MAX_ATTEMPTS", "3"))


class Telegram:
    """Telegram 通知"""

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
    """GitHub Secret 更新器"""

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
                headers=headers,
                timeout=30,
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
    """Lunes Host 自动保活"""

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
        print(line)
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
            self.log("LUNES_STORAGE_STATE 格式无效", "WARN")
        except Exception as e:
            self.log(f"解析 LUNES_STORAGE_STATE 失败: {e}", "WARN")
        return None

    def encode_storage_state(self, context):
        state = context.storage_state()
        raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        return base64.b64encode(raw.encode("utf-8")).decode("ascii")

    def save_storage_state(self, context):
        value = self.encode_storage_state(context)
        if self.secret.update(STORAGE_SECRET_NAME, value):
            self.log(f"已自动更新 {STORAGE_SECRET_NAME}", "SUCCESS")
            self.tg.send(f"🔑 <b>Lunes 会话已更新</b>\n\nSecret <b>{STORAGE_SECRET_NAME}</b> 已刷新")
        else:
            self.log(f"自动更新 {STORAGE_SECRET_NAME} 失败", "WARN")

    def wait_page_ready(self, page, timeout=30000):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception:
            pass
        time.sleep(2)

    def get_turnstile_token(self, page):
        token_selectors = [
            'textarea[name="g-recaptcha-response"]',
            'textarea[name="cf-turnstile-response"]',
            'input[name="cf-turnstile-response"]',
        ]

        for selector in token_selectors:
            try:
                token = page.locator(selector).first.input_value(timeout=500).strip()
                if token:
                    return token
            except Exception:
                pass
        return ""

    def has_cloudflare_widget(self, page):
        iframe_selectors = [
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[title*="Cloudflare"]',
            'iframe[src*="turnstile"]',
        ]

        for selector in iframe_selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=800):
                    return True
            except Exception:
                pass
        return False

    def click_cloudflare_widget(self, page):
        frame_selectors = [
            'label.ctp-checkbox-label',
            'label.cb-lb',
            'input[type="checkbox"]',
            '[role="checkbox"]',
            '.ctp-checkbox-container',
            '.ctp-checkbox',
        ]

        for frame in page.frames:
            if "challenges.cloudflare.com" not in frame.url and "turnstile" not in frame.url:
                continue
            for selector in frame_selectors:
                try:
                    el = frame.locator(selector).first
                    if el.is_visible(timeout=800):
                        el.click(force=True, timeout=2000)
                        self.log(f"已点击 Cloudflare 验证控件: {selector}", "SUCCESS")
                        return True
                except Exception:
                    pass

        iframe_selectors = [
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[title*="Cloudflare"]',
            'iframe[src*="turnstile"]',
        ]

        for selector in iframe_selectors:
            try:
                iframe = page.locator(selector).first
                if not iframe.is_visible(timeout=800):
                    continue
                box = iframe.bounding_box()
                if not box:
                    continue
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                page.mouse.click(x, y)
                self.log("已点击 Cloudflare iframe 中心点", "SUCCESS")
                return True
            except Exception:
                pass

        return False

    def handle_cloudflare_challenge(self, page, stage):
        if self.get_turnstile_token(page):
            self.log("Turnstile token 已就绪", "SUCCESS")
            return True

        if not self.has_cloudflare_widget(page):
            return False

        self.log(f"{stage}: 检测到 Cloudflare 验证，先等待 {CLOUDFLARE_CLICK_DELAY} 秒", "WARN")
        time.sleep(CLOUDFLARE_CLICK_DELAY)

        for attempt in range(1, CLOUDFLARE_MAX_ATTEMPTS + 1):
            if self.get_turnstile_token(page):
                self.log("Cloudflare 验证已通过", "SUCCESS")
                return True

            clicked = self.click_cloudflare_widget(page)
            if not clicked:
                self.log(f"第 {attempt} 次未找到可点击的 Cloudflare 控件", "WARN")
            else:
                self.shot(page, f"cloudflare_clicked_{attempt}")

            time.sleep(4)
            if self.get_turnstile_token(page):
                self.log("Cloudflare 验证已通过", "SUCCESS")
                return True

            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

        self.log("Cloudflare 验证未明确通过，将继续后续流程", "WARN")
        return False

    def is_login_page(self, page):
        try:
            path = urlparse(page.url).path.rstrip("/") or "/"
            if path == "/login":
                return True
        except Exception:
            pass

        selectors = [
            'input[name="email"]',
            'input[name="password"]',
            'button:has-text("Continue to dashboard")',
        ]
        for selector in selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=1200):
                    return True
            except Exception:
                pass
        return False

    def is_authenticated(self, page):
        return not self.is_login_page(page)

    def wait_for_login_form(self, page, retries=3):
        """等待登录表单可交互。若未出现，则尝试处理 challenge 并刷新重试。"""
        for attempt in range(1, retries + 1):
            try:
                page.locator('input[name="email"]').first.wait_for(state="visible", timeout=10000)
                page.locator('input[name="password"]').first.wait_for(state="visible", timeout=10000)
                self.log(f"登录表单已就绪（第 {attempt} 次）", "SUCCESS")
                return True
            except Exception:
                self.log(f"第 {attempt} 次等待登录表单失败", "WARN")
                self.handle_cloudflare_challenge(page, f"等待登录表单第 {attempt} 次")
                self.shot(page, f"login_form_wait_{attempt}")

                if attempt < retries:
                    try:
                        page.reload(timeout=30000)
                        self.wait_page_ready(page, timeout=30000)
                    except Exception:
                        try:
                            page.goto(LOGIN_URL, timeout=30000)
                            self.wait_page_ready(page, timeout=30000)
                        except Exception:
                            pass

        self.log("多次尝试后仍未等到登录表单", "ERROR")
        return False

    def wait_turnstile(self, page):
        self.log(f"等待 Turnstile 就绪，最长 {TURNSTILE_WAIT} 秒", "STEP")
        self.handle_cloudflare_challenge(page, "登录前")

        for i in range(TURNSTILE_WAIT):
            token = self.get_turnstile_token(page)
            if token:
                self.log("Turnstile 已生成 token", "SUCCESS")
                return True

            if self.has_cloudflare_widget(page) and i > 0 and i % 6 == 0:
                self.handle_cloudflare_challenge(page, f"等待 Turnstile 第 {i} 秒")

            time.sleep(1)
            if i > 0 and i % 5 == 0:
                self.log(f"  Turnstile 等待中... ({i}/{TURNSTILE_WAIT}秒)")

        self.log("未检测到 Turnstile token，将继续尝试提交", "WARN")
        return False

    def open_root(self, page):
        self.log("访问首页", "STEP")
        page.goto(f"{BASE_URL}/", timeout=60000)
        self.wait_page_ready(page, timeout=60000)
        self.handle_cloudflare_challenge(page, "访问首页")
        self.log(f"当前 URL: {page.url}")
        self.shot(page, "home")

    def login(self, page):
        if not self.email or not self.password:
            self.log("缺少 LUNES_EMAIL 或 LUNES_PASSWORD，无法重新登录", "ERROR")
            return False

        self.log("会话失效，尝试账号密码登录", "STEP")
        page.goto(LOGIN_URL, timeout=60000)
        self.wait_page_ready(page, timeout=60000)
        self.handle_cloudflare_challenge(page, "打开登录页")
        self.shot(page, "login")

        if not self.wait_for_login_form(page):
            return False

        try:
            email_input = page.locator('input[name="email"]').first
            password_input = page.locator('input[name="password"]').first

            email_input.click()
            time.sleep(random.uniform(0.2, 0.6))
            email_input.fill("")
            email_input.type(self.email, delay=random.randint(30, 100))

            time.sleep(random.uniform(0.4, 0.9))

            password_input.click()
            time.sleep(random.uniform(0.2, 0.6))
            password_input.fill("")
            password_input.type(self.password, delay=random.randint(30, 100))
            self.log("已输入登录凭据", "SUCCESS")
        except Exception as e:
            self.log(f"输入登录凭据失败: {e}", "ERROR")
            return False

        self.wait_turnstile(page)
        self.shot(page, "login_filled")

        try:
            page.locator('button[type="submit"], .submit-btn').first.click()
        except Exception as e:
            self.log(f"提交登录失败: {e}", "ERROR")
            return False

        deadline = time.time() + POST_LOGIN_WAIT
        while time.time() < deadline:
            self.wait_page_ready(page, timeout=5000)
            self.handle_cloudflare_challenge(page, "提交登录后")
            if self.is_authenticated(page):
                self.log("Lunes 登录成功", "SUCCESS")
                self.shot(page, "login_success")
                return True
            time.sleep(1)

        self.shot(page, "login_failed")
        self.log("登录后仍停留在登录页，可能是 Turnstile 未通过或凭据无效", "ERROR")

        flash_selectors = [".flash-message", ".alert", '[role="alert"]']
        for selector in flash_selectors:
            try:
                text = page.locator(selector).first.inner_text(timeout=1000).strip()
                if text:
                    self.log(f"页面提示: {text}", "WARN")
                    break
            except Exception:
                pass
        return False

    def keepalive(self, page):
        self.log("执行保活访问", "STEP")
        targets = [
            (f"{BASE_URL}/", "首页"),
            (page.url, "当前页面"),
        ]

        seen = set()
        for url, name in targets:
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                page.goto(url, timeout=30000)
                self.wait_page_ready(page, timeout=30000)
                self.log(f"已访问: {name} ({page.url})", "SUCCESS")
                time.sleep(2)
            except Exception as e:
                self.log(f"访问 {name} 失败: {e}", "WARN")

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
                for shot in self.shots[-3:]:
                    self.tg.photo(shot, shot)

    def run(self):
        print("\n" + "=" * 50)
        print("🚀 Lunes Host 自动登录保活")
        print("=" * 50 + "\n")

        self.log(f"站点: {BASE_URL}")
        self.log(f"账号: {self.email or '未配置'}")
        self.log(f"会话状态: {'有' if self.storage_state_b64 else '无'}")

        if not self.storage_state_b64 and (not self.email or not self.password):
            self.log(
                f"缺少初始化凭据。至少需要 {STORAGE_SECRET_NAME} 或 LUNES_EMAIL/LUNES_PASSWORD",
                "ERROR",
            )
            self.notify(False, "缺少初始化凭据")
            sys.exit(1)

        with sync_playwright() as p:
            launch_args = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--exclude-switches=enable-automation",
                ],
            }

            if PROXY_DSN:
                try:
                    parsed = urlparse(PROXY_DSN)
                    proxy_config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
                    if parsed.username:
                        proxy_config["username"] = parsed.username
                    if parsed.password:
                        proxy_config["password"] = parsed.password
                    launch_args["proxy"] = proxy_config
                    self.log(f"启用代理: {proxy_config['server']}")
                except Exception as e:
                    self.log(f"代理配置解析失败: {e}", "WARN")

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

            page.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                window.chrome = { runtime: {} };
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : originalQuery(parameters)
                );
                """
            )

            try:
                self.open_root(page)

                if not self.is_authenticated(page):
                    self.log("当前未登录", "WARN")
                    if not self.login(page):
                        self.notify(False, "Lunes 登录失败")
                        sys.exit(1)
                else:
                    self.log("已通过历史会话登录", "SUCCESS")

                self.keepalive(page)
                self.save_storage_state(context)
                self.notify(True)
                print("\n✅ 成功！\n")
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
