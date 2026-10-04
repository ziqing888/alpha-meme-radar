import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("alpha_radar_pipeline.py")
SPEC = importlib.util.spec_from_file_location("alpha_radar_pipeline", MODULE_PATH)
pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = pipeline
SPEC.loader.exec_module(pipeline)


def test_default_pipeline_config_keeps_refresh_knobs_in_one_place():
    config = pipeline.default_pipeline_config()

    assert config.alpha_limit == 120
    assert config.top == 120
    assert config.meme_top == 40
    assert config.dealer_top == 120
    assert config.holder_top_tokens == 30
    assert config.risk_top_tokens == 15
    assert config.gold_watch_confirmations == 3
    assert config.site_url == "https://vercel-site-eta-blush.vercel.app"


def test_report_command_is_built_from_pipeline_config(tmp_path):
    config = pipeline.default_pipeline_config()
    command = pipeline.report_command("python", tmp_path / "scripts", tmp_path / "outputs", config)

    assert command[0] == "python"
    assert command[1].endswith("alpha_radar_report.py")
    assert command[command.index("--holder-top-tokens") + 1] == "30"
    assert command[command.index("--risk-top-tokens") + 1] == "15"
    assert command[command.index("--gold-watch-confirmations") + 1] == "3"
    assert command[command.index("--top") + 1] == "120"
    assert command[command.index("--dealer-top") + 1] == "120"
    assert "--holders-enable" in command
    assert "--risk-enable" in command


def test_pipeline_step_names_are_chinese():
    assert pipeline.STEP_REFRESH_DATA == "\u5237\u65b0\u96f7\u8fbe\u6570\u636e"
    assert pipeline.STEP_BUILD_SITE == "\u751f\u6210\u4e2d\u6587\u7f51\u9875"
    assert pipeline.STEP_DEPLOY_VERCEL == "\u90e8\u7f72\u5230 Vercel"
