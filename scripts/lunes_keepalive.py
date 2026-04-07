"""
Lunes Host 自动登录保活
- 使用 Scrapling StealthyFetcher 内置绕过 Cloudflare Turnstile
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
    from scrapling.fetchers import StealthyFetcher, StealthySession
    _ENGINE = "scrapling"
except ImportError:
    _ENGINE = "none"
    print("⚠️ scrapling 未安装，请运行: pip install scrapling[fetchers] && scrapling install", flush=True)

# ==================== 配置 ====================
BASE_URL = os.environ.get("LUNES_BASE_URL", "https://betadash.lunes.host").rstrip("/")
LOGIN_URL = f"{BASE_URL}/login?next=/"
STORAGE_SECRET_NAME = os.environ.get("LUNES_STORAGE_STATE_NAME", "LUNES_STORAGE_STATE")
PROXY_DSN = os.environ.get("LUNES_PROXY_DSN", os.environ.get("PROXY_DSN", "")).strip()
TURNSTILE_WAIT = int(os.environ.get("LUNES_TURNSTILE_WAIT", "30"))
POST_LOGIN_WAIT = int(os.environ.get("LUNES_POST_LOGIN_WAIT", "30"))


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
    """Lunes Host 自动保活 — 基于 Scrapling StealthyFetcher"""

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

    # ==================== Cloudflare / 页面状态检测 ====================

    def is_cloudflare_challenge(self, page):
        """检测是否在 Cloudflare 验证页"""
        try:
            title = page.title()
            if "just a moment" in title.lower():
                return True
        except Exception:
            pass

        text_patterns = [
            "Performing security verification",
            "Verify you are human",
            "Checking your browser",
            "Just a moment",
        ]
        try:
            body = page.locator("body").inner_text(timeout=2000)
            if any(p in body for p in text_patterns):
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
                if page.locator(sel).first.is_visible(timeout=1500):
                    return True
            except Exception:
                pass
        return False

    def is_authenticated(self, page):
        if self.is_cloudflare_challenge(page):
            return False
        return not self.is_login_page(page)

    # ==================== 核心流程 ====================

    def open_page(self, page, url, label="页面", timeout=60000):
        """打开页面并等待 Cloudflare 验证自动通过（Scrapling 处理）"""
        self.log(f"访问 {label}: {url}", "STEP")
        page.goto(url, timeout=timeout)
        self.wait_page_ready(page, timeout=timeout)

        if self.is_cloudflare_challenge(page):
            self.log(f"{label} 出现 Cloudflare 验证，等待自动通过...", "WARN")
            self.shot(page, f"{label}_cf")
            # StealthyFetcher 已经在底层处理 Cloudflare，额外等待
            for i in range(30):
                if not self.is_cloudflare_challenge(page):
                    self.log(f"Cloudflare 验证已通过", "SUCCESS")
                    return True
                time.sleep(1)
                if i > 0 and i % 10 == 0:
                    self.log(f"  验证等待中... ({i}/30秒)")
            self.log("Cloudflare 验证等待超时", "WARN")
            return False

        return True

    def wait_for_login_form(self, page, retries=3):
        """等待登录表单出现"""
        for attempt in range(1, retries + 1):
            # 先处理 Cloudflare
            if self.is_cloudflare_challenge(page):
                self.open_page(page, page.url, f"登录表单等待({attempt})")

            try:
                page.locator('input[name="email"]').first.wait_for(state="visible", timeout=15000)
                page.locator('input[name="password"]').first.wait_for(state="visible", timeout=15000)
                self.log(f"登录表单已就绪（第 {attempt} 次）", "SUCCESS")
                return True
            except Exception:
                self.log(f"第 {attempt} 次等待登录表单失败", "WARN")
                self.shot(page, f"login_form_wait_{attempt}")

                if attempt < retries:
                    try:
                        page.goto(LOGIN_URL, timeout=60000)
                        self.wait_page_ready(page, timeout=60000)
                    except Exception:
                        pass

        self.log("多次尝试后仍未等到登录表单", "ERROR")
        return False

    def fill_and_login(self, page):
        """填写凭据并登录"""
        if not self.email or not self.password:
            self.log("缺少 LUNES_EMAIL 或 LUNES_PASSWORD", "ERROR")
            return False

        if not self.wait_for_login_form(page):
            return False

        try:
            email_input = page.locator('input[name="email"]').first
            password_input = page.locator('input[name="password"]').first

            email_input.click()
            time.sleep(random.uniform(0.2, 0.5))
            email_input.fill("")
            email_input.type(self.email, delay=random.randint(30, 80))

            time.sleep(random.uniform(0.3, 0.8))

            password_input.click()
            time.sleep(random.uniform(0.2, 0.5))
            password_input.fill("")
            password_input.type(self.password, delay=random.randint(30, 80))
            self.log("已输入登录凭据", "SUCCESS")
        except Exception as e:
            self.log(f"输入凭据失败: {e}", "ERROR")
            return False

        self.shot(page, "login_filled")

        # 等待 Turnstile 自动完成（Scrapling 处理）
        self.log(f"等待 Turnstile/提交处理，最长 {TURNSTILE_WAIT} 秒", "STEP")
        time.sleep(3)

        try:
            page.locator('button[type="submit"], .submit-btn').first.click()
            self.log("已提交登录", "SUCCESS")
        except Exception as e:
            self.log(f"提交登录失败: {e}", "ERROR")
            return False

        # 等待登录完成
        deadline = time.time() + POST_LOGIN_WAIT
        while time.time() < deadline:
            self.wait_page_ready(page, timeout=5000)
            if self.is_cloudflare_challenge(page):
                self.log("登录后触发 Cloudflare 验证，等待通过...", "WARN")
                for i in range(30):
                    if not self.is_cloudflare_challenge(page):
                        break
                    time.sleep(1)
                self.wait_page_ready(page, timeout=10000)

            if self.is_authenticated(page):
                self.log("Lunes 登录成功", "SUCCESS")
                self.shot(page, "login_success")
                return True
            time.sleep(1)

        self.shot(page, "login_failed")
        self.log("登录后仍停留在登录页", "ERROR")
        return False

    def keepalive(self, page):
        """保活访问"""
        self.log("执行保活访问", "STEP")
        targets = [
            (f"{BASE_URL}/", "首页"),
            (page.url, "当前页面"),
        ]

        cf_blocked = False
        seen = set()
        for url, name in targets:
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                page.goto(url, timeout=30000)
                self.wait_page_ready(page, timeout=30000)

                if self.is_cloudflare_challenge(page):
                    self.log(f"访问 {name} 被 Cloudflare 拦截", "WARN")
                    cf_blocked = True
                    continue

                self.log(f"已访问: {name} ({page.url})", "SUCCESS")
                time.sleep(2)
            except Exception as e:
                self.log(f"访问 {name} 失败: {e}", "WARN")

        if cf_blocked:
            self.log("保活访问被 Cloudflare 拦截，本次保活可能无效", "WARN")

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
        print("\n" + "=" * 50, flush=True)
        print("🚀 Lunes Host 自动登录保活", flush=True)
        print("=" * 50 + "\n", flush=True)

        self.log(f"站点: {BASE_URL}")
        self.log(f"账号: {self.email or '未配置'}")
        self.log(f"会话状态: {'有' if self.storage_state_b64 else '无'}")
        self.log(f"引擎: {_ENGINE}")

        if _ENGINE != "scrapling":
            self.log("需要 scrapling 库！安装: pip install scrapling[fetchers] && scrapling install", "ERROR")
            self.notify(False, "scrapling 未安装")
            sys.exit(1)

        if not self.storage_state_b64 and (not self.email or not self.password):
            self.log(
                f"缺少凭据。至少需要 {STORAGE_SECRET_NAME} 或 LUNES_EMAIL/LUNES_PASSWORD",
                "ERROR",
            )
            self.notify(False, "缺少凭据")
            sys.exit(1)

        # 构建 session 参数
        session_kwargs = {
            "headless": True,
            "solve_cloudflare": True,
            "network_idle": True,
        }

        if PROXY_DSN:
            try:
                parsed = urlparse(PROXY_DSN)
                session_kwargs["proxy"] = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                if parsed.username or parsed.password:
                    session_kwargs["proxy"] = (
                        f"{parsed.scheme}://{parsed.username or ''}:{parsed.password or ''}"
                        f"@{parsed.hostname}:{parsed.port}"
                    )
                self.log(f"启用代理: {parsed.scheme}://{parsed.hostname}:{parsed.port}")
            except Exception as e:
                self.log(f"代理配置解析失败: {e}", "WARN")

        state = self.decode_storage_state()
        if state:
            # 从 storage state 提取 cookies 注入
            cookies = state.get("cookies", [])
            if cookies:
                # 转换为 Scrapling/Playwright 格式
                formatted = []
                for c in cookies:
                    fc = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ""),
                        "path": c.get("path", "/"),
                    }
                    if c.get("expires"):
                        fc["expires"] = c["expires"]
                    formatted.append(fc)
                session_kwargs["cookies"] = formatted

        try:
            with StealthySession(**session_kwargs) as session:
                page = session.fetch(f"{BASE_URL}/", google_search=False)
                pw_page = page._page  # 获取底层 Playwright page 对象

                self.log(f"当前 URL: {pw_page.url}")

                # 处理 Cloudflare
                if self.is_cloudflare_challenge(pw_page):
                    self.log("首页 Cloudflare 验证中，等待通过...", "WARN")
                    for i in range(30):
                        if not self.is_cloudflare_challenge(pw_page):
                            self.log("Cloudflare 验证已通过", "SUCCESS")
                            break
                        time.sleep(1)
                        if i > 0 and i % 10 == 0:
                            self.log(f"  验证等待中... ({i}/30秒)")
                    else:
                        self.log("首页 Cloudflare 验证超时", "WARN")

                self.shot(pw_page, "home")

                if not self.is_authenticated(pw_page):
                    self.log("当前未登录，尝试账号密码登录", "WARN")
                    # 导航到登录页
                    pw_page.goto(LOGIN_URL, timeout=60000)
                    self.wait_page_ready(pw_page, timeout=60000)

                    # 处理 Cloudflare
                    if self.is_cloudflare_challenge(pw_page):
                        self.log("登录页 Cloudflare 验证中...", "WARN")
                        for i in range(30):
                            if not self.is_cloudflare_challenge(pw_page):
                                break
                            time.sleep(1)

                    if not self.fill_and_login(pw_page):
                        self.notify(False, "Lunes 登录失败")
                        sys.exit(1)
                else:
                    self.log("已通过历史会话登录", "SUCCESS")

                self.keepalive(pw_page)

                # 保存会话
                try:
                    context = pw_page.context
                    self.save_storage_state(context)
                except Exception as e:
                    self.log(f"保存会话状态失败: {e}", "WARN")

                self.notify(True)
                print("\n✅ 成功！\n", flush=True)

        except Exception as e:
            self.log(f"异常: {e}", "ERROR")
            traceback.print_exc()
            self.notify(False, str(e))
            sys.exit(1)


if __name__ == "__main__":
    LunesKeepAlive().run()
