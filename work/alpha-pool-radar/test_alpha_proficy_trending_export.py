import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path


BASE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("alpha_proficy_trending_export", BASE / "alpha_proficy_trending_export.py")
proficy = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = proficy
SPEC.loader.exec_module(proficy)


def test_parse_trending_page_extracts_public_rows():
    html = """
    <table>
      <tbody>
        <tr class="group">
          <td><span>1</span></td>
          <td>
            <a href="https://t.me/ProficyPriceBot?start=0x39dBED3a2bd333467115dE45665cC57F813C4571">
              <span><span class="block truncate text-[13.5px] font-semibold tracking-tight">PONS</span>
              <span class="block truncate text-[11px] text-[var(--color-faint)]">Pons</span></span>
            </a>
          </td>
          <td><span><img src="/assets/chains/robinhood.png"/>Robinhood</span></td>
          <td>$654.5M</td>
          <td><span>+3.7%</span></td>
          <td><span>+25%</span></td>
          <td>$8.03M</td>
          <td>$7.73M</td>
          <td><span>2mo</span></td>
        </tr>
        <tr class="group">
          <td>2</td>
          <td>
            <a href="https://t.me/ProficyPriceBot?start=0xd85c0c5f5a81A1527Bb90f225cEacFbCB590C7D9">
              <span class="font-semibold">OZZY</span><span class="text-[11px]">The Joker</span>
            </a>
          </td>
          <td>BNB Chain</td><td>$8.01M</td><td>-1.2%</td><td>+11%</td><td>$1.2M</td><td>$930K</td><td>3h</td>
        </tr>
      </tbody>
    </table>
    """

    rows = proficy.parse_trending_page(html)

    assert len(rows) == 2
    assert rows[0]["chain"] == "robinhood"
    assert rows[0]["address"] == "0x39dBED3a2bd333467115dE45665cC57F813C4571"
    assert rows[0]["symbol"] == "PONS"
    assert rows[0]["name"] == "Pons"
    assert rows[0]["marketCap"] == 654_500_000
    assert rows[0]["liquidity"] == 7_730_000
    assert rows[0]["volume24h"] == 8_030_000
    assert rows[0]["priceChange1h"] == 3.7
    assert rows[0]["priceChange24h"] == 25
    assert rows[0]["pair_age_hours"] == 1440
    assert rows[0]["source_family"] == "proficy_trending"
    assert rows[1]["chain"] == "bsc"
    assert rows[1]["priceChange1h"] == -1.2
    assert rows[1]["pair_age_hours"] == 3


def test_parse_trending_page_falls_back_to_official_telegram_preview():
    html = """
    <div class="tgme_widget_message_text js-message_text" dir="auto">
      1. <a href="https://t.me/ProficyPriceBot?start=0x35a79120e07bAE083045d44B8349c5d47f37AC13"><b>Compound Capital (CC)</b> robinhood</a> 284%<br/>
      <b>MC:</b> 205K | <b>Liq:</b> 42K | <b>Vol:</b> 391K | <b>Age:</b> 1 hour<br/>
    </div>
    """

    rows = proficy.parse_trending_page(html)

    assert len(rows) == 1
    assert rows[0]["chain"] == "robinhood"
    assert rows[0]["address"] == "0x35a79120e07bAE083045d44B8349c5d47f37AC13"
    assert rows[0]["symbol"] == "CC"
    assert rows[0]["name"] == "Compound Capital"
    assert rows[0]["marketCap"] == 205_000
    assert rows[0]["liquidity"] == 42_000
    assert rows[0]["volume24h"] == 391_000
    assert rows[0]["priceChange24h"] == 284
    assert rows[0]["pair_age_hours"] == 1
    assert rows[0]["source_origin"] == "proficy_public_telegram"


def test_fetch_text_uses_official_telegram_preview_when_site_blocks_local_request(monkeypatch):
    calls = []
    telegram_html = b'<div class="tgme_widget_message_text">preview</div>'

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if request.full_url == proficy.DEFAULT_URL:
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
        return io.BytesIO(telegram_html)

    monkeypatch.setattr(proficy.urllib.request, "urlopen", fake_urlopen)

    html = proficy.fetch_text(proficy.DEFAULT_URL, 8)

    assert html == telegram_html.decode()
    assert calls == [proficy.DEFAULT_URL, proficy.TELEGRAM_PREVIEW_URL]


def test_build_payload_writes_proficy_source_contract(tmp_path):
    rows = [{"chain": "robinhood", "address": "0xabc", "symbol": "PONS"}]
    out = tmp_path / "proficy-trending.json"

    payload = proficy.write_payload(rows, out)

    assert payload["source"] == "proficy_trending"
    assert payload["origin"] == "public_trending_page"
    assert payload["data"] == rows
    assert json.loads(out.read_text(encoding="utf-8"))["data"][0]["symbol"] == "PONS"
