import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("alpha_tweet_sources.py")
SPEC = importlib.util.spec_from_file_location("alpha_tweet_sources", MODULE_PATH)
tweet_sources = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = tweet_sources
SPEC.loader.exec_module(tweet_sources)


def test_parse_rss_feed_normalizes_account_text_and_time():
    xml = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>CZ: Happy orange cat builders. 4</title>
        <link>https://x.com/cz_binance/status/1</link>
        <pubDate>Thu, 20 Aug 2026 12:00:00 GMT</pubDate>
      </item>
    </channel></rss>
    """

    rows = tweet_sources.parse_rss_feed(xml, default_account="cz_binance")

    assert rows == [
        {
            "account": "cz_binance",
            "text": "CZ: Happy orange cat builders. 4",
            "created_at": "Thu, 20 Aug 2026 12:00:00 GMT",
            "url": "https://x.com/cz_binance/status/1",
            "source": "rss",
        }
    ]


def test_parse_json_payload_accepts_x_api_shape():
    payload = {
        "data": [
            {"id": "1", "text": "雪王来了，BNB community is fun", "created_at": "2026-08-20T12:00:00Z"}
        ],
        "includes": {"users": [{"id": "u1", "username": "heyibinance"}]},
    }

    rows = tweet_sources.parse_json_payload(payload, default_account="binance")

    assert rows[0]["account"] == "heyibinance"
    assert rows[0]["text"] == "雪王来了，BNB community is fun"
    assert rows[0]["url"] == "https://x.com/heyibinance/status/1"


def test_collect_tweets_merges_local_and_url_sources(tmp_path):
    local_file = tmp_path / "tweets.json"
    local_file.write_text(
        json.dumps(
            [
                {
                    "account": "binance",
                    "text": "AI agent season",
                    "created_at": "2026-08-20T12:00:00Z",
                    "url": "https://x.com/binance/status/1",
                }
            ]
        ),
        encoding="utf-8",
    )

    def fake_fetch(url, timeout=8, headers=None):
        assert url == "https://example.com/cz.rss"
        return """<rss><channel><item><title>Orange cat 4</title><link>https://x.com/cz_binance/status/2</link></item></channel></rss>"""

    result = tweet_sources.collect_tweets(
        local_files=[local_file],
        source_urls=["https://example.com/cz.rss"],
        http_text=fake_fetch,
    )

    assert result["ok"] is True
    assert len(result["tweets"]) == 2
    assert result["tweet_count"] == 2
    assert {row["account"] for row in result["tweets"]} == {"binance", "cz_binance"}
    assert result["errors"] == []


def test_collect_tweets_accepts_browser_command_json_output():
    def fake_command(command, timeout=20):
        assert command == "bb-browser fetch cz"
        return json.dumps(
            {
                "data": [
                    {
                        "id": "42",
                        "text": "CZ says orange cat is cooking",
                        "created_at": "2026-08-20T12:00:00Z",
                    }
                ],
                "includes": {"users": [{"username": "cz_binance"}]},
            }
        )

    result = tweet_sources.collect_tweets(
        browser_commands=["bb-browser fetch cz"],
        command_runner=fake_command,
    )

    assert result["ok"] is True
    assert result["tweet_count"] == 1
    assert result["tweets"][0]["account"] == "cz_binance"
    assert result["tweets"][0]["text"] == "CZ says orange cat is cooking"
    assert result["tweets"][0]["url"] == "https://x.com/cz_binance/status/42"


def test_collect_tweets_keeps_other_sources_when_browser_command_fails(tmp_path):
    local_file = tmp_path / "tweets.json"
    local_file.write_text(
        json.dumps([{"account": "binance", "text": "AI season", "url": "https://x.com/binance/status/1"}]),
        encoding="utf-8",
    )

    def failing_command(command, timeout=20):
        raise RuntimeError("browser not ready")

    result = tweet_sources.collect_tweets(
        local_files=[local_file],
        browser_commands=["bb-browser fetch x"],
        command_runner=failing_command,
    )

    assert result["ok"] is True
    assert result["tweet_count"] == 1
    assert result["tweets"][0]["account"] == "binance"
    assert result["errors"] == ["browser command: browser not ready"]


def test_write_tweets_file_outputs_report_shape(tmp_path):
    output = tmp_path / "alpha-narrative-tweets.json"
    result = {"ok": True, "tweets": [{"account": "binance", "text": "cat", "url": "u"}], "errors": []}

    tweet_sources.write_tweets_file(output, result)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["tweets"][0]["text"] == "cat"
