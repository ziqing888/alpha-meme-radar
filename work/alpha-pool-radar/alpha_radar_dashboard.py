#!/usr/bin/env python3
"""
Static dashboard for the read-only Alpha/Meme/Dealer radar.

It reads outputs/alpha-radar-report-latest.json and writes a standalone HTML
file. No server, no account APIs, no trading.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def money(value: Any) -> str:
    amount = to_float(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    for suffix, denom in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if amount >= denom:
            return f"{sign}${amount / denom:.2f}{suffix}"
    return f"{sign}${amount:.0f}"


def pct(value: Any, digits: int = 2) -> str:
    if value is None or value == "":
        return "-"
    return f"{to_float(value):.{digits}f}%"


def age(value: Any) -> str:
    if value is None or value == "":
        return "-"
    hours = to_float(value)
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.0f}d"


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def link(label: Any, url: Any) -> str:
    safe_label = esc(label or "-")
    safe_url = esc(url or "")
    if not safe_url:
        return safe_label
    return f'<a href="{safe_url}" target="_blank" rel="noreferrer">{safe_label}</a>'


def badge(value: Any) -> str:
    text = str(value or "unknown")
    css = text if text in {"live", "cached", "partial", "failed"} else "unknown"
    return f'<span class="badge {css}">{esc(text)}</span>'


def dealer_table(rows: list[dict[str, Any]]) -> str:
    body = []
    for idx, row in enumerate(rows[:30], start=1):
        symbol = row.get("symbol") or row.get("name") or "?"
        url = row.get("dex_url") or row.get("url") or ""
        mcap = row.get("market_cap") if "market_cap" in row else row.get("mcap")
        flags = row.get("dealer_flags") or []
        if isinstance(flags, str):
            flags = [flags]
        body.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{link(symbol, url)}</td>"
            f"<td>{esc(row.get('chain', '-'))}</td>"
            f"<td>{to_float(row.get('dealer_score')):.1f}</td>"
            f"<td>{money(mcap)}</td>"
            f"<td>{money(row.get('dex_liquidity') or row.get('liquidity'))}</td>"
            f"<td>{pct(row.get('top10_holder_pct'))}</td>"
            f"<td>{esc(row.get('risk_level') or '-')}</td>"
            f"<td>{pct(row.get('oi_change_1h_pct'))}</td>"
            f"<td>{pct(row.get('funding_rate_pct'), 4)}</td>"
            f"<td>{esc(' / '.join(map(str, flags[:4])))}</td>"
            "</tr>"
        )
    return table(["#", "币", "链", "庄家分", "市值", "池子", "Top10", "OI 1h", "费率", "原因"], body)


def meme_table(rows: list[dict[str, Any]]) -> str:
    body = []
    for idx, row in enumerate(rows[:30], start=1):
        body.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{link(row.get('symbol') or row.get('name') or '?', row.get('url'))}</td>"
            f"<td>{esc(row.get('chain', '-'))}</td>"
            f"<td>{to_float(row.get('score')):.1f}</td>"
            f"<td>{money(row.get('mcap'))}</td>"
            f"<td>{to_float(row.get('heat_score')):.1f}</td>"
            f"<td>{money(row.get('volume24h'))}</td>"
            f"<td>{pct(row.get('change_m5'), 1)}</td>"
            f"<td>{pct(row.get('change_h1'), 1)}</td>"
            f"<td>{age(row.get('pair_age_hours'))}</td>"
            f"<td>{esc(' / '.join(map(str, (row.get('sources') or [])[:4])))}</td>"
            "</tr>"
        )
    return table(["#", "Token", "Chain", "Score", "MCap", "Heat", "24h Vol", "5m", "1h", "Pool Age", "Sources"], body)


def alpha_table(rows: list[dict[str, Any]]) -> str:
    body = []
    for idx, row in enumerate(rows[:30], start=1):
        body.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{link(row.get('symbol') or '?', row.get('dex_url'))}</td>"
            f"<td>{esc(row.get('chain', '-'))}</td>"
            f"<td>{to_float(row.get('score')):.1f}</td>"
            f"<td>{money(row.get('market_cap'))}</td>"
            f"<td>{money(row.get('alpha_volume24h'))}</td>"
            f"<td>{money(row.get('dex_volume24h'))}</td>"
            f"<td>{pct(row.get('top10_holder_pct'))}</td>"
            f"<td>{esc(row.get('risk_level') or '-')}</td>"
            f"<td>{pct(row.get('oi_change_1h_pct'))}</td>"
            f"<td>{pct(row.get('funding_rate_pct'), 4)}</td>"
            "</tr>"
        )
    return table(["#", "币", "链", "分数", "市值", "Alpha量", "Dex量", "Top10", "OI 1h", "费率"], body)


def table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{esc(item)}</th>" for item in headers)
    body = "".join(rows) if rows else f"<tr><td colspan='{len(headers)}' class='empty'>本轮无数据</td></tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render(report: dict[str, Any]) -> str:
    meta = report.get("meta") or {}
    quality = meta.get("data_quality") or {}
    section_quality = meta.get("section_quality") or {}
    errors = (meta.get("errors") or []) + (meta.get("meme_errors") or [])
    fallback_note = (
        '<div class="notice">Meme 榜使用上一轮有效缓存，本轮 DexScreener profile/boost 数据未完整返回。</div>'
        if meta.get("used_previous_meme")
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Alpha / Meme / Dealer Radar</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b1118;
      --panel: #111a24;
      --line: #263443;
      --text: #e8eef6;
      --muted: #98a6b8;
      --green: #28d39a;
      --amber: #f5b84b;
      --red: #ff667a;
      --blue: #58a6ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font-family: Inter, Segoe UI, Arial, sans-serif; }}
    main {{ width: min(1480px, calc(100vw - 32px)); margin: 24px auto 48px; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-end; margin-bottom: 18px; }}
    h1 {{ font-size: 28px; margin: 0 0 6px; letter-spacing: 0; }}
    h2 {{ font-size: 18px; margin: 0; letter-spacing: 0; }}
    .muted {{ color: var(--muted); }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 14px 0 18px; }}
    .stat, section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
    .stat {{ padding: 12px 14px; }}
    .stat strong {{ display: block; font-size: 22px; margin-top: 4px; }}
    section {{ margin-top: 14px; overflow: hidden; }}
    .section-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 14px 16px; border-bottom: 1px solid var(--line); }}
    .badge {{ display: inline-flex; align-items: center; height: 24px; padding: 0 9px; border-radius: 999px; font-size: 12px; border: 1px solid var(--line); color: var(--muted); }}
    .badge.live {{ color: var(--green); border-color: rgba(40, 211, 154, .45); }}
    .badge.cached {{ color: var(--blue); border-color: rgba(88, 166, 255, .45); }}
    .badge.partial {{ color: var(--amber); border-color: rgba(245, 184, 75, .5); }}
    .badge.failed {{ color: var(--red); border-color: rgba(255, 102, 122, .5); }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 900px; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid rgba(38, 52, 67, .75); text-align: left; white-space: nowrap; font-size: 14px; }}
    th {{ color: var(--muted); font-weight: 600; }}
    td:first-child, th:first-child {{ width: 52px; color: var(--muted); }}
    a {{ color: var(--text); text-decoration: none; }}
    a:hover {{ color: var(--blue); }}
    .empty {{ color: var(--muted); text-align: center; padding: 28px; }}
    .notice {{ margin: 14px 0; padding: 12px 14px; border: 1px solid rgba(245, 184, 75, .45); background: rgba(245, 184, 75, .08); color: var(--amber); border-radius: 8px; }}
    details {{ margin-top: 14px; color: var(--muted); }}
    summary {{ cursor: pointer; }}
    pre {{ white-space: pre-wrap; background: #071019; border: 1px solid var(--line); padding: 12px; border-radius: 8px; max-height: 260px; overflow: auto; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} header {{ display: block; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Alpha / Meme / Dealer Radar</h1>
      <div class="muted">只读雷达 · 更新时间 {esc(meta.get('report_generated_at') or meta.get('generated_at') or '-')}</div>
    </div>
    <div>{badge(quality.get('overall'))}</div>
  </header>

  <div class="grid">
    <div class="stat"><span class="muted">Meme 候选</span><strong>{esc(meta.get('meme_count', 0))}</strong></div>
    <div class="stat"><span class="muted">Alpha 候选</span><strong>{esc(meta.get('filtered_count', 0))}</strong></div>
    <div class="stat"><span class="muted">庄家代理候选</span><strong>{esc(meta.get('dealer_count', 0))}</strong></div>
    <div class="stat"><span class="muted">缓存/失败请求</span><strong>{esc(quality.get('cached_count', 0))}/{esc(quality.get('failed_count', 0))}</strong></div>
  </div>
  {fallback_note}

  <section>
    <div class="section-head"><h2>庄家雷达代理榜</h2>{badge(section_quality.get('dealer'))}</div>
    <div class="table-wrap">{dealer_table(report.get('dealer_rows') or [])}</div>
  </section>

  <section>
    <div class="section-head"><h2>Meme 推荐榜</h2>{badge(section_quality.get('meme'))}</div>
    <div class="table-wrap">{meme_table(report.get('meme_rows') or [])}</div>
  </section>

  <section>
    <div class="section-head"><h2>Alpha 妖币榜</h2>{badge(section_quality.get('alpha'))}</div>
    <div class="table-wrap">{alpha_table(report.get('alpha_rows') or [])}</div>
  </section>

  <details>
    <summary>数据源错误 ({len(errors)})</summary>
    <pre>{esc(chr(10).join(map(str, errors[:80])) or '无')}</pre>
  </details>
</main>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build static Alpha radar dashboard.")
    parser.add_argument("--out-dir", default="outputs")
    parser.add_argument("--input", default="alpha-radar-report-latest.json")
    parser.add_argument("--output", default="alpha-radar-dashboard.html")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    report_path = out_dir / args.input
    output_path = out_dir / args.output
    report = json.loads(report_path.read_text(encoding="utf-8"))
    output_path.write_text(render(report), encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
