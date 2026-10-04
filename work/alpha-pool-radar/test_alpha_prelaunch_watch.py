import importlib.util
import sys
from pathlib import Path


BASE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("alpha_prelaunch_watch", BASE / "alpha_prelaunch_watch.py")
watch = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)


def test_parse_prelaunch_post_without_contract():
    html = """
    <div class="tgme_widget_message" data-post="demo/1">
      <div class="tgme_widget_message_text">
        MoonBiscuit launch soon on Four.meme<br>
        whitelist opens tonight, docs and website ready<br>
        CA soon
      </div>
      <time datetime="2026-09-07T04:00:00+00:00"></time>
    </div></div>
    """

    rows = watch.parse_telegram_preview("demo", html)

    assert len(rows) == 1
    assert rows[0].project == "MoonBiscuit"
    assert rows[0].score >= 70
    assert rows[0].stage == "priority_prelaunch"
    assert rows[0].has_contract is False
    assert "launch_soon" in rows[0].matched_signals
    assert "launchpad" in rows[0].matched_signals


def test_contract_posts_are_routed_to_meme_radar():
    html = """
    <div class="tgme_widget_message" data-post="demo/2">
      <div class="tgme_widget_message_text">
        $DOG launch soon<br>
        CA: 0x1111111111111111111111111111111111111111
      </div>
    </div></div>
    """

    rows = watch.parse_telegram_preview("demo", html)

    assert len(rows) == 1
    assert rows[0].has_contract is True
    assert rows[0].stage == "has_contract_route_to_meme_radar"
