import importlib.util
import json
import sys
from pathlib import Path


BASE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("alpha_noxa_telegram_export", BASE / "alpha_noxa_telegram_export.py")
noxa = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = noxa
SPEC.loader.exec_module(noxa)


def test_parse_public_telegram_preview_extracts_noxa_launch_rows():
    html = """
    <div class="tgme_widget_message" data-post="memmememjk/47200">
      <div class="tgme_widget_message_text">
        AI信号 (RBH) 推荐<br>
        <b>anatuly yekovenko</b> ($TULY)<br>
        CA: <code>0x115852de955fae83f084313233af68a42fdfc618</code><br>
        信号时间: 16:33:22 (12秒前) | 币龄: 1分钟内<br>
        MC: $45,493 | 流动性: $23,085<br>
        Holders: 97 | 1h涨幅: +877% | 1h成交: $22,128<br>
        聪明钱包: 3个买入 (SW-42ad, SW-8045, SW-4900)<br>
        <a href="https://fun.noxa.fi/robinhood/token/0x115852de955fae83f084313233af68a42fdfc618">NoxaFun</a>
        <a href="https://dexscreener.com/robinhood/0x115852de955fae83f084313233af68a42fdfc618">K线</a>
      </div>
      <time datetime="2026-09-05T08:33:34+00:00"></time>
    </div>
    """

    rows = noxa.parse_public_preview(html)

    assert len(rows) == 1
    row = rows[0]
    assert row["chain"] == "robinhood"
    assert row["address"] == "0x115852de955fae83f084313233af68a42fdfc618"
    assert row["symbol"] == "TULY"
    assert row["name"] == "anatuly yekovenko"
    assert row["source_family"] == "noxa_launchpad"
    assert row["launchpad_platform"] == "Noxa"
    assert row["marketCap"] == 45493
    assert row["liquidity"] == 23085
    assert row["holders"] == 97
    assert row["priceChange1h"] == 877
    assert row["volume_1h"] == 22128
    assert row["smart_money"] == 3
    assert row["signal_action"] == "推荐"
    assert row["telegram_post_id"] == "memmememjk/47200"
    assert row["observed_at"] == "2026-09-05T08:33:34+00:00"


def test_parse_public_preview_keeps_each_messages_post_id_and_timestamp():
    html = """
    <div class="tgme_widget_message_wrap js-widget_message_wrap">
      <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="memmememjk/52467">
        <div class="tgme_widget_message_text js-message_text" dir="auto">
          AI信号 (RBH) 推荐<br>
          First Token ($FIRST)<br>
          CA: 0x1111111111111111111111111111111111111111<br>
          MC: $45,493 | 流动性: $23,085<br>
          <a href="https://fun.noxa.fi/robinhood/token/0x1111111111111111111111111111111111111111">NoxaFun</a>
        </div>
        <div class="tgme_widget_message_footer compact js-message_footer">
          <time datetime="2026-09-12T14:47:00+00:00"></time>
        </div>
      </div>
    </div>
    <div class="tgme_widget_message_wrap js-widget_message_wrap">
      <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="memmememjk/52468">
        <div class="tgme_widget_message_text js-message_text" dir="auto">
          AI信号 (RBH) 推荐<br>
          Second Token ($SECOND)<br>
          CA: 0x2222222222222222222222222222222222222222<br>
          MC: $55,493 | 流动性: $33,085<br>
          <a href="https://fun.noxa.fi/robinhood/token/0x2222222222222222222222222222222222222222">NoxaFun</a>
        </div>
        <div class="tgme_widget_message_footer compact js-message_footer">
          <time datetime="2026-09-12T14:48:00+00:00"></time>
        </div>
      </div>
    </div>
    """

    rows = noxa.parse_public_preview(html)

    assert [row["telegram_post_id"] for row in rows] == [
        "memmememjk/52467",
        "memmememjk/52468",
    ]
    assert [row["observed_at"] for row in rows] == [
        "2026-09-12T14:47:00+00:00",
        "2026-09-12T14:48:00+00:00",
    ]


def test_build_payload_writes_source_contract(tmp_path):
    rows = [
        {
            "chain": "robinhood",
            "address": "0x115852de955fae83f084313233af68a42fdfc618",
            "symbol": "TULY",
        }
    ]
    out = tmp_path / "noxa-launches.json"

    payload = noxa.write_payload(rows, out)

    assert payload["source"] == "noxa_launchpad"
    assert payload["chain"] == "robinhood"
    assert payload["data"] == rows
    assert json.loads(out.read_text(encoding="utf-8"))["data"][0]["symbol"] == "TULY"
