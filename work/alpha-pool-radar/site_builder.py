#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


DEFAULT_REPORT = Path("outputs/alpha-radar-report-latest.json")
DEFAULT_OUT_DIR = Path("outputs/vercel-site")
ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"


def hidden_subprocess_kwargs() -> dict[str, object]:
    if os.name != "nt" or not hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Alpha 妖币雷达</title>
  <meta name="description" content="Alpha、Meme、庄家雷达综合看板。" />
  <style>
    :root { color-scheme: dark; --bg: #05080d; --panel: #101720; --panel2: #0a1018; --surface-glass: rgba(13, 20, 31, .72); --line: rgba(122, 143, 166, .18); --line-strong: rgba(122, 143, 166, .32); --text: #edf6ff; --muted: #91a0b4; --accent: #14f195; --blue: #45c7ff; --amber: #ffcf66; --red: #ff5c7a; --shadow: 0 24px 90px rgba(0, 0, 0, .42); }
    * { box-sizing: border-box; }
    body { margin: 0; background: radial-gradient(circle at 16% -8%, rgba(20, 241, 149, .22), transparent 34%), radial-gradient(circle at 88% 4%, rgba(69, 199, 255, .16), transparent 30%), linear-gradient(180deg, #08111d 0%, #05080d 42%, #060a10 100%); color: var(--text); font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; }
    .market-site-shell { min-height: 100vh; position: relative; overflow-x: hidden; }
    .market-site-shell::before { content: ""; position: fixed; inset: 0; pointer-events: none; z-index: -1; background-image: linear-gradient(rgba(255, 255, 255, .035) 1px, transparent 1px), linear-gradient(90deg, rgba(255, 255, 255, .03) 1px, transparent 1px); background-size: 46px 46px; mask-image: linear-gradient(180deg, rgba(0,0,0,.9), rgba(0,0,0,.15)); }
    .market-nav { position: sticky; top: 0; z-index: 30; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: center; padding: 14px 24px; border-bottom: 1px solid var(--line); background: rgba(5, 8, 13, .72); backdrop-filter: blur(22px) saturate(140%); box-shadow: 0 10px 34px rgba(0, 0, 0, .26); }
    .nav-brand { display: inline-flex; align-items: center; gap: 11px; width: fit-content; color: var(--text); }
    .brand-mark { display: inline-grid; place-items: center; width: 38px; height: 38px; border-radius: 10px; color: #04100b; background: linear-gradient(145deg, #8cffce, var(--accent)); font-weight: 900; box-shadow: 0 0 28px rgba(20, 241, 149, .36); }
    .nav-brand span { display: block; color: var(--muted); font-size: 12px; }
    .nav-brand strong { display: block; font-size: 17px; line-height: 1.1; }
    .nav-links { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; justify-content: flex-end; }
    .nav-links a { min-height: 36px; padding: 9px 12px; border: 1px solid transparent; border-radius: 999px; color: var(--muted); font-size: 13px; }
    .nav-links a:hover, .nav-links a.active { color: var(--text); border-color: rgba(20, 241, 149, .36); background: linear-gradient(180deg, rgba(20, 241, 149, .13), rgba(20, 241, 149, .04)); }
    .sidebar { position: sticky; top: 0; height: 100vh; padding: 20px 16px; border-right: 1px solid var(--line); background: rgba(8, 16, 22, .92); }
    .brand { display: grid; gap: 4px; padding: 10px 10px 18px; border-bottom: 1px solid var(--line); }
    .brand strong { font-size: 18px; }
    .brand span { color: var(--muted); font-size: 12px; }
    .dashboard-nav { display: grid; gap: 7px; padding: 18px 4px; }
    .nav-item { display: flex; align-items: center; gap: 9px; min-height: 38px; padding: 0 10px; border-radius: 8px; color: var(--muted); font-size: 14px; }
    .nav-item.active { background: #132532; color: var(--accent); }
    .sidebar-note { margin: 14px 4px 0; padding: 12px; border: 1px solid var(--line); border-radius: 8px; color: var(--muted); font-size: 12px; line-height: 1.5; background: var(--panel2); }
    .shell { max-width: 1600px; width: 100%; margin: 0 auto; padding: 22px 24px 48px; }
    .topbar { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: center; margin-bottom: 16px; padding: 14px 16px; border: 1px solid var(--line); border-radius: 14px; background: var(--surface-glass); backdrop-filter: blur(18px) saturate(135%); box-shadow: 0 12px 44px rgba(0, 0, 0, .22); }
    .hero-board { display: grid; grid-template-columns: minmax(0, 1.24fr) minmax(260px, .76fr); gap: 12px; align-items: stretch; margin-bottom: 0; }
    .premium-hero { filter: drop-shadow(0 26px 70px rgba(0, 0, 0, .24)); }
    .hero-board .terminal-header { margin-bottom: 0; min-height: 128px; }
    .hero-metrics { display: grid; grid-template-columns: 1fr; gap: 10px; }
    .hero-metric { display: grid; gap: 6px; align-content: center; min-height: 50px; padding: 11px 13px; border: 1px solid var(--line); border-radius: 14px; background: linear-gradient(145deg, rgba(17, 28, 43, .88), rgba(8, 13, 21, .92)); box-shadow: inset 0 1px 0 rgba(255, 255, 255, .06), 0 18px 50px rgba(0, 0, 0, .22); }
    .hero-metric span { color: var(--muted); font-size: 12px; }
    .hero-metric strong { font-size: 15px; }
    .visual-grade { color: var(--amber); border-color: rgba(255, 207, 102, .45); background: rgba(255, 207, 102, .08); }
    .ticker-strip { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 16px; padding: 10px; border: 1px solid var(--line); border-radius: 14px; background: rgba(5, 8, 13, .54); backdrop-filter: blur(14px); }
    .ticker-chip { display: inline-flex; align-items: center; gap: 8px; min-height: 34px; padding: 0 12px; border: 1px solid rgba(122, 143, 166, .2); border-radius: 999px; background: linear-gradient(180deg, rgba(16, 27, 41, .92), rgba(9, 14, 22, .92)); color: var(--text); font-size: 13px; box-shadow: inset 0 1px 0 rgba(255, 255, 255, .05); }
    .ticker-chip span { color: var(--muted); }
    .ticker-chip strong { color: var(--accent); font-weight: 800; }
    .market-tabs { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 16px; }
    .market-tabs a { display: inline-flex; align-items: center; min-height: 38px; padding: 0 14px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); background: rgba(12, 21, 29, .72); font-size: 13px; box-shadow: inset 0 1px 0 rgba(255, 255, 255, .04); }
    .market-tabs a.active { color: #06110d; border-color: rgba(20, 241, 149, .74); background: linear-gradient(145deg, #83ffd0, var(--accent)); font-weight: 800; }
    .screener-layout { display: block; }
    .terminal-header { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(300px, .95fr); gap: 14px; align-items: stretch; margin-bottom: 16px; padding: 16px; border: 1px solid rgba(20, 241, 149, .28); border-radius: 18px; background: linear-gradient(145deg, rgba(18, 32, 49, .88), rgba(6, 10, 16, .95)); box-shadow: var(--shadow), inset 0 1px 0 rgba(255, 255, 255, .07); position: relative; overflow: hidden; backdrop-filter: blur(20px) saturate(140%); }
    .terminal-header::before { content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, rgba(20, 241, 149, .20), transparent 40%, rgba(69, 199, 255, .14)), repeating-linear-gradient(90deg, rgba(255,255,255,.04) 0, rgba(255,255,255,.04) 1px, transparent 1px, transparent 54px); pointer-events: none; }
    .terminal-copy, .pulse-grid { position: relative; z-index: 1; }
    .terminal-header h2 { margin: 5px 0 7px; font-size: 26px; line-height: 1.05; letter-spacing: 0; }
    .eyebrow { color: var(--accent); font-size: 12px; font-weight: 750; letter-spacing: .08em; text-transform: uppercase; }
    .pulse-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .pulse-card { border: 1px solid rgba(122, 143, 166, .18); background: rgba(5, 8, 13, .58); border-radius: 14px; padding: 11px; min-height: 68px; box-shadow: inset 0 1px 0 rgba(255, 255, 255, .06); }
    .pulse-card span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 8px; }
    .pulse-card strong { display: flex; align-items: center; gap: 7px; font-size: 14px; line-height: 1.25; }
    .status-dot { width: 8px; height: 8px; border-radius: 999px; background: var(--accent); box-shadow: 0 0 16px rgba(43, 212, 164, .72); display: inline-block; flex: 0 0 auto; }
    header { display: grid; gap: 3px; }
    h1 { margin: 0 0 6px; font-size: 28px; letter-spacing: 0; }
    h2 { font-size: 18px; margin: 0; }
    .muted { color: var(--muted); }
    .control-panel { margin-bottom: 16px; }
    .toolbar { display: grid; grid-template-columns: minmax(220px, 1fr) 140px 130px 130px 150px 150px 132px 132px 132px; gap: 10px; margin: 0; padding: 12px; }
    input, select, button { width: 100%; border: 1px solid var(--line); border-radius: 10px; background: rgba(7, 12, 19, .82); color: var(--text); padding: 10px 12px; font: inherit; }
    input:focus, select:focus, button:focus { outline: 2px solid rgba(43, 212, 164, .34); outline-offset: 1px; border-color: rgba(43, 212, 164, .6); }
    button { cursor: pointer; background: #132532; transition: border-color .16s ease, color .16s ease, background .16s ease, transform .12s ease; }
    button:hover { border-color: rgba(43, 212, 164, .5); color: var(--accent); }
    button:active { transform: translateY(1px); }
    .local-tools { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 0; padding: 0 12px 12px; }
    .local-tools button { width: auto; min-width: 132px; }
    .local-state-status { color: var(--muted); font-size: 13px; }
    .local-state-status.error { color: var(--red); }
    .radar-overview { display: grid; gap: 10px; margin-bottom: 16px; }
    .stats, .module-grid { display: grid; gap: 10px; margin-bottom: 16px; }
    .stats { grid-template-columns: repeat(6, minmax(0, 1fr)); }
    .module-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .signal-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 16px; }
    .split-screen-shell { display: grid; grid-template-columns: minmax(0, 1fr) 410px; gap: 14px; align-items: start; }
    .terminal-workbench { min-height: calc(100vh - 94px); }
    .radar-left-pane { display: grid; gap: 12px; min-width: 0; }
    .radar-right-pane { min-width: 0; }
    .compact-command-bar { display: grid; gap: 10px; }
    .compact-recommendations .recommendation-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); max-height: 430px; overflow: auto; }
    .compact-recommendations .recommendation-bucket { min-height: 184px; }
    .terminal-workbench > section { min-width: 0; }
    .token-dossier { position: sticky; top: 82px; display: grid; gap: 12px; min-width: 0; max-height: calc(100vh - 98px); overflow: auto; padding-right: 2px; }
    .dossier-card { background: var(--surface-glass); border: 1px solid var(--line); border-radius: 14px; padding: 14px; backdrop-filter: blur(16px) saturate(125%); box-shadow: 0 18px 55px rgba(0, 0, 0, .18), inset 0 1px 0 rgba(255,255,255,.045); }
    .mini-chart-panel { min-height: 178px; overflow: hidden; }
    .chart-candles { display: flex; align-items: end; gap: 7px; height: 112px; margin-top: 16px; padding: 10px 8px 4px; border: 1px solid var(--line); border-radius: 12px; background: linear-gradient(180deg, rgba(69, 199, 255, .08), rgba(20, 241, 149, .03)); }
    .chart-candles i { display: block; flex: 1; max-width: 18px; min-width: 8px; border-radius: 999px 999px 3px 3px; background: linear-gradient(180deg, var(--accent), rgba(20, 241, 149, .24)); box-shadow: 0 0 18px rgba(20, 241, 149, .22); }
    .chart-candles i:nth-child(2n) { background: linear-gradient(180deg, var(--red), rgba(255, 92, 122, .2)); box-shadow: 0 0 18px rgba(255, 92, 122, .16); }
    .chart-candles i:nth-child(1) { height: 34%; } .chart-candles i:nth-child(2) { height: 58%; } .chart-candles i:nth-child(3) { height: 44%; } .chart-candles i:nth-child(4) { height: 74%; } .chart-candles i:nth-child(5) { height: 62%; } .chart-candles i:nth-child(6) { height: 88%; } .chart-candles i:nth-child(7) { height: 52%; } .chart-candles i:nth-child(8) { height: 96%; } .chart-candles i:nth-child(9) { height: 68%; } .chart-candles i:nth-child(10) { height: 82%; }
    .stat, .module, section, .detail article { background: var(--surface-glass); border: 1px solid var(--line); border-radius: 14px; backdrop-filter: blur(16px) saturate(125%); box-shadow: 0 18px 55px rgba(0, 0, 0, .18), inset 0 1px 0 rgba(255,255,255,.045); }
    .stat { padding: 12px 14px; }
    .stat strong { display: block; font-size: 23px; margin-top: 3px; }
    .summary-tile { background: linear-gradient(145deg, rgba(10, 16, 24, .92), rgba(6, 10, 16, .92)); border: 1px solid var(--line); border-radius: 14px; padding: 13px 15px; }
    .summary-tile[data-signal] { cursor: pointer; }
    .summary-tile[data-signal]:hover { background: #132532; }
    .summary-tile span { color: var(--muted); font-size: 13px; }
    .summary-tile strong { display: block; font-size: 24px; margin-top: 3px; }
    .recommendations { margin-bottom: 16px; }
    .recommendation-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; padding: 12px; align-items: stretch; }
    .recommendation-bucket { background: linear-gradient(180deg, rgba(12, 21, 29, .86), rgba(5, 8, 13, .94)); border: 1px solid var(--line); border-radius: 14px; padding: 12px; min-height: 228px; box-shadow: inset 3px 0 0 rgba(20, 241, 149, .34), 0 18px 50px rgba(0,0,0,.16); }
    .recommendation-bucket-head { display: flex; justify-content: space-between; gap: 8px; align-items: center; margin-bottom: 10px; }
    .recommendation-bucket h3 { margin: 0; font-size: 16px; }
    .bucket-count { color: var(--muted); font-size: 12px; }
    .recommendation-items { display: grid; gap: 8px; }
    .recommendation-card { background: linear-gradient(145deg, rgba(13, 24, 37, .94), rgba(5, 8, 13, .96)); border: 1px solid rgba(122, 143, 166, .2); border-radius: 12px; padding: 11px; min-height: 142px; position: relative; overflow: hidden; }
    .recommendation-card::before { content: ""; position: absolute; inset: 0 0 auto; height: 2px; background: linear-gradient(90deg, var(--accent), var(--blue), transparent); opacity: .86; }
    .recommendation-card h3 { margin: 8px 0 8px; font-size: 18px; }
    .recommendation-card p { margin: 6px 0; color: var(--muted); font-size: 13px; line-height: 1.42; white-space: normal; overflow-wrap: anywhere; }
    .recommendation-empty { color: var(--muted); font-size: 13px; line-height: 1.45; padding: 10px 0; }
    .recommendation-action { display: inline-flex; align-items: center; min-height: 24px; padding: 3px 9px; border: 1px solid var(--line); border-radius: 999px; font-size: 12px; color: var(--muted); }
    .recommendation-main .recommendation-action { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 55%, var(--line)); }
    .recommendation-ambush .recommendation-action { color: var(--blue); border-color: color-mix(in srgb, var(--blue) 55%, var(--line)); }
    .recommendation-retest .recommendation-action { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 55%, var(--line)); }
    .recommendation-danger .recommendation-action { color: var(--red); border-color: color-mix(in srgb, var(--red) 55%, var(--line)); }
    .module { padding: 15px 16px; min-height: 128px; }
    .module h3 { margin: 0 0 8px; font-size: 15px; }
    .module p { margin: 0 0 10px; color: var(--muted); font-size: 13px; line-height: 1.45; }
    .tag, .badge { display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); }
    .tag { padding: 4px 8px; margin: 0 6px 6px 0; font-size: 12px; }
    .badge { min-width: 42px; justify-content: center; height: 24px; padding: 0 9px; font-size: 12px; }
    .signal { display: inline-flex; align-items: center; justify-content: center; min-width: 76px; height: 24px; padding: 0 9px; border: 1px solid var(--line); border-radius: 999px; font-size: 12px; }
    .signal-risk { color: var(--red); border-color: color-mix(in srgb, var(--red) 55%, var(--line)); }
    .signal-warning { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 55%, var(--line)); }
    .signal-move { color: var(--blue); border-color: color-mix(in srgb, var(--blue) 55%, var(--line)); }
    .signal-watch { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 50%, var(--line)); }
    .pin-btn { min-width: 72px; height: 28px; padding: 0 8px; font-size: 12px; }
    .pin-btn.active { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 55%, var(--line)); }
    .group-tag { display: inline-flex; align-items: center; justify-content: center; min-width: 58px; height: 24px; padding: 0 9px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); font-size: 12px; }
    .group-ambush { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 50%, var(--line)); }
    .group-retest { color: var(--blue); border-color: color-mix(in srgb, var(--blue) 50%, var(--line)); }
    .group-danger { color: var(--red); border-color: color-mix(in srgb, var(--red) 55%, var(--line)); }
    .group-skip { color: var(--muted); }
    .review-outcome { min-width: 112px; height: 32px; padding: 4px 8px; }
    .note-actions { display: grid; gap: 8px; }
    .note-editor { width: 100%; min-height: 76px; resize: vertical; line-height: 1.45; }
    .note-row { display: flex; gap: 8px; align-items: center; }
    .note-row button { width: auto; min-width: 92px; }
    .note-status { color: var(--muted); font-size: 12px; }
    .tabs { display: flex; gap: 8px; flex-wrap: wrap; margin: 14px 0; }
    .tab { width: auto; min-width: 112px; color: var(--muted); }
    .tab.active { border-color: color-mix(in srgb, var(--accent) 55%, var(--line)); color: var(--accent); }
    button.active-filter { border-color: color-mix(in srgb, var(--red) 55%, var(--line)); color: var(--red); }
    section { overflow: hidden; }
    .section-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 13px 15px; border-bottom: 1px solid var(--line); }
    .section-meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    .live { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 50%, var(--line)); }
    .medium, .partial, .cached { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 55%, var(--line)); }
    .high, .failed { color: var(--red); border-color: color-mix(in srgb, var(--red) 55%, var(--line)); }
    .symbol-cell { display: inline-flex; align-items: center; gap: 9px; min-width: 130px; color: var(--text); }
    .token-avatar { display: inline-grid; place-items: center; width: 30px; height: 30px; border-radius: 999px; color: #06110d; background: linear-gradient(145deg, #ffffff, var(--accent)); font-size: 12px; font-weight: 900; box-shadow: 0 0 18px rgba(20, 241, 149, .22); }
    .symbol-name { display: grid; gap: 1px; line-height: 1.1; }
    .symbol-name strong { font-size: 14px; }
    .symbol-name span { color: var(--muted); font-size: 11px; }
    .change-pill { display: inline-flex; align-items: center; justify-content: center; min-width: 58px; min-height: 24px; padding: 0 8px; border-radius: 999px; font-variant-numeric: tabular-nums; font-weight: 760; }
    .change-up { color: var(--accent); background: rgba(20, 241, 149, .08); }
    .change-down { color: var(--red); background: rgba(255, 92, 122, .09); }
    .table-wrap { overflow-x: auto; background: rgba(5, 8, 13, .32); border-radius: 0 0 14px 14px; }
    table { width: 100%; border-collapse: collapse; min-width: 1120px; }
    th, td { padding: 11px 13px; border-bottom: 1px solid rgba(122, 143, 166, .13); text-align: left; white-space: nowrap; font-size: 14px; }
    tbody tr { transition: background .16s ease; }
    tbody tr:hover { background: rgba(43, 212, 164, .045); }
    th { color: var(--muted); font-weight: 700; cursor: pointer; user-select: none; position: sticky; top: 0; z-index: 2; background: rgba(7, 12, 19, .96); }
    td:first-child, th:first-child { width: 54px; color: var(--muted); }
    a { color: var(--text); text-decoration: none; }
    a:hover { color: var(--blue); }
    .num { font-variant-numeric: tabular-nums; }
    .empty { color: var(--muted); text-align: center; padding: 34px; }
    .detail { display: grid; grid-template-columns: 1fr; gap: 12px; }
    .detail article { padding: 14px; min-height: 142px; }
    .detail h3 { margin: 0 0 10px; font-size: 18px; }
    .kv { display: grid; grid-template-columns: 132px 1fr; gap: 6px 10px; font-size: 14px; }
    .kv span:nth-child(odd) { color: var(--muted); }
    .kv span:nth-child(even) { min-width: 0; white-space: normal; overflow-wrap: anywhere; }
    @media (max-width: 1180px) { .split-screen-shell { grid-template-columns: 1fr; } .token-dossier { position: static; max-height: none; overflow: visible; } .detail { grid-template-columns: repeat(2, minmax(0, 1fr)); } .recommendation-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 980px) { .market-nav, .topbar, .hero-board, .terminal-header { grid-template-columns: 1fr; } .nav-links { justify-content: flex-start; } .pulse-grid, .toolbar { grid-template-columns: 1fr 1fr; } .stats, .signal-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 640px) { .toolbar, .pulse-grid, .stats, .module-grid, .signal-summary { grid-template-columns: 1fr; } .tab { flex: 1 1 100px; } .shell { padding: 14px 12px 34px; } .market-nav { padding: 12px; } }
  </style>
</head>
<body>
  <div class="market-site-shell commercial-skin">
    <nav class="market-nav" aria-label="主导航">
      <a class="nav-brand" href="#">
        <span class="brand-mark">A</span>
        <span><strong>Alpha 妖币雷达</strong><span>行情终端</span></span>
      </a>
      <div class="nav-links">
        <a class="active" href="#recommendations">今日雷达</a>
        <a href="#signalSummary">异动信号</a>
        <a href="#tableTitle">榜单</a>
        <a href="#detail">单币诊断</a>
        <a href="#sources">数据质量</a>
      </div>
    </nav>
    <main class="shell">
    <div class="screener-layout">
      <div class="topbar">
        <header>
        <h1>Alpha 妖币雷达</h1>
        <div class="muted" id="subtitle">正在加载最新快照...</div>
        </header>
        <div><span class="badge" id="quality">加载中</span></div>
      </div>
    <div class="split-screen-shell terminal-workbench" aria-label="左右分屏">
    <section class="radar-left-pane">
    <section class="hero-board premium-hero">
    <section class="terminal-header" aria-label="市场脉冲">
      <div class="terminal-copy">
        <span class="eyebrow">市场脉冲</span>
        <h2>Alpha Radar 工作台</h2>
        <div class="muted">把 Meme 热度、Binance Alpha、庄家代理和合约异动放在一张决策屏里。</div>
      </div>
      <div class="pulse-grid">
        <div class="pulse-card"><span>扫描状态</span><strong><i class="status-dot"></i>在线刷新</strong></div>
        <div class="pulse-card"><span>执行边界</span><strong>只读模式</strong></div>
        <div class="pulse-card"><span>视觉版本</span><strong><span class="badge visual-grade">商业版视觉</span></strong></div>
      </div>
    </section>
      <div class="hero-metrics" aria-label="雷达要点">
        <div class="hero-metric"><span>今日雷达</span><strong>先看推荐，再看风险</strong></div>
        <div class="hero-metric"><span>Alpha 范围</span><strong>妖币榜和庄家雷达仅 Binance Alpha</strong></div>
        <div class="hero-metric"><span>安全边界</span><strong>只读数据，不接交易</strong></div>
      </div>
    </section>
    <div class="ticker-strip" aria-label="信号热度">
      <div class="ticker-chip"><span>信号热度</span><strong>实时评分</strong></div>
      <div class="ticker-chip"><span>Meme</span><strong>全网扫描</strong></div>
      <div class="ticker-chip"><span>Alpha</span><strong>Binance Alpha</strong></div>
      <div class="ticker-chip"><span>庄家代理</span><strong>筹码 + OI</strong></div>
      <div class="ticker-chip"><span>提醒边界</span><strong>只读展示</strong></div>
    </div>
    <div class="compact-command-bar">
    <div class="market-tabs" aria-label="行情频道">
      <a class="active" href="#recommendations">推荐</a>
      <a href="#signalSummary">异动</a>
      <a href="#tableTitle">Meme / Alpha / 庄家</a>
      <a href="#detail">详情诊断</a>
    </div>
    <section class="control-panel" aria-label="筛选控制台">
      <div class="section-head"><h2>筛选控制台</h2><span class="badge live">快速定位</span></div>
    <div class="toolbar">
      <input id="search" placeholder="搜索币名 / 链 / 触发原因" />
      <select id="chain"><option value="">全部链</option></select>
      <select id="risk"><option value="">全部风险</option><option value="high">高风险</option><option value="medium">中风险</option><option value="low">低风险</option></select>
      <select id="minScore"><option value="0">全部分数</option><option value="50">50+</option><option value="70">70+</option><option value="85">85+</option></select>
      <select id="focus"><option value="">重点筛选</option><option value="holder">高控盘</option><option value="fresh">新池</option><option value="futures">已上合约</option><option value="funding">费率异常</option></select>
      <select id="signalFilter"><option value="">全部信号</option><option value="holder_risk">高控盘风险</option><option value="pump">可疑拉盘</option><option value="futures_move">合约异动</option><option value="watch">观察</option></select>
      <select id="groupFilter"><option value="">观察分组</option><option value="ambush">埋伏</option><option value="retest">等回踩</option><option value="danger">高危</option><option value="skip">放弃</option></select>
      <button id="dangerOnly" type="button">只看危险</button>
      <button id="pinnedOnly" type="button">只看关注</button>
    </div>
    <div class="local-tools" aria-label="本地设置">
      <button id="exportLocalState" type="button">导出本地设置</button>
      <button id="importLocalState" type="button">导入本地设置</button>
      <input id="importLocalFile" type="file" accept="application/json,.json" hidden />
      <span class="local-state-status" id="localStateStatus">本地设置只保存在当前浏览器</span>
    </div>
    </section>
    </div>
    <section class="radar-overview" aria-label="实时概览">
      <div class="section-head"><h2>实时概览</h2><span class="badge live">策略工作台</span></div>
      <div class="stats">
        <div class="stat"><span class="muted">Meme/土狗候选</span><strong id="memeCount">0</strong></div>
        <div class="stat"><span class="muted">Alpha 候选（仅 Binance Alpha）</span><strong id="alphaCount">0</strong></div>
        <div class="stat"><span class="muted">庄家代理（仅 Binance Alpha）</span><strong id="dealerCount">0</strong></div>
        <div class="stat"><span class="muted">筹码覆盖</span><strong id="holderCoverage">0%</strong></div>
        <div class="stat"><span class="muted">实时数据源</span><strong id="liveSources">0</strong></div>
        <div class="stat"><span class="muted">失败源</span><strong id="errorCount">0</strong></div>
      </div>
      <div class="signal-summary" id="signalSummary" aria-label="风险摘要">
        <div class="summary-tile signal-risk" data-signal="holder_risk"><span>高控盘风险</span><strong id="summaryHolderRisk">0</strong></div>
        <div class="summary-tile signal-warning" data-signal="pump"><span>可疑拉盘</span><strong id="summaryPump">0</strong></div>
        <div class="summary-tile signal-move" data-signal="futures_move"><span>合约异动</span><strong id="summaryFuturesMove">0</strong></div>
        <div class="summary-tile signal-watch" data-signal="watch"><span>观察</span><strong id="summaryWatch">0</strong></div>
      </div>
    </section>
    <section class="compact-recommendations recommendations" id="recommendations">
      <div class="section-head"><h2>AI结论台（今日推荐）</h2><span class="badge live">一句话结论</span></div>
      <div class="recommendation-grid" id="recommendationGrid"></div>
    </section>
    <div class="tabs"><button class="tab active" data-view="dealer">庄家雷达</button><button class="tab" data-view="meme">Meme/土狗推荐</button><button class="tab" data-view="alpha">Alpha 妖币</button><button class="tab" data-view="watchlist">我的关注池</button><button class="tab" data-view="review">复盘视图</button></div>
    <section>
      <div class="section-head"><h2 id="tableTitle">庄家雷达</h2><div class="section-meta"><span class="badge" id="memeBacktestSummary" hidden>1h 胜率 - · 回测 0 条</span><span class="badge live" id="rowCount">0 条</span></div></div>
      <div class="table-wrap"><table><thead><tr id="thead"></tr></thead><tbody id="tbody"></tbody></table></div>
    </section>
    </section>
    <aside class="radar-right-pane token-dossier" aria-label="单币情报面板">
      <article class="dossier-card mini-chart-panel">
        <div class="section-head"><h2>结构速览</h2><span class="badge live">单币情报面板</span></div>
        <div class="chart-candles" aria-label="结构速览图"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
      </article>
    <div class="detail">
      <article><h3>当前选中</h3><div class="kv" id="detail">点击表格里任意一行查看详情。</div></article>
      <article><h3>筹码雷达</h3><div class="kv" id="holderDetail">点击表格里任意一行查看筹码。</div></article>
      <article><h3>庄家评分拆解</h3><div class="kv" id="dealerDetail">点击表格里任意一行查看拆解。</div></article>
      <article><h3>数据质量</h3><div class="kv" id="sources"></div></article>
      <article><h3>本地操作日志</h3><div class="kv" id="actionLog">暂无操作</div></article>
    </div>
    </aside>
    </div>
    </div>
    </main>
  </div>
  <script>
    const PIN_STORAGE_KEY = "alphaRadarPinnedV1";
    const NOTE_STORAGE_KEY = "alphaRadarNotesV1";
    const GROUP_STORAGE_KEY = "alphaRadarGroupsV1";
    const ACTION_LOG_STORAGE_KEY = "alphaRadarActionLogV1";
    const REVIEW_OUTCOME_STORAGE_KEY = "alphaRadarReviewOutcomesV1";
    const MEME_FIRST_SEEN_STORAGE_KEY = "alphaRadarMemeFirstSeenV1";
    const LOCAL_STATE_VERSION = 1;
    const GROUP_OPTIONS = [{ value: "", label: "未分组" }, { value: "ambush", label: "埋伏" }, { value: "retest", label: "等回踩" }, { value: "danger", label: "高危" }, { value: "skip", label: "放弃" }];
    const REVIEW_OUTCOME_OPTIONS = [{ value: "", label: "未标记" }, { value: "right", label: "对" }, { value: "wrong", label: "错" }, { value: "pending", label: "还没走出来" }];
    const state = { view: "dealer", report: null, sortKey: "priority", sortDir: 1, dangerOnly: false, pinnedOnly: false, pinned: loadPinned(), notes: loadNotes(), groups: loadGroups(), actionLog: loadActionLog(), reviewOutcomes: loadReviewOutcomes(), memeFirstSeen: loadMemeFirstSeen() };
    const labels = { pin: "关注", group: "观察分组", watchlist_source: "来自", review_time: "时间", review_action: "动作", symbol: "币", signal: "信号", chain: "链", dealer_score: "分数", score: "分数", market_cap: "市值", heat_score: "热度", top10_holder_pct: "前10钱包", risk_level: "风险", oi_change_1h_pct: "持仓1h", funding_rate_pct: "资金费率", dealer_flags: "触发原因", volume_24h: "24h量", liquidity_usd: "池子", sources: "来源", price_change_24h_pct: "24h涨跌", reasons: "触发原因" };
    labels.review_outcome = "标记结果";
    Object.assign(labels, { name: "名称", price_usd: "价格", change_m1: "1m", change_m5: "5m", change_h1: "1h", holders: "持有人", smart_money: "聪明钱", kol: "KOL", ai_judgement: "AI判断", meme_potential: "潜力", first_seen_change: "首推后涨幅", meme_links: "链接" });
    const columns = {
      dealer: ["pin", "group", "symbol", "signal", "chain", "dealer_score", "market_cap", "heat_score", "top10_holder_pct", "risk_level", "oi_change_1h_pct", "funding_rate_pct", "dealer_flags"],
      meme: ["pin", "group", "symbol", "name", "price_usd", "change_m1", "change_m5", "change_h1", "market_cap", "volume_24h", "liquidity_usd", "holders", "smart_money", "kol", "ai_judgement", "meme_potential", "first_seen_change", "meme_links"],
      alpha: ["pin", "group", "symbol", "signal", "chain", "score", "market_cap", "price_change_24h_pct", "oi_change_1h_pct", "funding_rate_pct", "risk_level", "reasons"],
      watchlist: ["pin", "group", "watchlist_source", "symbol", "signal", "chain", "score", "market_cap", "top10_holder_pct", "oi_change_1h_pct", "funding_rate_pct", "risk_level"],
      review: ["review_time", "review_outcome", "review_action", "symbol", "group", "signal", "chain", "score", "market_cap", "risk_level"]
    };
    const RECOMMENDATION_BUCKETS = [
      { key: "main", title: "主推候选", hint: "优先看，等短线量能继续确认。" },
      { key: "ambush", title: "埋伏候选", hint: "先放关注池，等下一次放量。" },
      { key: "retest", title: "等回踩候选", hint: "涨太快，不追高，等回踩结构。" },
      { key: "danger", title: "高危别碰", hint: "风险信号明显，只做风险记录。" }
    ];
    function esc(v) { return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c])); }
    function num(v, d = 1) { const n = Number(v); return Number.isFinite(n) ? n.toFixed(d) : "-"; }
    function pct(v, d = 1) { const n = Number(v); return Number.isFinite(n) ? `${n.toFixed(d)}%` : "-"; }
    function signedPct(v, d = 1) { const n = Number(v); return Number.isFinite(n) ? `${n >= 0 ? "+" : ""}${n.toFixed(d)}%` : "-"; }
    function changePill(v, d = 1) {
      const n = Number(v);
      if (!Number.isFinite(n)) return "-";
      return `<span class="change-pill ${n >= 0 ? "change-up" : "change-down"}">${n >= 0 ? "+" : ""}${n.toFixed(d)}%</span>`;
    }
    function tokenInitials(row) {
      return String(row.symbol || row.name || "?").replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase() || "?";
    }
    function symbolCell(row, value) {
      const symbol = value || row.symbol || "-";
      const sub = row.chain || row.chain_id || row.name || "Token";
      const content = `<span class="symbol-cell"><span class="token-avatar">${esc(tokenInitials(row))}</span><span class="symbol-name"><strong>${esc(symbol)}</strong><span>${esc(sub)}</span></span></span>`;
      const href = row.dex_url || row.url || "";
      return href ? `<a href="${esc(href)}" target="_blank" rel="noreferrer">${content}</a>` : content;
    }
    function price(v) { const n = Number(v); if (!Number.isFinite(n) || n <= 0) return "-"; if (n < 0.000001) return `$${n.toExponential(2)}`; if (n < 0.01) return `$${n.toPrecision(4)}`; return `$${n.toFixed(n < 1 ? 4 : 2)}`; }
    function money(v) { const n = Number(v); if (!Number.isFinite(n)) return "-"; if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`; if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`; if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`; return `$${n.toFixed(0)}`; }
    function fieldValue(row, key) {
      const aliases = { market_cap: ["market_cap", "mcap", "fdv"], volume_24h: ["volume_24h", "volume24h", "dex_volume24h"], liquidity_usd: ["liquidity_usd", "liquidity", "dex_liquidity"], price_usd: ["price_usd", "priceUsd"], change_m1: ["change_m1", "change_m1_pct"], change_m5: ["change_m5", "price_change_m5"], change_h1: ["change_h1", "price_change_h1"] }[key] || [key];
      for (const name of aliases) {
        if (row[name] !== undefined && row[name] !== null && row[name] !== "") return row[name];
      }
      return undefined;
    }
    function riskText(v) { return ({ high: "高", medium: "中", low: "低" }[v] || v || "-"); }
    function sourceLabel(v) { const s = String(v || "").toLowerCase(); if (!s) return "未接到"; if (s.includes("rugcheck")) return "RugCheck 持仓"; if (s.includes("bscscan")) return "BscScan 持仓"; if (s.includes("blockscout")) return "Blockscout 持仓"; if (s.includes("goplus")) return "GoPlus 风险"; if (s.includes("dex")) return "DexScreener"; if (s.includes("binance")) return "Binance"; return v; }
    function loadMemeFirstSeen() {
      try { return JSON.parse(localStorage.getItem(MEME_FIRST_SEEN_STORAGE_KEY) || "{}"); } catch (_) { return {}; }
    }
    function saveMemeFirstSeen() {
      localStorage.setItem(MEME_FIRST_SEEN_STORAGE_KEY, JSON.stringify(state.memeFirstSeen));
    }
    function updateMemeFirstSeen() {
      (state.report?.meme_rows || []).forEach(row => {
        const key = rowKey(row);
        const current = Number(fieldValue(row, "price_usd"));
        if (!key || !Number.isFinite(current) || current <= 0 || state.memeFirstSeen[key]) return;
        state.memeFirstSeen[key] = { symbol: row.symbol || "-", price: current, at: new Date().toISOString() };
      });
      saveMemeFirstSeen();
    }
    function firstSeenStats(row) {
      const item = state.memeFirstSeen[rowKey(row)];
      const current = Number(fieldValue(row, "price_usd"));
      const first = Number(item?.price);
      if (!item || !Number.isFinite(current) || !Number.isFinite(first) || first <= 0) return "--";
      return signedPct(((current - first) / first) * 100, 1);
    }
    function memeBacktestSummary() {
      const rows = state.report?.meme_rows || [];
      const now = Date.now();
      let total = 0, wins = 0;
      rows.forEach(row => {
        const item = state.memeFirstSeen[rowKey(row)];
        if (!item || now - Date.parse(item.at || "") < 60 * 60 * 1000) return;
        const current = Number(fieldValue(row, "price_usd"));
        const first = Number(item.price);
        if (!Number.isFinite(current) || !Number.isFinite(first) || first <= 0) return;
        total += 1;
        if (((current - first) / first) * 100 >= 30) wins += 1;
      });
      return total ? `1h 胜率 ${Math.round((wins / total) * 100)}% · 回测 ${total} 条` : "1h 胜率 - · 回测 0 条";
    }
    function holdersText(row) {
      const count = firstNumber(row.holder_count, row.holders_count, row.holderCount);
      if (count !== null) return String(Math.round(count));
      const top10 = firstNumber(row.top10_holder_pct);
      return top10 !== null ? `Top10 ${top10.toFixed(1)}%` : "--";
    }
    function smartMoneyText(row) {
      const count = firstNumber(row.smart_money, row.smart_money_count, row.smart_wallet_count);
      if (count !== null) return String(Math.round(count));
      const sources = String((row.sources || []).join(" ")).toLowerCase();
      return sources.includes("gmgn") ? "GMGN" : "--";
    }
    function kolText(row) {
      const count = firstNumber(row.kol, row.kol_count, row.twitter_kol_count);
      if (count !== null) return String(Math.round(count));
      const heat = firstNumber(row.heat_score);
      return heat !== null && heat >= 25 ? "热" : "--";
    }
    function memePotential(row) {
      const score = rowScore(row);
      const h1 = Number(fieldValue(row, "change_h1"));
      const m5 = Number(fieldValue(row, "change_m5"));
      const mcap = Number(fieldValue(row, "market_cap"));
      const liq = Number(fieldValue(row, "liquidity_usd"));
      if (score >= 85 && h1 >= 100 && mcap > 0 && mcap <= 10_000_000) return "100x+";
      if (score >= 75 && m5 >= 30) return "强";
      if (liq > 0 && mcap > 0 && liq < Math.max(50000, mcap * 0.015)) return "浅池";
      if (score >= 60) return "观察";
      return "--";
    }
    function memeAiVerdict(row) {
      const h1 = Number(fieldValue(row, "change_h1"));
      const m5 = Number(fieldValue(row, "change_m5"));
      const mcap = Number(fieldValue(row, "market_cap"));
      const liq = Number(fieldValue(row, "liquidity_usd"));
      const top10 = firstNumber(row.top10_holder_pct);
      const maxHolder = firstNumber(row.max_holder_pct);
      const vol = Number(fieldValue(row, "volume_24h"));
      const volToMcap = mcap > 0 && vol > 0 ? vol / mcap : 0;
      if ((top10 !== null && top10 >= 70) || (maxHolder !== null && maxHolder >= 35)) return "高危";
      if (liq > 0 && mcap > 0 && liq < Math.max(30000, mcap * 0.01)) return "放弃";
      if (h1 >= 200 || m5 >= 80) return "等回踩";
      if (h1 >= 30 && m5 >= 10 && volToMcap >= 0.25) return "可埋伏";
      return "观察";
    }
    function memeLinks(row) {
      const links = [];
      if (row.url || row.dex_url) links.push(`<a href="${esc(row.url || row.dex_url)}" target="_blank" rel="noreferrer">DS</a>`);
      const chain = row.chain || row.chain_id || "";
      const address = row.contract_address || row.token_address || "";
      if (chain && address) links.push(`<a href="https://gmgn.ai/${esc(chain)}/token/${esc(address)}" target="_blank" rel="noreferrer">GMGN</a>`);
      return links.length ? links.join(" · ") : "--";
    }
    function recommendationFor(row) {
      const verdict = memeAiVerdict(row);
      const score = rowScore(row);
      const h1 = Number(fieldValue(row, "change_h1"));
      const m5 = Number(fieldValue(row, "change_m5"));
      const mcap = Number(fieldValue(row, "market_cap"));
      const liq = Number(fieldValue(row, "liquidity_usd"));
      const top10 = firstNumber(row.top10_holder_pct);
      if (verdict === "高危" || verdict === "放弃" || (top10 !== null && top10 >= 70)) return { key: "danger", label: "高危别碰", rank: 4 };
      if (Number.isFinite(h1) && h1 >= 120) return { key: "retest", label: "等回踩", rank: 2 };
      if (verdict === "可埋伏" && score >= 75 && Number.isFinite(mcap) && mcap <= 80_000_000 && (!Number.isFinite(liq) || liq >= 20_000)) return { key: "main", label: "主推", rank: 0 };
      if (verdict === "可埋伏" || (score >= 65 && Number.isFinite(m5) && m5 >= 5)) return { key: "ambush", label: "可埋伏", rank: 1 };
      return { key: "watch", label: "观察", rank: 3 };
    }
    function recommendationReason(row) {
      const bits = [];
      const h1 = Number(fieldValue(row, "change_h1"));
      const m5 = Number(fieldValue(row, "change_m5"));
      const mcap = Number(fieldValue(row, "market_cap"));
      const vol = Number(fieldValue(row, "volume_24h"));
      const liq = Number(fieldValue(row, "liquidity_usd"));
      if (Number.isFinite(h1)) bits.push(`1h ${signedPct(h1)}`);
      if (Number.isFinite(m5)) bits.push(`5m ${signedPct(m5)}`);
      if (Number.isFinite(mcap)) bits.push(`市值 ${money(mcap)}`);
      if (Number.isFinite(vol)) bits.push(`成交 ${money(vol)}`);
      if (Number.isFinite(liq)) bits.push(`池子 ${money(liq)}`);
      return bits.slice(0, 4).join(" / ") || reasonText(row);
    }
    function recommendationOneLiner(row, action) {
      const symbol = row.symbol || row.name || "这个币";
      if (action.label === "主推") return `${symbol} 可以优先盯，量能和热度同时够强。`;
      if (action.label === "可埋伏") return `${symbol} 先放关注池，等下一次放量确认。`;
      if (action.label === "等回踩") return `${symbol} 已经拉太快，先等回踩再看。`;
      if (action.label === "高危别碰") return `${symbol} 风险信号太硬，先别碰。`;
      return `${symbol} 还没到推荐位，继续观察。`;
    }
    function recommendationNextStep(row) {
      const action = recommendationFor(row).label;
      if (action === "主推") return "优先看，等 5m 放量延续，不追长上影。";
      if (action === "可埋伏") return "加入关注池，等量能继续放大。";
      if (action === "等回踩") return "不追高，等 5m/15m 回踩不破再看。";
      if (action === "高危别碰") return "只做风险记录，不进观察池。";
      return "继续观察，等成交或聪明钱信号变强。";
    }
    function recommendationRisk(row) {
      const risks = [];
      const top10 = firstNumber(row.top10_holder_pct);
      const maxHolder = firstNumber(row.max_holder_pct);
      const liq = Number(fieldValue(row, "liquidity_usd"));
      const mcap = Number(fieldValue(row, "market_cap"));
      if (top10 !== null) risks.push(`Top10 ${top10.toFixed(1)}%`);
      if (maxHolder !== null) risks.push(`最大钱包 ${maxHolder.toFixed(1)}%`);
      if (Number.isFinite(liq) && Number.isFinite(mcap) && liq < Math.max(50_000, mcap * 0.02)) risks.push("浅池");
      return risks.slice(0, 3).join(" / ") || "暂无硬风险，仍需等量确认";
    }
    function recommendationRows() {
      const seen = new Set();
      return (state.report?.meme_rows || [])
        .map(row => ({ row, action: recommendationFor(row) }))
        .filter(item => {
          const key = rowKey(item.row);
          if (seen.has(key)) return false;
          seen.add(key);
          return item.action.label !== "观察";
        })
        .sort((a, b) => a.action.rank - b.action.rank || rowScore(b.row) - rowScore(a.row))
        .slice(0, 4);
    }
    function recommendationBuckets() {
      const buckets = Object.fromEntries(RECOMMENDATION_BUCKETS.map(bucket => [bucket.key, []]));
      const seen = new Set();
      (state.report?.meme_rows || [])
        .map(row => ({ row, action: recommendationFor(row) }))
        .sort((a, b) => a.action.rank - b.action.rank || rowScore(b.row) - rowScore(a.row))
        .forEach(item => {
          const key = rowKey(item.row);
          const target = buckets[item.action.key];
          if (!target || seen.has(key) || item.action.label === "观察" || target.length >= 2) return;
          seen.add(key);
          target.push(item);
        });
      return buckets;
    }
    function recommendationCard(row, action) {
      return `
        <article class="recommendation-card recommendation-${esc(action.key)}">
          <span class="recommendation-action">${esc(action.label)}</span>
          <h3>${esc(row.symbol || "-")}</h3>
          <p><strong>一句话结论</strong>：${esc(recommendationOneLiner(row, action))}</p>
          <p><strong>原因</strong>：${esc(recommendationReason(row))}</p>
          <p><strong>下一步动作</strong>：${esc(recommendationNextStep(row))}</p>
          <p><strong>风险</strong>：${esc(recommendationRisk(row))}</p>
          <p>${memeLinks(row)}</p>
        </article>`;
    }
    function renderRecommendations() {
      const target = document.getElementById("recommendationGrid");
      if (!target) return;
      const buckets = recommendationBuckets();
      target.innerHTML = RECOMMENDATION_BUCKETS.map(bucket => {
        const rows = buckets[bucket.key] || [];
        const body = rows.length
          ? rows.map(({ row, action }) => recommendationCard(row, action)).join("")
          : `<div class="recommendation-empty"><strong>暂无符合</strong><br>${esc(bucket.hint)}</div>`;
        return `
          <article class="recommendation-bucket recommendation-${esc(bucket.key)}">
            <div class="recommendation-bucket-head">
              <h3>${esc(bucket.title)}</h3>
              <span class="bucket-count">${rows.length} 个</span>
            </div>
            <div class="recommendation-items">${body}</div>
          </article>`;
      }).join("");
    }
    function loadPinned() {
      try { return new Set(JSON.parse(localStorage.getItem(PIN_STORAGE_KEY) || "[]")); } catch (_) { return new Set(); }
    }
    function savePinned() {
      localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify([...state.pinned]));
    }
    function rowKey(row) {
      return [row.chain || row.chain_id || "", row.contract_address || row.token_address || row.pair_address || row.symbol || ""].join(":").toLowerCase();
    }
    function isPinned(row) {
      return state.pinned.has(rowKey(row));
    }
    function togglePinned(row) {
      const key = rowKey(row);
      const nextPinned = !state.pinned.has(key);
      if (nextPinned) state.pinned.add(key); else state.pinned.delete(key);
      savePinned();
      addActionLog(row, nextPinned ? "置顶" : "取消置顶", row.symbol || "");
      renderTable();
    }
    function loadNotes() {
      try { return JSON.parse(localStorage.getItem(NOTE_STORAGE_KEY) || "{}"); } catch (_) { return {}; }
    }
    function saveNotes() {
      localStorage.setItem(NOTE_STORAGE_KEY, JSON.stringify(state.notes));
    }
    function noteFor(row) {
      return state.notes[rowKey(row)] || "";
    }
    function saveNoteFor(row, value) {
      const key = rowKey(row);
      const text = String(value || "").trim();
      if (text) state.notes[key] = text; else delete state.notes[key];
      saveNotes();
      addActionLog(row, "保存备注", text ? text.slice(0, 40) : "清空备注");
    }
    function loadGroups() {
      try { return JSON.parse(localStorage.getItem(GROUP_STORAGE_KEY) || "{}"); } catch (_) { return {}; }
    }
    function saveGroups() {
      localStorage.setItem(GROUP_STORAGE_KEY, JSON.stringify(state.groups));
    }
    function groupFor(row) {
      return state.groups[rowKey(row)] || "";
    }
    function saveGroupFor(row, value) {
      const key = rowKey(row);
      const before = groupLabel(state.groups[key] || "");
      if (value) state.groups[key] = value; else delete state.groups[key];
      saveGroups();
      addActionLog(row, "修改分组", `${before} -> ${groupLabel(value)}`);
    }
    function groupLabel(value) {
      return (GROUP_OPTIONS.find(item => item.value === value) || GROUP_OPTIONS[0]).label;
    }
    function groupBadge(row) {
      const group = groupFor(row);
      return `<span class="group-tag ${group ? "group-" + group : ""}">${esc(groupLabel(group))}</span>`;
    }
    function groupSelect(row) {
      const selected = groupFor(row);
      return `<select id="groupEditor">${GROUP_OPTIONS.map(item => `<option value="${esc(item.value)}" ${item.value === selected ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select>`;
    }
    function listSourceLabel(v) {
      return ({ dealer: "庄家", meme: "Meme", alpha: "Alpha" }[v] || v || "-");
    }
    function withListSource(row, source) {
      return { ...row, watchlist_source: source };
    }
    function isWatchlisted(row) {
      return isPinned(row) || Boolean(groupFor(row));
    }
    function watchlistRows() {
      const seen = new Set();
      const rows = [];
      [["dealer", "dealer_rows"], ["meme", "meme_rows"], ["alpha", "alpha_rows"]].forEach(([source, key]) => {
        (state.report?.[key] || []).forEach(row => {
          const tagged = withListSource(row, source);
          const keyValue = rowKey(tagged);
          if (!isWatchlisted(tagged) || seen.has(keyValue)) return;
          seen.add(keyValue);
          rows.push(tagged);
        });
      });
      return rows;
    }
    function findRowByKey(itemKey) {
      for (const key of ["dealer_rows", "meme_rows", "alpha_rows"]) {
        const row = (state.report?.[key] || []).find(item => rowKey(item) === itemKey);
        if (row) return row;
      }
      return null;
    }
    function loadReviewOutcomes() {
      try { return JSON.parse(localStorage.getItem(REVIEW_OUTCOME_STORAGE_KEY) || "{}"); } catch (_) { return {}; }
    }
    function saveReviewOutcomes() {
      localStorage.setItem(REVIEW_OUTCOME_STORAGE_KEY, JSON.stringify(state.reviewOutcomes));
    }
    function reviewItemId(item) {
      return item.id || [item.key, item.at, item.action, item.detail || ""].join("|");
    }
    function reviewOutcomeFor(item) {
      return state.reviewOutcomes[reviewItemId(item)] || "";
    }
    function saveReviewOutcome(item, value) {
      const id = reviewItemId(item);
      if (value) state.reviewOutcomes[id] = value; else delete state.reviewOutcomes[id];
      saveReviewOutcomes();
    }
    function reviewOutcomeSelect(row) {
      const selected = row.review_outcome || "";
      const id = esc(row.review_id || "");
      return `<select class="review-outcome" data-review-id="${id}">${REVIEW_OUTCOME_OPTIONS.map(item => `<option value="${esc(item.value)}" ${item.value === selected ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select>`;
    }
    function withReviewSource(item, row) {
      return { ...row, review_id: reviewItemId(item), review_item: item, review_outcome: reviewOutcomeFor(item), review_time: item.at, review_action: `${item.action}${item.detail ? " / " + item.detail : ""}` };
    }
    function reviewRows() {
      return state.actionLog.map(item => {
        const row = findRowByKey(item.key);
        return row ? withReviewSource(item, row) : null;
      }).filter(Boolean);
    }
    function openReviewRow(row) {
      const base = findRowByKey(rowKey(row)) || row;
      renderDetail(base);
    }
    function loadActionLog() {
      try { return JSON.parse(localStorage.getItem(ACTION_LOG_STORAGE_KEY) || "[]"); } catch (_) { return []; }
    }
    function saveActionLog() {
      localStorage.setItem(ACTION_LOG_STORAGE_KEY, JSON.stringify(state.actionLog.slice(0, 300)));
    }
    function addActionLog(row, action, detail) {
      state.actionLog.unshift({ id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, key: rowKey(row), symbol: row.symbol || "-", action, detail: detail || "", at: new Date().toISOString() });
      state.actionLog = state.actionLog.slice(0, 300);
      saveActionLog();
      renderActionLog(row);
    }
    function actionLogFor(row) {
      const key = rowKey(row);
      return state.actionLog.filter(item => item.key === key).slice(0, 8);
    }
    function renderActionLog(row) {
      const target = document.getElementById("actionLog");
      if (!target || !row) return;
      const items = actionLogFor(row);
      target.innerHTML = items.length ? items.map(item => `<span>${new Date(item.at).toLocaleString("zh-CN")}</span><span>${esc(item.action)}${item.detail ? " / " + esc(item.detail) : ""}</span>`).join("") : "<span>暂无操作</span><span>这个币还没有本地记录</span>";
    }
    function localStatePayload() {
      return { version: LOCAL_STATE_VERSION, exported_at: new Date().toISOString(), pinned: [...state.pinned], groups: state.groups, notes: state.notes, actionLog: state.actionLog, outcomes: state.reviewOutcomes, memeFirstSeen: state.memeFirstSeen };
    }
    function setLocalStateStatus(text, isError = false) {
      const status = document.getElementById("localStateStatus");
      if (!status) return;
      status.textContent = text;
      status.classList.toggle("error", isError);
    }
    function downloadLocalState() {
      const blob = new Blob([JSON.stringify(localStatePayload(), null, 2)], { type: "application/json;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "alpha-radar-local-state.json";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setLocalStateStatus("已导出本地设置");
    }
    function applyLocalState(payload) {
      if (!payload || typeof payload !== "object") throw new Error("文件格式不对");
      state.pinned = new Set(Array.isArray(payload.pinned) ? payload.pinned.map(String) : []);
      state.groups = payload.groups && typeof payload.groups === "object" && !Array.isArray(payload.groups) ? payload.groups : {};
      state.notes = payload.notes && typeof payload.notes === "object" && !Array.isArray(payload.notes) ? payload.notes : {};
      state.actionLog = Array.isArray(payload.actionLog) ? payload.actionLog : [];
      state.reviewOutcomes = payload.outcomes && typeof payload.outcomes === "object" && !Array.isArray(payload.outcomes) ? payload.outcomes : {};
      state.memeFirstSeen = payload.memeFirstSeen && typeof payload.memeFirstSeen === "object" && !Array.isArray(payload.memeFirstSeen) ? payload.memeFirstSeen : {};
      savePinned();
      saveGroups();
      saveNotes();
      saveActionLog();
      saveReviewOutcomes();
      saveMemeFirstSeen();
      renderTable();
      setLocalStateStatus("已导入本地设置");
    }
    function bindLocalStateTools() {
      const exportButton = document.getElementById("exportLocalState");
      const importButton = document.getElementById("importLocalState");
      const fileInput = document.getElementById("importLocalFile");
      if (!exportButton || !importButton || !fileInput) return;
      exportButton.onclick = downloadLocalState;
      importButton.onclick = () => fileInput.click();
      fileInput.onchange = () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => {
          try { applyLocalState(JSON.parse(String(reader.result || "{}"))); }
          catch (err) { setLocalStateStatus("导入失败：" + err.message, true); }
        };
        reader.onerror = () => setLocalStateStatus("导入失败：无法读取文件", true);
        reader.readAsText(file, "utf-8");
        fileInput.value = "";
      };
    }
    function rowScore(row) { return Number(row.dealer_score ?? row.score ?? row.meme_score ?? 0); }
    function firstNumber(...values) { for (const v of values) { const n = Number(v); if (Number.isFinite(n)) return n; } return null; }
    function maxNumber(...values) { const nums = values.map(Number).filter(Number.isFinite); return nums.length ? Math.max(...nums) : null; }
    function scoreReasons(row) {
      const reasons = [];
      const flags = [row.dealer_flags, row.flags, row.heat_flags, row.reasons].flat().filter(Boolean);
      reasons.push(...flags);
      const top10 = Number(row.top10_holder_pct);
      const maxHolder = Number(row.max_holder_pct);
      const oi = Number(row.oi_change_1h_pct);
      const funding = Number(row.funding_rate_pct);
      const age = Number(row.pair_age_hours);
      const heat = Number(row.heat_score);
      if (Number.isFinite(top10) && top10 >= 45) reasons.push(`Top10 ${top10.toFixed(1)}%`);
      if (Number.isFinite(maxHolder) && maxHolder >= 18) reasons.push(`最大钱包 ${maxHolder.toFixed(1)}%`);
      if (Number.isFinite(oi) && oi >= 5) reasons.push(`OI抬头 ${oi.toFixed(1)}%`);
      if (Number.isFinite(funding) && Math.abs(funding) >= 0.05) reasons.push(`费率异常 ${funding.toFixed(3)}%`);
      if (Number.isFinite(age) && age <= 168) reasons.push(`新池 ${(age / 24).toFixed(1)}天`);
      if (row.futures_symbol) reasons.push(`已上合约 ${row.futures_symbol}`);
      if (Number.isFinite(heat) && heat > 0) reasons.push(`热度 ${heat.toFixed(1)}`);
      if (row.risk_level) reasons.push(`风险 ${riskText(row.risk_level)}`);
      return [...new Set(reasons.map(v => String(v).trim()).filter(Boolean))].slice(0, 8);
    }
    function reasonText(row) {
      const reasons = scoreReasons(row);
      return reasons.length ? reasons.map(esc).join(" / ") : "暂无明显触发";
    }
    function signalFor(row) {
      const top10 = firstNumber(row.top10_holder_pct);
      const maxHolder = firstNumber(row.max_holder_pct);
      const oi = firstNumber(row.oi_change_1h_pct);
      const funding = firstNumber(row.funding_rate_pct);
      const mcap = firstNumber(row.market_cap, row.mcap, row.dex_market_cap, row.fdv);
      const volume = maxNumber(row.alpha_volume24h, row.dex_volume24h, row.volume_24h, row.volume24h);
      const liquidity = firstNumber(row.dex_liquidity, row.liquidity_usd, row.liquidity, row.alpha_liquidity);
      const age = firstNumber(row.pair_age_hours);
      const volToMcap = mcap && volume ? volume / mcap : 0;
      const shallow = Boolean(liquidity && mcap && liquidity < Math.max(50000, mcap * 0.02));
      if ((top10 !== null && top10 >= 70) || (maxHolder !== null && maxHolder >= 35)) return { key: "holder_risk", label: "高控盘风险", cls: "signal-risk" };
      if (volToMcap >= 0.25 && (shallow || (age !== null && age <= 168))) return { key: "pump", label: "可疑拉盘", cls: "signal-warning" };
      if ((oi !== null && oi >= 5) || (funding !== null && Math.abs(funding) >= 0.05)) return { key: "futures_move", label: "合约异动", cls: "signal-move" };
      return { key: "watch", label: "观察", cls: "signal-watch" };
    }
    function signalBadge(row) {
      const signal = signalFor(row);
      return `<span class="signal ${signal.cls}">${esc(signal.label)}</span>`;
    }
    function signalRank(row) {
      return ({ holder_risk: 0, pump: 1, futures_move: 2, watch: 3 }[signalFor(row).key] ?? 9);
    }
    function dealerBreakdown(row) {
      const mcap = firstNumber(row.market_cap, row.mcap, row.dex_market_cap, row.fdv);
      const volume = maxNumber(row.alpha_volume24h, row.dex_volume24h, row.volume_24h, row.volume24h);
      const liquidity = firstNumber(row.dex_liquidity, row.liquidity_usd, row.liquidity, row.alpha_liquidity);
      const age = firstNumber(row.pair_age_hours);
      const top10 = firstNumber(row.top10_holder_pct);
      const maxHolder = firstNumber(row.max_holder_pct);
      const oi = firstNumber(row.oi_change_1h_pct);
      const funding = firstNumber(row.funding_rate_pct);
      const volToMcap = mcap && volume ? (volume / mcap) * 100 : null;
      const shallowLine = liquidity && mcap ? (liquidity < Math.max(50000, mcap * 0.02) ? `偏浅，池子 ${money(liquidity)}` : `正常，池子 ${money(liquidity)}`) : (liquidity ? `池子 ${money(liquidity)}` : "缺流动性");
      return [
        ["控盘", top10 !== null ? `Top10 ${top10.toFixed(1)}%${maxHolder !== null ? `，最大钱包 ${maxHolder.toFixed(1)}%` : ""}` : "未接到Top10"],
        ["量市比", volToMcap !== null ? `${volToMcap.toFixed(1)}%` : "缺市值/成交量"],
        ["浅池", shallowLine],
        ["池龄", age !== null ? (age <= 24 ? `新池 ${age.toFixed(1)}小时` : `${(age / 24).toFixed(1)}天`) : "未知"],
        ["合约异动", oi !== null ? `OI${oi >= 0 ? "+" : ""}${oi.toFixed(1)}%` : (row.futures_symbol ? "已上合约，暂无OI" : "未上合约/无OI")],
        ["资金费率", funding !== null ? `${funding.toFixed(3)}%` : "无费率"]
      ];
    }
    function matchesFocus(row, focus) {
      if (!focus) return true;
      if (focus === "holder") return Number(row.top10_holder_pct || 0) >= 45 || Number(row.max_holder_pct || 0) >= 18;
      if (focus === "fresh") return Number.isFinite(Number(row.pair_age_hours)) && Number(row.pair_age_hours) <= 168;
      if (focus === "futures") return Boolean(row.futures_symbol) || Number.isFinite(Number(row.oi_change_1h_pct));
      if (focus === "funding") return Math.abs(Number(row.funding_rate_pct || 0)) >= 0.05;
      return true;
    }
    function matchesSignal(row, signalFilter) {
      return !signalFilter || signalFor(row).key === signalFilter;
    }
    function matchesDangerOnly(row) {
      return !state.dangerOnly || ["holder_risk", "pump", "futures_move"].includes(signalFor(row).key);
    }
    function matchesPinnedOnly(row) {
      return !state.pinnedOnly || isPinned(row);
    }
    function matchesGroupFilter(row, groupFilter) {
      return !groupFilter || groupFor(row) === groupFilter;
    }
    function rawRowsForView() {
      if (state.view === "watchlist") return watchlistRows();
      if (state.view === "review") return reviewRows();
      const key = state.view === "dealer" ? "dealer_rows" : state.view === "meme" ? "meme_rows" : "alpha_rows";
      return [...(state.report?.[key] || [])];
    }
    function rowsForView() {
      let rows = rawRowsForView();
      const q = document.getElementById("search").value.trim().toLowerCase();
      const chain = document.getElementById("chain").value;
      const risk = document.getElementById("risk").value;
      const minScore = Number(document.getElementById("minScore").value || 0);
      const focus = document.getElementById("focus").value;
      const signalFilter = document.getElementById("signalFilter").value;
      const groupFilter = document.getElementById("groupFilter").value;
      rows = rows.filter(row => {
        const hay = [row.symbol, row.name, row.chain, row.dealer_flags, row.reasons, row.sources, scoreReasons(row)].flat().join(" ").toLowerCase();
        return (!q || hay.includes(q)) && (!chain || row.chain === chain || row.chain_id === chain) && (!risk || row.risk_level === risk) && rowScore(row) >= minScore && matchesFocus(row, focus) && matchesSignal(row, signalFilter) && matchesDangerOnly(row) && matchesPinnedOnly(row) && matchesGroupFilter(row, groupFilter);
      });
      rows.sort((a, b) => {
        if (state.sortKey === "priority") {
          const pinDiff = Number(isPinned(b)) - Number(isPinned(a));
          if (pinDiff) return pinDiff;
          const signalDiff = signalRank(a) - signalRank(b);
          return signalDiff || (rowScore(b) - rowScore(a));
        }
        const av = state.sortKey === "score" ? rowScore(a) : a[state.sortKey];
        const bv = state.sortKey === "score" ? rowScore(b) : b[state.sortKey];
        const an = Number(av), bn = Number(bv);
        if (Number.isFinite(an) && Number.isFinite(bn)) return (an - bn) * state.sortDir;
        return String(av ?? "").localeCompare(String(bv ?? ""), "zh-CN") * state.sortDir;
      });
      return rows;
    }
    function renderSignalSummary() {
      const counts = { holder_risk: 0, pump: 0, futures_move: 0, watch: 0 };
      rawRowsForView().forEach(row => { const key = signalFor(row).key; counts[key] = (counts[key] || 0) + 1; });
      document.getElementById("summaryHolderRisk").textContent = counts.holder_risk || 0;
      document.getElementById("summaryPump").textContent = counts.pump || 0;
      document.getElementById("summaryFuturesMove").textContent = counts.futures_move || 0;
      document.getElementById("summaryWatch").textContent = counts.watch || 0;
    }
    function bindSignalSummaryClicks() {
      const signalFilter = document.getElementById("signalFilter");
      document.querySelectorAll(".summary-tile[data-signal]").forEach(tile => {
        tile.onclick = () => {
          signalFilter.value = tile.dataset.signal;
          state.dangerOnly = false;
          const dangerOnly = document.getElementById("dangerOnly");
          dangerOnly.classList.remove("active-filter");
          dangerOnly.textContent = "只看危险";
          renderTable();
        };
      });
    }
    function cellValue(row, key) {
      const v = fieldValue(row, key);
      if (key === "review_time") return v ? esc(new Date(v).toLocaleString("zh-CN")) : "-";
      if (key === "review_outcome") return reviewOutcomeSelect(row);
      if (key === "review_action") return esc(v || "-");
      if (key === "pin") return `<button class="pin-btn ${isPinned(row) ? "active" : ""}" data-pin-key="${esc(rowKey(row))}" type="button">${isPinned(row) ? "已置顶" : "置顶"}</button>`;
      if (key === "group") return groupBadge(row);
      if (key === "watchlist_source") return esc(listSourceLabel(v));
      if (key === "price_usd") return price(v);
      if (["change_m1", "change_m5", "change_h1"].includes(key)) return signedPct(v);
      if (key === "holders") return esc(holdersText(row));
      if (key === "smart_money") return esc(smartMoneyText(row));
      if (key === "kol") return esc(kolText(row));
      if (key === "ai_judgement") return esc(memeAiVerdict(row));
      if (key === "meme_potential") return esc(memePotential(row));
      if (key === "first_seen_change") return esc(firstSeenStats(row));
      if (key === "meme_links") return memeLinks(row);
      if (key === "symbol") return symbolCell(row, v);
      if (key === "signal") return signalBadge(row);
      if (["market_cap", "volume_24h", "liquidity_usd"].includes(key)) return money(v);
      if (["change_m1", "change_m5", "change_h1", "oi_change_1h_pct", "funding_rate_pct", "price_change_24h_pct"].includes(key)) return changePill(v);
      if (["top10_holder_pct"].includes(key)) return pct(v);
      if (key === "risk_level") return riskText(v);
      if (Array.isArray(v)) return esc(v.join(" / "));
      return esc(v ?? "-");
    }
    function bindPinButtons(rows) {
      const byKey = new Map(rows.map(row => [rowKey(row), row]));
      document.querySelectorAll(".pin-btn[data-pin-key]").forEach(btn => {
        btn.onclick = event => {
          event.stopPropagation();
          const row = byKey.get(btn.dataset.pinKey);
          if (row) togglePinned(row);
        };
      });
    }
    function bindReviewOutcomeControls(rows) {
      const byId = new Map(rows.filter(row => row.review_item).map(row => [row.review_id, row.review_item]));
      document.querySelectorAll(".review-outcome[data-review-id]").forEach(select => {
        select.onclick = event => event.stopPropagation();
        select.onchange = event => {
          event.stopPropagation();
          const item = byId.get(select.dataset.reviewId);
          if (!item) return;
          saveReviewOutcome(item, select.value);
        };
      });
    }
    function bindNoteEditor(row) {
      const editor = document.getElementById("noteEditor");
      const button = document.getElementById("saveNote");
      const status = document.getElementById("noteStatus");
      if (!editor || !button || !status) return;
      button.onclick = () => {
        saveNoteFor(row, editor.value);
        status.textContent = "已保存";
      };
      editor.oninput = () => {
        status.textContent = "未保存";
      };
    }
    function bindGroupEditor(row) {
      const editor = document.getElementById("groupEditor");
      if (!editor) return;
      editor.onchange = () => {
        saveGroupFor(row, editor.value);
        renderTable();
        renderDetail(row);
      };
    }
    function renderTable() {
      const title = { dealer: "庄家雷达", meme: "Meme/土狗推荐", alpha: "Alpha 妖币", watchlist: "我的关注池", review: "复盘视图" }[state.view];
      document.getElementById("tableTitle").textContent = title;
      const memeBacktest = document.getElementById("memeBacktestSummary");
      memeBacktest.hidden = state.view !== "meme";
      memeBacktest.textContent = memeBacktestSummary();
      renderSignalSummary();
      const cols = columns[state.view];
      document.getElementById("thead").innerHTML = "<th>#</th>" + cols.map(k => `<th data-key="${k}">${labels[k] || k}</th>`).join("");
      const rows = rowsForView();
      document.getElementById("rowCount").textContent = `${rows.length} 条`;
      const emptyText = state.view === "watchlist" ? "关注池为空：先置顶或分组一个币" : state.view === "review" ? "复盘日志为空：先做一次置顶、分组或备注" : "没有匹配的记录";
      document.getElementById("tbody").innerHTML = rows.length ? rows.map((row, i) => `<tr data-index="${i}"><td>${i + 1}</td>${cols.map(k => `<td class="num">${cellValue(row, k)}</td>`).join("")}</tr>`).join("") : `<tr><td colspan="${cols.length + 1}" class="empty">${emptyText}</td></tr>`;
      bindPinButtons(rows);
      bindReviewOutcomeControls(rows);
      document.querySelectorAll("th[data-key]").forEach(th => th.onclick = () => { const key = th.dataset.key === "dealer_score" ? "score" : th.dataset.key; state.sortDir = state.sortKey === key ? state.sortDir * -1 : -1; state.sortKey = key; renderTable(); });
      document.querySelectorAll("tbody tr[data-index]").forEach(tr => tr.onclick = () => state.view === "review" ? openReviewRow(rows[Number(tr.dataset.index)]) : renderDetail(rows[Number(tr.dataset.index)]));
      if (rows[0]) renderDetail(rows[0]);
    }
    function renderDetail(row) {
      document.getElementById("detail").innerHTML = [["币名", esc(`${row.symbol || "-"} ${row.name ? "/ " + row.name : ""}`)], ["雷达信号", signalBadge(row)], ["链", esc(row.chain || row.chain_id || "-")], ["分数", num(rowScore(row), 1)], ["分数来源", reasonText(row)], ["市值", money(row.market_cap)], ["池子", money(row.liquidity_usd)], ["Dex", row.dex_url || row.url ? `<a href="${esc(row.dex_url || row.url)}" target="_blank" rel="noreferrer">打开</a>` : "-"]].map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("");
      const verdict = Number(row.top10_holder_pct) >= 70 ? "高集中" : Number(row.top10_holder_pct) >= 45 ? "中等集中" : row.top10_holder_pct ? "分散" : "未接到";
      document.getElementById("holderDetail").innerHTML = [["结论", verdict], ["前10钱包", pct(row.top10_holder_pct)], ["前20钱包", pct(row.top20_holder_pct)], ["最大钱包", pct(row.max_holder_pct)], ["特殊钱包", row.holder_contract_count ?? "-"], ["筹码来源", sourceLabel(row.holder_source)]].map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("");
      document.getElementById("dealerDetail").innerHTML = dealerBreakdown(row).concat([
        ["观察分组", groupSelect(row)],
        ["本地备注", `<div class="note-actions"><textarea class="note-editor" id="noteEditor" placeholder="等回踩 / 看OI / 不追高">${esc(noteFor(row))}</textarea><div class="note-row"><button id="saveNote" type="button">保存备注</button><span class="note-status" id="noteStatus">本地保存</span></div></div>`]
      ]).map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("");
      bindGroupEditor(row);
      bindNoteEditor(row);
      renderActionLog(row);
    }
    function renderSources() {
      const by = state.report?.meta?.data_quality?.by_source || {};
      document.getElementById("sources").innerHTML = Object.entries(by).map(([k, v]) => `<span>${sourceLabel(k)}</span><span>实时 ${v.live || 0} / 缓存 ${v.cached || 0} / 失败 ${v.failed || 0}</span>`).join("");
    }
    function initFilters() {
      const chains = new Set();
      ["alpha_rows", "meme_rows", "dealer_rows"].forEach(key => (state.report[key] || []).forEach(row => row.chain && chains.add(row.chain)));
      document.getElementById("chain").innerHTML = `<option value="">全部链</option>` + [...chains].sort().map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
    }
    async function main() {
      const report = await fetch("report.json", { cache: "no-store" }).then(r => r.json());
      state.report = report;
      updateMemeFirstSeen();
      const meta = report.meta || {}, q = meta.data_quality || {};
      document.getElementById("subtitle").textContent = "更新: " + (meta.report_generated_at || meta.generated_at || "-");
      document.getElementById("quality").textContent = ({ live: "实时", cached: "缓存", partial: "部分", failed: "失败" }[q.overall] || q.overall || "-");
      document.getElementById("quality").className = "badge " + (q.overall || "");
      document.getElementById("memeCount").textContent = (report.meme_rows || []).length;
      document.getElementById("alphaCount").textContent = (report.alpha_rows || []).length;
      document.getElementById("dealerCount").textContent = (report.dealer_rows || []).length;
      const holderCoverage = meta.holder_coverage || {};
      document.getElementById("holderCoverage").textContent = `${num(holderCoverage.coverage_pct || 0, 0)}%`;
      document.getElementById("holderCoverage").title = `已接筹码 ${holderCoverage.with_top10 || 0} / ${holderCoverage.unique_tokens || 0}`;
      document.getElementById("liveSources").textContent = Object.values(q.by_source || {}).reduce((n, s) => n + (s.live || 0), 0);
      document.getElementById("errorCount").textContent = q.failed_count || 0;
      initFilters();
      renderTable();
      renderRecommendations();
      renderSources();
      bindSignalSummaryClicks();
      bindLocalStateTools();
      ["search", "chain", "risk", "minScore", "focus", "signalFilter", "groupFilter"].forEach(id => document.getElementById(id).oninput = renderTable);
      const dangerOnly = document.getElementById("dangerOnly");
      dangerOnly.onclick = () => {
        state.dangerOnly = !state.dangerOnly;
        dangerOnly.classList.toggle("active-filter", state.dangerOnly);
        dangerOnly.textContent = state.dangerOnly ? "显示全部" : "只看危险";
        renderTable();
      };
      const pinnedOnly = document.getElementById("pinnedOnly");
      pinnedOnly.onclick = () => {
        state.pinnedOnly = !state.pinnedOnly;
        pinnedOnly.classList.toggle("active-filter", state.pinnedOnly);
        pinnedOnly.textContent = state.pinnedOnly ? "显示全部" : "只看关注";
        renderTable();
      };
      document.querySelectorAll(".tab").forEach(btn => btn.onclick = () => { document.querySelectorAll(".tab").forEach(b => b.classList.remove("active")); btn.classList.add("active"); state.view = btn.dataset.view; state.sortKey = "priority"; state.sortDir = 1; renderTable(); });
    }
    main().catch(err => { document.getElementById("subtitle").textContent = "加载失败: " + err.message; document.getElementById("quality").textContent = "失败"; document.getElementById("quality").className = "badge failed"; });
  </script>
</body>
</html>
"""


def run_frontend_build(command: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"},
        **hidden_subprocess_kwargs(),
    )


def write_vercel_config(out_dir: Path) -> None:
    (out_dir / "vercel.json").write_text(
        json.dumps(
            {
                "cleanUrls": True,
                "headers": [
                    {
                        "source": "/report.json",
                        "headers": [{"key": "Cache-Control", "value": "s-maxage=60, stale-while-revalidate=300"}],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def write_cloud_api_files(out_dir: Path) -> None:
    api_source = Path(__file__).resolve().parent / "vercel_api"
    api_dir = out_dir / "api"
    api_dir.mkdir(parents=True, exist_ok=True)
    for source in api_source.glob("*.js"):
        shutil.copy2(source, api_dir / source.name)
    (out_dir / "package.json").write_text(
        json.dumps(
            {
                "private": True,
                "type": "module",
                "dependencies": {"@vercel/blob": "^2.8.0"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def clear_site_dir(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for child in out_dir.iterdir():
        if child.name == ".vercel":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def copy_dist_to_site(dist_dir: Path, out_dir: Path) -> None:
    clear_site_dir(out_dir)
    for child in dist_dir.iterdir():
        target = out_dir / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def build_react_frontend(report: dict, out_dir: Path) -> None:
    public_dir = WEB_DIR / "public"
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    npm = shutil.which("npm.cmd") or shutil.which("npm") or "npm"
    if not (WEB_DIR / "node_modules").exists():
        run_frontend_build([npm, "install"], WEB_DIR)
    run_frontend_build([npm, "run", "build"], WEB_DIR)
    copy_dist_to_site(WEB_DIR / "dist", out_dir)
    write_vercel_config(out_dir)
    write_cloud_api_files(out_dir)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "README.md").write_text(
        "# Alpha Radar Terminal\n\n"
        "?????? React/Vite ???????????? Alpha Radar report.json?\n",
        encoding="utf-8",
    )


def build_static_fallback(report: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_vercel_config(out_dir)
    write_cloud_api_files(out_dir)
    (out_dir / "README.md").write_text(
        "# Alpha ???? Vercel ??\n\n"
        "??????? `alpha-radar-report-latest.json` ???\n\n"
        "?????????? Vercel ?????\n",
        encoding="utf-8",
    )
    old = out_dir / ".vercel"
    if old.exists() and old.is_dir():
        shutil.rmtree(old)


def merge_fast_track_overlay(report: dict, report_path: Path) -> dict:
    """Embed the latest local quote/execution status in static deployments."""
    overlay_path = report_path.parent / "alpha-fast-track.json"
    if not overlay_path.exists():
        return report
    try:
        overlay = json.loads(overlay_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return report
    if not isinstance(overlay, dict):
        return report
    try:
        from alpha_fast_track import overlay_report
        return overlay_report(report, overlay)
    except Exception:
        return report


def build_site(report_path: Path, out_dir: Path) -> None:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report = merge_fast_track_overlay(report, Path(report_path))
    if (WEB_DIR / "package.json").exists():
        build_react_frontend(report, out_dir)
        return
    build_static_fallback(report, out_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 Alpha 雷达报告生成 Vercel 静态站。")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    build_site(Path(args.report), Path(args.out_dir))
    print(f"已生成 Vercel 站点: {Path(args.out_dir).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
