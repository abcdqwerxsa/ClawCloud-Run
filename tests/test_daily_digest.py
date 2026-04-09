import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "daily_digest.py"
SPEC = importlib.util.spec_from_file_location("daily_digest", MODULE_PATH)
daily_digest = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = daily_digest
SPEC.loader.exec_module(daily_digest)

DigestItem = daily_digest.DigestItem
build_api_url = daily_digest.build_api_url
classify_category = daily_digest.classify_category
is_recent_duplicate = daily_digest.is_recent_duplicate
load_history_index = daily_digest.load_history_index
parse_feed_config = daily_digest.parse_feed_config
parse_github_trending = daily_digest.parse_github_trending
parse_rss_items = daily_digest.parse_rss_items
render_markdown = daily_digest.render_markdown
score_item = daily_digest.score_item


def test_parse_rss_items_extracts_basic_fields():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Example Feed</title>
        <item>
          <title>Open source inference runtime ships new release</title>
          <link>https://example.com/posts/runtime</link>
          <description><![CDATA[<p>Targets low-cost AI deployment.</p>]]></description>
          <pubDate>Tue, 08 Apr 2026 01:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """

    items = parse_rss_items(xml, "Example Feed", "ai")

    assert len(items) == 1
    assert items[0].source == "Example Feed"
    assert items[0].title == "Open source inference runtime ships new release"
    assert items[0].url == "https://example.com/posts/runtime"
    assert "low-cost AI deployment" in items[0].raw_summary
    assert items[0].category_hint == "ai"
    assert items[0].published_at.startswith("2026-04-08")


def test_parse_github_trending_extracts_repo_and_stars():
    html = """
    <article class="Box-row">
      <h2><a href="/oven-sh/bun"> oven-sh / bun </a></h2>
      <p>Fast JavaScript runtime and toolkit.</p>
      <div>
        <span itemprop="programmingLanguage">TypeScript</span>
        <span>1,234 stars today</span>
      </div>
    </article>
    """

    items = parse_github_trending(html)

    assert len(items) == 1
    assert items[0].title == "oven-sh / bun"
    assert items[0].url == "https://github.com/oven-sh/bun"
    assert items[0].metadata["language"] == "TypeScript"
    assert items[0].metadata["stars_today"] == 1234


def test_parse_feed_config_supports_json_and_lines():
    json_config = '[{"url":"https://a.example/rss","source":"A","category":"free-platform"}]'
    line_config = "https://b.example/rss\n# comment\nhttps://c.example/rss\n"

    parsed_json = parse_feed_config(json_config)
    parsed_lines = parse_feed_config(line_config)

    assert parsed_json == [{"url": "https://a.example/rss", "source": "A", "category": "free-platform"}]
    assert parsed_lines == [{"url": "https://b.example/rss"}, {"url": "https://c.example/rss"}]


def test_history_index_detects_recent_duplicates(tmp_path: Path):
    reports_dir = tmp_path
    digest_path = reports_dir / "reports" / "digests" / "2026" / "04" / "08.md"
    digest_path.parent.mkdir(parents=True)
    digest_path.write_text(
        "\n".join(
            [
                "# Daily Tech Digest - 2026-04-08",
                "<!-- digest-item-url: https://example.com/bun-cloud -->",
                "### Bun launches free edge platform",
            ]
        ),
        encoding="utf-8",
    )

    item = DigestItem(
        source="GitHub Trending",
        title="Bun launches free edge platform",
        url="https://example.com/bun-cloud",
        raw_summary="A new free deployment option.",
        category="free-platform",
    )

    history = load_history_index(reports_dir, Path("reports/digests"), dedupe_days=7)
    assert is_recent_duplicate(item, history) is False

    item.raw_summary = "A deployment platform story without a new milestone."
    item.title = "Bun free edge platform"
    history = load_history_index(reports_dir, Path("reports/digests"), dedupe_days=7)
    assert is_recent_duplicate(item, history) is True


def test_classification_and_scoring_prioritize_free_platform_signal():
    item = DigestItem(
        source="Official Feed",
        title="Void Cloud launches a free edge deploy plan for Bun apps",
        url="https://void.example/blog/free-plan",
        raw_summary="New free tier for deploying Bun edge workloads with AI workers.",
    )

    item.category = classify_category(item)
    item.score = score_item(item)

    assert item.category == "free-platform"
    assert item.score >= 6


def test_render_markdown_contains_fixed_sections():
    item = DigestItem(
        source="Hacker News",
        title="Interesting infra post",
        url="https://example.com/post",
        published_at="2026-04-09T01:00:00+00:00",
        raw_summary="A concise summary.",
        category="cloud",
        summary="A concise summary.",
        reason="Relevant to platform and deployment changes.",
        tags=["cloud", "deploy"],
    )

    markdown = render_markdown(
        items=[item],
        generated_at=datetime(2026, 4, 9, 9, 0, tzinfo=timezone.utc),
        source_statuses=[("Hacker News", "ok (1 items)")],
        ai_model="test-model",
        dedupe_days=7,
    )

    assert "# Daily Tech Digest - 2026-04-09" in markdown
    assert "## AI" in markdown
    assert "## 云原生" in markdown
    assert "## 框架" in markdown
    assert "## 免费平台" in markdown
    assert "<!-- digest-item-url: https://example.com/post -->" in markdown


def test_build_api_url_adds_v1_once():
    assert build_api_url("https://api.example.com", "/chat/completions") == "https://api.example.com/v1/chat/completions"
    assert build_api_url("https://api.example.com/v1", "/chat/completions") == "https://api.example.com/v1/chat/completions"
