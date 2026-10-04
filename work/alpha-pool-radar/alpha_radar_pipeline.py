#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


STEP_REFRESH_DATA = "\u5237\u65b0\u96f7\u8fbe\u6570\u636e"
STEP_BUILD_SITE = "\u751f\u6210\u4e2d\u6587\u7f51\u9875"
STEP_DEPLOY_VERCEL = "\u90e8\u7f72\u5230 Vercel"

ERROR_REFRESH_DATA = "\u5237\u65b0\u96f7\u8fbe\u6570\u636e\u5931\u8d25"
ERROR_BUILD_SITE = "\u751f\u6210\u4e2d\u6587\u7f51\u9875\u5931\u8d25"
ERROR_DEPLOY_VERCEL = "\u90e8\u7f72\u5230 Vercel \u5931\u8d25"
ERROR_SITE_INCOMPLETE = "\u7f51\u9875\u6587\u4ef6\u4e0d\u5b8c\u6574"


@dataclass(frozen=True)
class PipelineConfig:
    alpha_limit: int
    top: int
    meme_top: int
    dealer_top: int
    holder_top_tokens: int
    risk_top_tokens: int
    gold_watch_confirmations: int
    site_url: str


def default_pipeline_config() -> PipelineConfig:
    return PipelineConfig(
        alpha_limit=120,
        top=120,
        meme_top=40,
        dealer_top=120,
        holder_top_tokens=30,
        risk_top_tokens=15,
        gold_watch_confirmations=3,
        site_url="https://vercel-site-eta-blush.vercel.app",
    )


def report_command(python_exe: str, script_dir: Path, out_dir: Path, config: PipelineConfig) -> list[str]:
    return [
        python_exe,
        str(script_dir / "alpha_radar_report.py"),
        "--out-dir",
        str(out_dir),
        "--alpha-limit",
        str(config.alpha_limit),
        "--top",
        str(config.top),
        "--meme-top",
        str(config.meme_top),
        "--dealer-top",
        str(config.dealer_top),
        "--holders-enable",
        "--holder-provider",
        "auto",
        "--holder-top-tokens",
        str(config.holder_top_tokens),
        "--risk-enable",
        "--risk-top-tokens",
        str(config.risk_top_tokens),
        "--gold-watch-confirmations",
        str(config.gold_watch_confirmations),
    ]


def build_command(python_exe: str, script_dir: Path, report_path: Path, site_dir: Path) -> list[str]:
    return [
        python_exe,
        str(script_dir / "build_vercel_site.py"),
        "--report",
        str(report_path),
        "--out-dir",
        str(site_dir),
    ]


def deploy_command(npx: str) -> list[str]:
    return [npx, "--yes", "vercel@latest", "--yes", "--prod"]
