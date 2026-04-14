"""
Daily tech digest collector for AI, cloud native, frameworks, and free platforms.

Features:
- Collects from stable sources: arXiv RSS, Hacker News front page, GitHub Trending,
  and optional configurable RSS/Atom feeds.
- Filters and ranks items for relevance to AI infra, cloud native tooling,
  frameworks, and free deployment platforms.
- Uses an OpenAI-compatible endpoint for concise summaries when configured.
- Generates a Markdown digest and optionally delivers it through Telegram
  as a short summary message plus the full Markdown file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


CATEGORY_LABELS = {
    "ai": "AI",
    "cloud": "云原生",
    "framework": "框架",
    "free-platform": "免费平台",
}
CATEGORY_ORDER = ["ai", "cloud", "framework", "free-platform"]
DEFAULT_ARXIV_FEEDS = [
    {"url": "https://export.arxiv.org/rss/cs.AI", "source": "arXiv cs.AI", "category": "ai"},
    {"url": "https://export.arxiv.org/rss/cs.LG", "source": "arXiv cs.LG", "category": "ai"},
    {"url": "https://export.arxiv.org/rss/cs.DC", "source": "arXiv cs.DC", "category": "cloud"},
]
PROGRESS_KEYWORDS = {
    "release",
    "launched",
    "launch",
    "announces",
    "announced",
    "beta",
    "ga",
    "generally available",
    "pricing",
    "free plan",
    "free tier",
    "available",
    "update",
    "v1",
    "2.0",
}


@dataclass
class DigestItem:
    source: str
    title: str
    url: str
    published_at: str = ""
    raw_summary: str = ""
    category_hint: str = ""
    signals: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    category: str = ""
    score: float = 0.0
    chinese_title: str = ""
    summary: str = ""
    reason: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def normalized_title(self) -> str:
        return normalize_text(self.title)

    @property
    def display_title(self) -> str:
        return self.chinese_title or self.title


class Telegram:
    def __init__(self, logger: "Logger", dry_run: bool = False):
        self.logger = logger
        self.token = os.environ.get("TG_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TG_CHAT_ID", "").strip()
        self.ok = bool(self.token and self.chat_id) and not dry_run

    def send(self, message: str) -> bool:
        if not self.ok:
            return False
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"},
                timeout=30,
            )
            return response.status_code == 200
        except Exception as exc:  # pragma: no cover
            self.logger.log(f"Telegram sendMessage failed: {exc}", "WARN")
            return False

    def document(self, path: Path, caption: str = "") -> bool:
        if not self.ok or not path.exists():
            return False
        try:
            with path.open("rb") as handle:
                response = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendDocument",
                    data={"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
                    files={"document": (path.name, handle, "text/markdown")},
                    timeout=60,
                )
            return response.status_code == 200
        except Exception as exc:  # pragma: no cover
            self.logger.log(f"Telegram sendDocument failed: {exc}", "WARN")
            return False

    def photo(self, image_path: Path, caption: str = "") -> bool:
        if not self.ok or not image_path.exists():
            return False
        try:
            with image_path.open("rb") as handle:
                response = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
                    files={"photo": (image_path.name, handle, "image/png")},
                    timeout=60,
                )
            if response.status_code != 200:
                self.logger.log(f"Telegram sendPhoto error {response.status_code}: {response.text[:300]}", "WARN")
            return response.status_code == 200
        except Exception as exc:  # pragma: no cover
            self.logger.log(f"Telegram sendPhoto failed: {exc}", "WARN")
            return False


class Logger:
    def __init__(self):
        self.logs: list[str] = []
        self.source_statuses: list[tuple[str, str]] = []

    def log(self, message: str, level: str = "INFO") -> None:
        icons = {
            "INFO": "ℹ️",
            "SUCCESS": "✅",
            "WARN": "⚠️",
            "ERROR": "❌",
            "STEP": "🔹",
        }
        line = f"{icons.get(level, '•')} {message}"
        self.logs.append(line)
        print(line, flush=True)

    def source_status(self, source: str, status: str) -> None:
        self.source_statuses.append((source, status))


class SourceAdapter:
    name = "source"

    def __init__(self, session: requests.Session, logger: Logger):
        self.session = session
        self.logger = logger

    def fetch(self) -> list[DigestItem]:
        raise NotImplementedError


class ArxivAdapter(SourceAdapter):
    name = "arxiv"

    def __init__(self, session: requests.Session, logger: Logger, feeds: list[dict[str, str]], per_feed_limit: int = 8):
        super().__init__(session, logger)
        self.feeds = feeds
        self.per_feed_limit = per_feed_limit

    def fetch(self) -> list[DigestItem]:
        items: list[DigestItem] = []
        for feed in self.feeds:
            url = feed["url"]
            source_name = feed.get("source") or host_label(url)
            try:
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                parsed = parse_rss_items(response.text, source_name, feed.get("category", ""))
                items.extend(parsed[: self.per_feed_limit])
                self.logger.source_status(source_name, f"ok ({len(parsed[: self.per_feed_limit])} items)")
            except Exception as exc:
                self.logger.source_status(source_name, f"error: {exc}")
                self.logger.log(f"{source_name} fetch failed: {exc}", "WARN")
        return items


class HackerNewsAdapter(SourceAdapter):
    name = "hackernews"

    ENDPOINT = "https://hn.algolia.com/api/v1/search?tags=front_page"

    def fetch(self) -> list[DigestItem]:
        try:
            response = self.session.get(self.ENDPOINT, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            self.logger.source_status("Hacker News", f"error: {exc}")
            self.logger.log(f"Hacker News fetch failed: {exc}", "WARN")
            return []

        hits = data.get("hits") or []
        items: list[DigestItem] = []
        for hit in hits[:30]:
            title = (hit.get("title") or hit.get("story_title") or "").strip()
            if not title:
                continue
            url = (hit.get("url") or hit.get("story_url") or "").strip()
            object_id = hit.get("objectID")
            if not url and object_id:
                url = f"https://news.ycombinator.com/item?id={object_id}"
            metadata = {
                "points": hit.get("points") or 0,
                "comments": hit.get("num_comments") or 0,
                "author": hit.get("author") or "",
            }
            summary = " ".join(
                part
                for part in [
                    strip_html(hit.get("story_text") or ""),
                    f"HN points: {metadata['points']}" if metadata["points"] else "",
                    f"comments: {metadata['comments']}" if metadata["comments"] else "",
                ]
                if part
            )
            items.append(
                DigestItem(
                    source="Hacker News",
                    title=title,
                    url=url,
                    published_at=normalize_datetime(hit.get("created_at") or ""),
                    raw_summary=summary,
                    signals=[
                        f"HN {metadata['points']} points" if metadata["points"] else "",
                        f"{metadata['comments']} comments" if metadata["comments"] else "",
                    ],
                    metadata=metadata,
                )
            )
        self.logger.source_status("Hacker News", f"ok ({len(items)} items)")
        return items


class GitHubTrendingAdapter(SourceAdapter):
    name = "github-trending"

    URL = "https://github.com/trending?since=daily"

    def fetch(self) -> list[DigestItem]:
        try:
            response = self.session.get(self.URL, timeout=30)
            response.raise_for_status()
            items = parse_github_trending(response.text)
            self.logger.source_status("GitHub Trending", f"ok ({len(items)} items)")
            return items
        except Exception as exc:
            self.logger.source_status("GitHub Trending", f"error: {exc}")
            self.logger.log(f"GitHub Trending fetch failed: {exc}", "WARN")
            return []


class OfficialFeedAdapter(SourceAdapter):
    name = "official-feeds"

    def __init__(self, session: requests.Session, logger: Logger, feeds: list[dict[str, str]], per_feed_limit: int = 6):
        super().__init__(session, logger)
        self.feeds = feeds
        self.per_feed_limit = per_feed_limit

    def fetch(self) -> list[DigestItem]:
        items: list[DigestItem] = []
        for feed in self.feeds:
            url = feed.get("url", "").strip()
            if not url:
                continue
            source_name = feed.get("source") or host_label(url)
            category = feed.get("category", "")
            try:
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                parsed = parse_rss_items(response.text, source_name, category)
                items.extend(parsed[: self.per_feed_limit])
                self.logger.source_status(source_name, f"ok ({len(parsed[: self.per_feed_limit])} items)")
            except Exception as exc:
                self.logger.source_status(source_name, f"error: {exc}")
                self.logger.log(f"{source_name} feed fetch failed: {exc}", "WARN")
        return items


class XAdapter(SourceAdapter):
    name = "x"

    def fetch(self) -> list[DigestItem]:
        self.logger.source_status("X Adapter", "skipped (reserved for future external crawler input)")
        return []


class BrowserCrawlerAdapter(SourceAdapter):
    name = "browser-crawler"

    def __init__(self, session: requests.Session, logger: Logger, config_path: Path, timeout_seconds: int = 240):
        super().__init__(session, logger)
        self.config_path = config_path
        self.timeout_seconds = timeout_seconds

    def fetch(self) -> list[DigestItem]:
        if not self.config_path.exists():
            self.logger.source_status("Browser Crawler", f"skipped (config missing: {self.config_path})")
            return []

        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.source_status("Browser Crawler", f"error: invalid config ({exc})")
            self.logger.log(f"Browser crawler config invalid: {exc}", "WARN")
            return []

        targets = [entry for entry in (config.get("targets") or []) if entry.get("enabled", True)]
        if not targets:
            self.logger.source_status("Browser Crawler", "skipped (no enabled targets)")
            return []

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            output_path = Path(handle.name)

        command = [
            "node",
            "scripts/browser_crawler.mjs",
            "--config",
            str(self.config_path),
            "--output",
            str(output_path),
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            raw_items = payload.get("items", payload) if isinstance(payload, dict) else payload
            statuses = payload.get("statuses", []) if isinstance(payload, dict) else []
            items = [digest_item_from_payload(entry) for entry in raw_items]
            items = [item for item in items if item]
            for status in statuses:
                target = normalize_inline_text(str(status.get("target") or "unknown target"))
                state = normalize_inline_text(str(status.get("status") or "unknown"))
                if state == "ok":
                    count = int(status.get("count") or 0)
                    self.logger.source_status(f"Browser:{target}", f"ok ({count} items)")
                else:
                    error = trim_sentence(normalize_inline_text(str(status.get("error") or "unknown error")), 180)
                    self.logger.source_status(f"Browser:{target}", f"error: {error}")
            self.logger.source_status("Browser Crawler", f"ok ({len(items)} items)")
            if result.stderr.strip():
                self.logger.log(f"Browser crawler stderr: {trim_sentence(result.stderr.strip(), 240)}", "INFO")
            return items
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip()
            self.logger.source_status("Browser Crawler", "error: crawler command failed")
            self.logger.log(f"Browser crawler failed: {trim_sentence(stderr, 240)}", "WARN")
            return []
        except subprocess.TimeoutExpired:
            self.logger.source_status("Browser Crawler", "error: timeout")
            self.logger.log("Browser crawler timed out", "WARN")
            return []
        except Exception as exc:
            self.logger.source_status("Browser Crawler", f"error: {exc}")
            self.logger.log(f"Browser crawler parse failed: {exc}", "WARN")
            return []
        finally:
            output_path.unlink(missing_ok=True)


class AISummarizer:
    def __init__(self, logger: Logger):
        self.logger = logger
        self.enabled = parse_bool(os.environ.get("AI_ENABLED", "") or "true")
        self.base_url = os.environ.get("AI_BASE_URL", "").strip()
        self.api_key = os.environ.get("AI_API_KEY", "").strip()
        self.model = os.environ.get("AI_MODEL", "").strip()
        self.timeout = env_int("AI_TIMEOUT_SECONDS", 60)

    def can_use(self) -> bool:
        return self.enabled and bool(self.base_url and self.api_key and self.model)

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.base_url:
            missing.append("AI_BASE_URL")
        if not self.api_key:
            missing.append("AI_API_KEY")
        if not self.model:
            missing.append("AI_MODEL")
        return missing

    def summarize(self, items: list[DigestItem], generated_at: datetime) -> bool:
        if not items:
            return False
        if not self.can_use():
            if not self.enabled:
                self.logger.log("AI_ENABLED=false，已禁用 AI 摘要，改用规则摘要", "WARN")
            else:
                missing = ", ".join(self.missing_fields()) or "unknown"
                self.logger.log(f"AI 摘要配置不完整，缺少: {missing}，改用规则摘要", "WARN")
            fallback_summaries(items)
            return False

        payload_items = [
            {
                "url": item.url,
                "title": item.title,
                "source": item.source,
                "category": CATEGORY_LABELS.get(item.category, item.category or "未分类"),
                "published_at": item.published_at,
                "raw_summary": item.raw_summary[:1200],
                "signals": [signal for signal in item.signals if signal],
            }
            for item in items
        ]
        system_prompt = textwrap.dedent(
            """
            你是一个技术情报分析助手，面向关注 AI、云原生、开发框架和免费部署平台的中文用户。
            只总结有明确价值的技术信号，不要写空泛评价。
            重点关注：
            - AI 模型、基础设施、推理、工具链
            - 云原生工具、平台变化、部署能力
            - 开发框架、运行时、热门工程趋势
            - 免费部署平台、新免费套餐、定价变化、产品发布

            输出必须是严格 JSON：
            {
              "items": [
                {
                  "url": "https://...",
                  "title_zh": "适合做简报小标题的简体中文标题",
                  "summary": "2-4 句简体中文摘要，交代这是什么、做了什么、能力或变化点是什么",
                  "reason": "1-2 句简体中文，说明为什么值得关注、会影响什么",
                  "tags": ["中文标签1", "中文标签2", "英文专有名词也可保留"]
                }
              ]
            }

            额外要求：
            - 默认使用简体中文输出
            - 专有名词、项目名、模型名保留原文
            - title_zh 必须是中文标题，适合直接作为 Markdown 小标题
            - 如果原文标题是英文，需要翻译成自然中文，不要生硬直译
            - summary 需要比普通新闻摘要更具体，不要只写泛泛结论
            - reason 和 summary 尽量互补，避免重复
            """
        ).strip()
        user_prompt = json.dumps(
            {
                "date": generated_at.strftime("%Y-%m-%d"),
                "items": payload_items,
            },
            ensure_ascii=False,
        )
        request_payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            data = self._request_summary(request_payload)
            content = (
                (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
            )
            parsed = extract_json(content)
            by_url = {entry.get("url"): entry for entry in parsed.get("items", []) if entry.get("url")}
            if not by_url:
                raise ValueError("empty AI summary payload")
            for item in items:
                entry = by_url.get(item.url)
                if not entry:
                    continue
                item.chinese_title = normalize_inline_text(entry.get("title_zh") or "")
                item.summary = normalize_inline_text(entry.get("summary") or "")
                item.reason = normalize_inline_text(entry.get("reason") or "")
                item.tags = sanitize_tags(entry.get("tags") or [])
            for item in items:
                if not item.summary or not item.chinese_title:
                    hydrate_fallback(item)
            self.logger.log(f"AI 已完成中文摘要，模型: {self.model}", "SUCCESS")
            return True
        except Exception as exc:
            self.logger.log(f"AI 摘要失败: {exc}", "WARN")
            fallback_summaries(items)
            return False

    def _request_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = build_api_url(self.base_url, "/chat/completions")

        first_payload = dict(payload)
        first_payload["response_format"] = {"type": "json_object"}
        response = requests.post(url, headers=headers, json=first_payload, timeout=self.timeout)
        if response.status_code < 400:
            return response.json()

        self.logger.log(
            f"AI 接口不接受 response_format ({response.status_code})，改为无该参数重试",
            "WARN",
        )
        response = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        response.raise_for_status()
        return response.json()


class DigestRunner:
    def __init__(self, dry_run: bool = False, no_telegram: bool = False):
        self.dry_run = dry_run
        self.logger = Logger()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 ClawCloudDigest/1.0"})
        self.max_items = env_int("DIGEST_MAX_ITEMS", 12)
        self.dedupe_days = env_int("DIGEST_DEDUPE_DAYS", 7)
        self.reports_dir = Path(os.environ.get("DIGEST_REPORTS_DIR", ".reports")).resolve()
        self.output_root = Path(os.environ.get("DIGEST_OUTPUT_ROOT", "reports/digests"))
        self.telegram = Telegram(self.logger, dry_run=dry_run or no_telegram)
        self.summarizer = AISummarizer(self.logger)
        self.timezone = ZoneInfo("Asia/Shanghai") if ZoneInfo else timezone(timedelta(hours=8))
        self.generated_at = datetime.now(timezone.utc).astimezone(self.timezone)

    def run(self) -> int:
        self.logger.log("Collecting source items", "STEP")
        items = self.collect_items()
        self.logger.log(f"Collected {len(items)} raw items", "INFO")

        history = load_history_index(self.reports_dir, self.output_root, self.dedupe_days)
        selected = self.filter_and_rank(items, history)
        self.logger.log(f"Selected {len(selected)} digest items after ranking", "INFO")

        ai_used = self.summarizer.summarize(selected, self.generated_at)
        markdown = render_markdown(
            items=selected,
            generated_at=self.generated_at,
            source_statuses=self.logger.source_statuses,
            ai_model=self.summarizer.model if ai_used else "",
            dedupe_days=self.dedupe_days,
        )
        output_path = self.write_output(markdown)
        self.logger.log(f"Digest written to {output_path}", "SUCCESS")

        if selected:
            self.send_notifications(selected, output_path, ai_used)
        else:
            self.logger.log("No selected items; skipping Telegram send", "WARN")
        return 0

    def collect_items(self) -> list[DigestItem]:
        official_feeds = parse_feed_config(os.environ.get("DIGEST_OFFICIAL_FEEDS", ""))
        browser_config = Path(os.environ.get("DIGEST_BROWSER_TARGETS_FILE", "config/browser_targets.json")).resolve()
        adapters: list[SourceAdapter] = [
            ArxivAdapter(self.session, self.logger, parse_feed_config(os.environ.get("DIGEST_ARXIV_FEEDS", ""), DEFAULT_ARXIV_FEEDS)),
            HackerNewsAdapter(self.session, self.logger),
            GitHubTrendingAdapter(self.session, self.logger),
            OfficialFeedAdapter(self.session, self.logger, official_feeds),
            BrowserCrawlerAdapter(
                self.session,
                self.logger,
                browser_config,
                timeout_seconds=env_int("DIGEST_BROWSER_TIMEOUT_SECONDS", 240),
            ),
            XAdapter(self.session, self.logger),
        ]
        items: list[DigestItem] = []
        for adapter in adapters:
            items.extend(adapter.fetch())
        return items

    def filter_and_rank(self, items: list[DigestItem], history: dict[str, set[str]]) -> list[DigestItem]:
        ranked: list[DigestItem] = []
        for item in items:
            item.category = classify_category(item)
            item.score = score_item(item)
            if not item.category or item.score < 4:
                continue
            if is_recent_duplicate(item, history):
                continue
            ranked.append(item)
        ranked.sort(key=lambda entry: (entry.score, entry.published_at or ""), reverse=True)
        return select_digest_items(ranked, self.max_items)

    def write_output(self, markdown: str) -> Path:
        target = self.report_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        return target

    def report_path(self) -> Path:
        subpath = self.output_root / self.generated_at.strftime("%Y/%m/%d.md")
        return self.reports_dir / subpath

    def send_notifications(self, items: list[DigestItem], markdown_path: Path, ai_used: bool) -> None:
        summary = build_telegram_summary(items, self.generated_at, ai_used)
        sent_text = self.telegram.send(summary)

        # Render markdown to a single long-page image via marknative
        image_path = self._render_preview(markdown_path)
        sent_image = False
        if image_path:
            sent_image = self.telegram.photo(
                image_path,
                caption=f"每日技术简报 {self.generated_at.strftime('%Y-%m-%d')}",
            )

        sent_file = self.telegram.document(
            markdown_path,
            caption=f"每日技术简报 {self.generated_at.strftime('%Y-%m-%d')} (源文件)",
        )
        if sent_text or sent_file or sent_image:
            self.logger.log("Telegram 通知已发送", "SUCCESS")
        elif self.telegram.ok:
            self.logger.log("Telegram 发送失败", "WARN")

    def _render_preview(self, markdown_path: Path) -> Path | None:
        """Render markdown to a single long-page PNG image using marknative."""
        try:
            import subprocess

            render_script = Path(__file__).resolve().parent / "render_markdown.mjs"
            output_path = markdown_path.with_suffix(".png")
            result = subprocess.run(
                ["node", str(render_script), str(markdown_path), str(output_path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0 and result.stdout.strip():
                rendered = Path(result.stdout.strip())
                if rendered.exists():
                    self.logger.log(f"Markdown 已渲染为长图: {rendered}", "INFO")
                    return rendered
            self.logger.log(f"marknative 渲染失败: {result.stderr.strip()}", "WARN")
        except Exception as exc:  # pragma: no cover
            self.logger.log(f"marknative 渲染异常: {exc}", "WARN")
        return None


def parse_bool(value: str) -> bool:
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def host_label(url: str) -> str:
    try:
        return (urlparse(url).hostname or url).replace("www.", "")
    except Exception:
        return url


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_inline_text(unescape(text))


def normalize_text(text: str) -> str:
    text = normalize_inline_text(text).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_inline_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def normalize_datetime(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except Exception:
        return value


def parse_feed_config(raw: str, default: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    if not raw.strip():
        return list(default or [])
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [coerce_feed_entry(entry) for entry in parsed if coerce_feed_entry(entry)]
    except json.JSONDecodeError:
        pass

    entries: list[dict[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append({"url": line})
    return entries


def coerce_feed_entry(entry: Any) -> dict[str, str] | None:
    if isinstance(entry, str):
        return {"url": entry}
    if isinstance(entry, dict) and entry.get("url"):
        return {
            "url": str(entry.get("url", "")).strip(),
            "source": str(entry.get("source", "")).strip(),
            "category": str(entry.get("category", "")).strip(),
        }
    return None


def parse_rss_items(xml_text: str, source_name: str, category_hint: str = "") -> list[DigestItem]:
    root = ET.fromstring(xml_text)
    items: list[DigestItem] = []
    namespaces = {
        "atom": "http://www.w3.org/2005/Atom",
        "dc": "http://purl.org/dc/elements/1.1/",
    }

    channel_items = root.findall(".//item")
    if channel_items:
        for node in channel_items:
            title = normalize_inline_text(node.findtext("title", ""))
            link = normalize_inline_text(node.findtext("link", ""))
            summary = strip_html(node.findtext("description", ""))
            published = normalize_datetime(node.findtext("pubDate", ""))
            if title and link:
                items.append(
                    DigestItem(
                        source=source_name,
                        title=title,
                        url=link,
                        published_at=published,
                        raw_summary=summary,
                        category_hint=category_hint,
                    )
                )
        return items

    atom_entries = root.findall(".//atom:entry", namespaces)
    for node in atom_entries:
        title = normalize_inline_text(node.findtext("atom:title", "", namespaces))
        summary = strip_html(
            node.findtext("atom:summary", "", namespaces)
            or node.findtext("atom:content", "", namespaces)
        )
        link = ""
        for link_node in node.findall("atom:link", namespaces):
            href = link_node.attrib.get("href", "").strip()
            rel = link_node.attrib.get("rel", "alternate")
            if href and rel == "alternate":
                link = href
                break
        published = normalize_datetime(
            node.findtext("atom:updated", "", namespaces)
            or node.findtext("atom:published", "", namespaces)
        )
        if title and link:
            items.append(
                DigestItem(
                    source=source_name,
                    title=title,
                    url=link,
                    published_at=published,
                    raw_summary=summary,
                    category_hint=category_hint,
                )
            )
    return items


def parse_github_trending(html_text: str) -> list[DigestItem]:
    soup = BeautifulSoup(html_text, "html.parser")
    items: list[DigestItem] = []
    for article in soup.select("article.Box-row"):
        link = article.select_one("h2 a")
        if not link:
            continue
        repo_path = normalize_inline_text(link.get("href", "")).strip("/")
        title = repo_path.replace("/", " / ")
        url = f"https://github.com/{repo_path}"
        description = normalize_inline_text(article.select_one("p").get_text(" ", strip=True) if article.select_one("p") else "")
        lang = normalize_inline_text(article.select_one('[itemprop="programmingLanguage"]').get_text(" ", strip=True) if article.select_one('[itemprop="programmingLanguage"]') else "")
        footer = normalize_inline_text(article.get_text(" ", strip=True))
        stars_today_match = re.search(r"(\d[\d,]*)\s+stars today", footer, re.IGNORECASE)
        stars_today = stars_today_match.group(1) if stars_today_match else ""
        signals = []
        if lang:
            signals.append(lang)
        if stars_today:
            signals.append(f"{stars_today} stars today")
        items.append(
            DigestItem(
                source="GitHub Trending",
                title=title,
                url=url,
                raw_summary=description,
                signals=signals,
                metadata={"language": lang, "stars_today": parse_int(stars_today)},
            )
        )
    return items


def parse_int(value: str) -> int:
    try:
        return int(value.replace(",", "").strip())
    except Exception:
        return 0


def digest_item_from_payload(payload: dict[str, Any]) -> DigestItem | None:
    if not isinstance(payload, dict):
        return None
    title = normalize_inline_text(str(payload.get("title") or ""))
    url = normalize_inline_text(str(payload.get("url") or ""))
    if not title or not url:
        return None
    signals = payload.get("signals") or []
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return DigestItem(
        source=normalize_inline_text(str(payload.get("source") or "Browser Crawler")),
        title=title,
        url=url,
        published_at=normalize_datetime(str(payload.get("published_at") or "")),
        raw_summary=normalize_inline_text(str(payload.get("raw_summary") or "")),
        category_hint=normalize_inline_text(str(payload.get("category_hint") or "")),
        signals=[normalize_inline_text(str(item)) for item in signals if normalize_inline_text(str(item))],
        metadata=metadata,
    )


def classify_category(item: DigestItem) -> str:
    if item.category_hint in CATEGORY_LABELS:
        return item.category_hint

    text = " ".join(
        [
            item.title,
            item.raw_summary,
            item.source,
            item.url,
            " ".join(signal for signal in item.signals if signal),
        ]
    ).lower()
    scores = {"ai": 0, "cloud": 0, "framework": 0, "free-platform": 0}

    ai_keywords = [
        "ai",
        "llm",
        "model",
        "agent",
        "inference",
        "training",
        "fine-tuning",
        "rag",
        "embedding",
        "arxiv",
        "openai",
        "anthropic",
        "deepseek",
        "mistral",
        "transformer",
    ]
    cloud_keywords = [
        "kubernetes",
        "k8s",
        "container",
        "docker",
        "serverless",
        "cloud",
        "cluster",
        "platform engineering",
        "service mesh",
        "observability",
        "postgres",
        "edge runtime",
        "infra",
        "wasm",
        "storage",
    ]
    framework_keywords = [
        "framework",
        "runtime",
        "react",
        "next.js",
        "nextjs",
        "vue",
        "svelte",
        "angular",
        "solid",
        "astro",
        "vite",
        "hono",
        "fastapi",
        "bun",
        "deno",
        "remix",
        "nuxt",
    ]
    free_platform_keywords = [
        "free plan",
        "free tier",
        "free credits",
        "free deploy",
        "hosting",
        "deploy",
        "deployment",
        "serverless",
        "platform",
        "render",
        "railway",
        "fly.io",
        "vercel",
        "netlify",
        "cloudflare workers",
        "edge",
        "paas",
        "free",
        "pricing",
    ]

    for word in ai_keywords:
        if word in text:
            scores["ai"] += 2
    for word in cloud_keywords:
        if word in text:
            scores["cloud"] += 2
    for word in framework_keywords:
        if word in text:
            scores["framework"] += 2
    for word in free_platform_keywords:
        if word in text:
            scores["free-platform"] += 2

    if item.source.startswith("arXiv"):
        scores["ai"] += 2
    if item.source == "GitHub Trending":
        scores["framework"] += 1
    if "free plan" in text or "free tier" in text:
        scores["free-platform"] += 3

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else ""


def score_item(item: DigestItem) -> float:
    text = f"{item.title} {item.raw_summary}".lower()
    score = 0.0
    if item.category == "free-platform":
        score += 4
    elif item.category in {"ai", "cloud", "framework"}:
        score += 3

    for keyword in [
        "launch",
        "launched",
        "announced",
        "release",
        "beta",
        "ga",
        "free plan",
        "free tier",
        "deploy",
        "deployment",
        "runtime",
    ]:
        if keyword in text:
            score += 1.5

    score += min(len(item.raw_summary) / 400.0, 1.5)

    points = int(item.metadata.get("points") or 0)
    comments = int(item.metadata.get("comments") or 0)
    stars_today = int(item.metadata.get("stars_today") or 0)
    score += min(points / 100.0, 3.0)
    score += min(comments / 80.0, 2.0)
    score += min(stars_today / 1000.0, 2.0)

    if item.source.startswith("arXiv"):
        score += 1.2
    if any(signal for signal in item.signals if signal):
        score += 0.5
    return round(score, 2)


def select_digest_items(items: list[DigestItem], max_items: int) -> list[DigestItem]:
    if not items:
        return []
    per_category_limit = max(1, max_items // max(len(CATEGORY_ORDER), 1))
    selected: list[DigestItem] = []
    seen_urls: set[str] = set()

    grouped = {category: [item for item in items if item.category == category] for category in CATEGORY_ORDER}
    for category in CATEGORY_ORDER:
        for item in grouped.get(category, [])[:per_category_limit]:
            if item.url in seen_urls:
                continue
            selected.append(item)
            seen_urls.add(item.url)

    if len(selected) < max_items:
        for item in items:
            if item.url in seen_urls:
                continue
            selected.append(item)
            seen_urls.add(item.url)
            if len(selected) >= max_items:
                break
    return selected[:max_items]


def load_history_index(reports_dir: Path, output_root: Path, dedupe_days: int) -> dict[str, set[str]]:
    urls: set[str] = set()
    titles: set[str] = set()
    if not reports_dir.exists():
        return {"urls": urls, "titles": titles}

    today = datetime.now(timezone.utc).date()
    root = reports_dir / output_root
    if not root.exists():
        return {"urls": urls, "titles": titles}

    for offset in range(1, dedupe_days + 1):
        date = today - timedelta(days=offset)
        path = root / date.strftime("%Y/%m/%d.md")
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        urls.update(re.findall(r"<!--\s*digest-item-url:\s*(https?://[^ ]+)\s*-->", text))
        titles.update(normalize_text(title) for title in re.findall(r"^###\s+(.+)$", text, flags=re.MULTILINE))
    return {"urls": urls, "titles": {title for title in titles if title}}


def is_recent_duplicate(item: DigestItem, history: dict[str, set[str]]) -> bool:
    if item.url and item.url in history["urls"]:
        return not has_progress_keyword(item)
    if item.normalized_title and item.normalized_title in history["titles"]:
        return not has_progress_keyword(item)
    return False


def has_progress_keyword(item: DigestItem) -> bool:
    text = f"{item.title} {item.raw_summary}".lower()
    return any(keyword in text for keyword in PROGRESS_KEYWORDS)


def fallback_summaries(items: list[DigestItem]) -> None:
    for item in items:
        hydrate_fallback(item)


def hydrate_fallback(item: DigestItem) -> None:
    if not item.chinese_title:
        item.chinese_title = item.title
    if not item.summary:
        parts = [item.raw_summary]
        if item.signals:
            parts.append("关键信号：" + "；".join(signal for signal in item.signals if signal))
        base = " ".join(part for part in parts if part) or "该来源未提供更多摘要信息。"
        item.summary = trim_sentence(base, 260)
    if not item.reason:
        item.reason = fallback_reason(item)
    if not item.tags:
        item.tags = fallback_tags(item)


def trim_sentence(text: str, limit: int) -> str:
    text = normalize_inline_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def fallback_reason(item: DigestItem) -> str:
    category_reason = {
        "ai": "这条信息可能影响你关注的 AI 模型、工具链或基础设施方向。",
        "cloud": "这条信息与云原生基础设施、部署能力或平台演进相关。",
        "framework": "这条信息可能影响框架、运行时或开发栈选择。",
        "free-platform": "这条信息与免费部署平台、免费套餐或平台机会直接相关。",
    }
    return category_reason.get(item.category, "这条信息与你当前跟踪的主题相关。")


def fallback_tags(item: DigestItem) -> list[str]:
    tags = [CATEGORY_LABELS.get(item.category, item.category or "signal")]
    if item.source == "GitHub Trending":
        language = item.metadata.get("language")
        if language:
            tags.append(str(language))
    if item.source.startswith("arXiv"):
        tags.append("paper")
    return sanitize_tags(tags)


def sanitize_tags(tags: list[Any]) -> list[str]:
    cleaned: list[str] = []
    for tag in tags[:5]:
        text = normalize_inline_text(str(tag))
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def render_markdown(
    items: list[DigestItem],
    generated_at: datetime,
    source_statuses: list[tuple[str, str]],
    ai_model: str,
    dedupe_days: int,
) -> str:
    lines = [
        f"# 每日技术简报 - {generated_at.strftime('%Y-%m-%d')}",
        "",
        f"- 生成时间: `{generated_at.isoformat()}`",
        f"- 去重窗口: `{dedupe_days}` 天",
        f"- AI 摘要: `{ai_model or '规则回退模板'}`",
        "",
        "## 今日重点",
        "",
    ]

    if items:
        for index, item in enumerate(items[:3], start=1):
            lines.append(f"{index}. [{escape_md(item.display_title)}]({item.url})")
            lines.append(f"   - {escape_md(item.reason or item.summary)}")
    else:
        lines.append("今天没有筛选出高信号内容。")

    for category in CATEGORY_ORDER:
        label = CATEGORY_LABELS[category]
        lines.extend(["", f"## {label}", ""])
        category_items = [item for item in items if item.category == category]
        if not category_items:
            lines.append("_今天该栏目暂无入选内容。_")
            continue
        for item in category_items:
            tags = ", ".join(item.tags) if item.tags else CATEGORY_LABELS.get(category, category)
            lines.extend(
                [
                    f"<!-- digest-item-url: {item.url} -->",
                    f"### {escape_md(item.display_title)}",
                    f"- 原文标题: `{escape_md(item.title)}`",
                    f"- 来源: `{item.source}`",
                    f"- 链接: [查看原文]({item.url})",
                    f"- 发布时间: `{item.published_at or 'unknown'}`",
                    f"- 摘要: {escape_md(item.summary)}",
                    f"- 值得关注: {escape_md(item.reason)}",
                    f"- 标签: `{tags}`",
                    "",
                ]
            )

    lines.extend(["## 数据源状态", ""])
    if source_statuses:
        for source, status in source_statuses:
            lines.append(f"- `{source}`: {escape_md(status)}")
    else:
        lines.append("- 本次未记录数据源状态。")

    return "\n".join(lines).strip() + "\n"


def escape_md(text: str) -> str:
    return (text or "").replace("\n", " ").strip()


def build_telegram_summary(items: list[DigestItem], generated_at: datetime, ai_used: bool) -> str:
    top_items = items[:3]
    by_category = {category: len([item for item in items if item.category == category]) for category in CATEGORY_ORDER}
    lines = [
        f"<b>每日技术简报 {generated_at.strftime('%Y-%m-%d')}</b>",
        "",
        f"共整理 <b>{len(items)}</b> 条高信号内容，AI 摘要: <b>{'已启用' if ai_used else '规则回退'}</b>",
        f"AI {by_category['ai']} / 云原生 {by_category['cloud']} / 框架 {by_category['framework']} / 免费平台 {by_category['free-platform']}",
        "",
        "<b>今日 Top 3</b>",
    ]
    for item in top_items:
        lines.append(f"• <a href=\"{item.url}\">{html_escape(item.display_title)}</a>")
    lines.append("")
    lines.append("完整 Markdown 已附带发送。")
    return "\n".join(lines)


def html_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_api_url(base_url: str, path: str) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        return root + path
    return root + "/v1" + path


def extract_json(text: str) -> dict[str, Any]:
    content = (text or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content).strip()
        content = re.sub(r"```$", "", content).strip()
    return json.loads(content)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate daily tech digest")
    parser.add_argument("--dry-run", action="store_true", help="Generate digest without Telegram side effects")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram delivery")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    runner = DigestRunner(dry_run=args.dry_run, no_telegram=args.no_telegram)
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
