import importlib.util
import sys
from pathlib import Path


BASE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("alpha_telegram_call_backtest", BASE / "alpha_telegram_call_backtest.py")
callbt = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = callbt
SPEC.loader.exec_module(callbt)


def test_parse_telegram_page_extracts_evm_ca_and_market_cap():
    html = """
    <div class="tgme_widget_message" data-post="Mrbigbagcalls/13659">
      <div class="tgme_widget_message_text">
        (BNB) $BIP444<br/>
        MC: 97,602<br/>
        CA: <code>0xad0F63E27F4166d56Bc9EEC32875D68Ad5154444</code>
      </div>
      <time datetime="2026-09-07T05:49:00+00:00"></time>
    </div></div>
    """

    rows = callbt.parse_telegram_page("Mrbigbagcalls", html)

    assert len(rows) == 1
    assert rows[0].address == "0xad0F63E27F4166d56Bc9EEC32875D68Ad5154444"
    assert rows[0].chain_hint == "bsc"
    assert rows[0].symbol_hint == "BIP444"
    assert rows[0].entry_market_cap == 97_602
    assert rows[0].post_url == "https://t.me/Mrbigbagcalls/13659"


def test_parse_telegram_page_extracts_sol_gmgn_link_and_sub_mcap():
    html = """
    <div class="tgme_widget_message" data-post="inside_calls/2265">
      <div class="tgme_widget_message_text">
        bought some $SQUID here on #Solana sub $2M MC.<br/>
        https://gmgn.ai/sol/token/6sbSz8r8sqap2DERkxvDLxAHBWpLiD5LnUfiW3wQpump
      </div>
      <time datetime="2026-09-07T01:08:00+00:00"></time>
    </div></div>
    """

    rows = callbt.parse_telegram_page("inside_calls", html)

    assert len(rows) == 1
    assert rows[0].address == "6sbSz8r8sqap2DERkxvDLxAHBWpLiD5LnUfiW3wQpump"
    assert rows[0].chain_hint == "solana"
    assert rows[0].symbol_hint == "SQUID"
    assert rows[0].entry_market_cap == 2_000_000


def test_multi_ca_digest_uses_address_local_market_cap():
    html = """
    <div class="tgme_widget_message" data-post="ogamdopamine/422518">
      <div class="tgme_widget_message_text">
        SOLANA · 2<br/>
        UBER<br/>
        HHig4peAgZqdAV17XuuAdojYSYLhsctjTEvT2kq9FkNA<br/>
        · MC $2.6M · LIQ $19.8K<br/>
        ULCAT<br/>
        Y8kyGRvobqkptmXPH2PLepA9cgYm7tsdeGuikRrpump<br/>
        · MC $143.7K · LIQ $33.3K<br/>
      </div>
      <time datetime="2026-09-07T01:08:00+00:00"></time>
    </div></div>
    """

    rows = callbt.parse_telegram_page("ogamdopamine", html)

    assert len(rows) == 2
    assert rows[0].symbol_hint == "UBER"
    assert rows[0].entry_market_cap == 2_600_000
    assert rows[1].symbol_hint == "ULCAT"
    assert rows[1].entry_market_cap == 143_700
