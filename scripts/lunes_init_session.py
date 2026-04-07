"""
Lunes Host 本地初始化登录态（基于 opencli Browser Bridge）

用途:
- 复用你已连接好的真实浏览器（例如 Windows 上的 Chrome）
- 通过 opencli 触发浏览器桥接
- 从 opencli daemon 导出 betadash.lunes.host 的 cookies
- 生成 Playwright 可用的 LUNES_STORAGE_STATE（base64）

前提:
- opencli 已安装
- opencli Browser Bridge 扩展已连接到你的浏览器
- 你已经在真实浏览器里完成 Lunes Host 登录
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

BASE_URL = os.environ.get("LUNES_BASE_URL", "https://betadash.lunes.host").rstrip("/")
OUTPUT_FILE = os.environ.get("LUNES_STORAGE_OUTPUT", "LUNES_STORAGE_STATE.txt")
OPENCLI_DAEMON_PORT = int(os.environ.get("OPENCLI_DAEMON_PORT", "19825"))
OPENCLI_DAEMON_URL = f"http://127.0.0.1:{OPENCLI_DAEMON_PORT}"
OPENCLI_WORKSPACE = os.environ.get("OPENCLI_WORKSPACE", "default")
OPENCLI_TRIGGER_TIMEOUT = int(os.environ.get("LUNES_OPENCLI_TRIGGER_TIMEOUT", "60"))
COOKIE_WAIT = int(os.environ.get("LUNES_COOKIE_WAIT", "15"))


def request_daemon(pathname, payload=None, timeout=30):
    url = f"{OPENCLI_DAEMON_URL}{pathname}"
    headers = {"X-OpenCLI": "1"}
    data = None

    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


def send_command(action, **params):
    payload = {"id": f"cmd_{int(time.time() * 1000)}", "action": action, **params}
    result = request_daemon("/command", payload=payload, timeout=30)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "opencli daemon command failed")
    return result.get("data")


def trigger_opencli_bridge():
    cmd = ["opencli", "web", "read", "--url", BASE_URL]
    print(f"🔹 触发 opencli 浏览器桥接: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=OPENCLI_TRIGGER_TIMEOUT)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise RuntimeError(f"opencli web read 失败，退出码 {proc.returncode}")

    if proc.stdout.strip():
        print(proc.stdout.strip())


def wait_for_extension():
    deadline = time.time() + COOKIE_WAIT
    while time.time() < deadline:
        try:
            status = request_daemon("/status", timeout=5)
            if status.get("extensionConnected"):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def normalize_cookie(cookie):
    domain = cookie.get("domain", "")
    expiry = cookie.get("expirationDate")
    if expiry is None:
        expiry = cookie.get("expires")
    if expiry is None:
        expiry = -1

    same_site_map = {
        "no_restriction": "None",
        "lax": "Lax",
        "strict": "Strict",
        "unspecified": "Lax",
        "none": "None",
    }
    raw_same_site = str(cookie.get("sameSite", "Lax")).lower()
    same_site = same_site_map.get(raw_same_site, "Lax")

    return {
        "name": cookie.get("name", ""),
        "value": cookie.get("value", ""),
        "domain": domain,
        "path": cookie.get("path", "/"),
        "expires": float(expiry),
        "httpOnly": bool(cookie.get("httpOnly", False)),
        "secure": bool(cookie.get("secure", True)),
        "sameSite": same_site,
    }


def export_storage_state():
    parsed = urlparse(BASE_URL)
    domain = parsed.hostname or "betadash.lunes.host"

    cookies = send_command(
        "cookies",
        workspace=OPENCLI_WORKSPACE,
        domain=domain,
        url=BASE_URL,
    )

    if not isinstance(cookies, list) or not cookies:
        raise RuntimeError(f"没有从 opencli 导出到 {domain} 的 cookies，请确认浏览器里已经登录")

    normalized = [normalize_cookie(cookie) for cookie in cookies if cookie.get("name")]
    state = {"cookies": normalized, "origins": []}
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    value = base64.b64encode(raw.encode("utf-8")).decode("ascii")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(value)
        f.write("\n")

    return value, len(normalized)


def main():
    print("\n" + "=" * 56)
    print("🚀 Lunes Host 本地初始化登录态（opencli）")
    print("=" * 56)
    print(f"站点: {BASE_URL}")
    print(f"输出文件: {OUTPUT_FILE}")
    print(f"daemon: {OPENCLI_DAEMON_URL}")
    print("\n前提说明:")
    print("1. 你的真实浏览器已经装好并启用了 opencli Browser Bridge 扩展")
    print("2. 你已经在那个浏览器里完成 Lunes Host 登录")
    print("3. opencli 的具体业务命令能够复用该浏览器会话")
    print("=" * 56 + "\n")

    try:
        trigger_opencli_bridge()
        if not wait_for_extension():
            raise RuntimeError("opencli daemon 未检测到扩展连接，请先确认 Browser Bridge 已接通")

        value, cookie_count = export_storage_state()
        print(f"\n✅ 已导出 LUNES_STORAGE_STATE，cookies 数量: {cookie_count}")
        print(f"📄 文件: {OUTPUT_FILE}")
        print("\n请将文件中的完整内容复制到 GitHub Secret:")
        print("Secret 名称: LUNES_STORAGE_STATE")
        print("\n前 80 个字符预览:")
        print(value[:80] + "...")
    except subprocess.TimeoutExpired:
        print("\n❌ opencli 命令超时")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"\n❌ 无法连接 opencli daemon: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 初始化失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
