#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
skill-llm-rag-financial-qa · 渲染层（可视化，artifact 第五节①）
================================================================================
把数字路的季度序列画成"单季度柱状图"——A股困境反转/趋势最直观的一张图：
  · quarterly_series(cache, symbol, metric)  —— PIT(原始公告优先)去重 + 累计→单季拆解
  · render_quarterly_chart(symbol, cache, ...) —— 自包含 HTML（内联 SVG，暗色，零外部依赖）
纯字符串/SVG，零 IO / 零联网，可离线测。免责：仅研究/教育示例。
"""
from __future__ import annotations

import re
from typing import Optional

import pandas as pd

METRIC_ZH = {"is_n_income_attr_p": "归母净利润", "is_total_revenue": "营业总收入", "is_revenue": "营业收入",
             "is_gross_profit": "毛利", "is_operate_profit": "营业利润", "is_basic_eps": "基本每股收益"}


def quarterly_series(cache: pd.DataFrame, symbol: str, metric: str = "is_n_income_attr_p") -> list[dict]:
    """PIT（原始公告 if_adjusted==0 优先）去重后，逐季给 {quarter, cumulative, single}。
    single = 本季累计 − 上季累计（同年）；Q1 累计即单季。"""
    if cache is None or getattr(cache, "empty", True) or metric not in cache.columns:
        return []
    sub = cache[cache["symbol"] == symbol].copy()
    if sub.empty:
        return []
    sub["_pref"] = (pd.to_numeric(sub.get("if_adjusted", 0), errors="coerce").fillna(0) != 0).astype(int)
    piv = (sub.sort_values(["quarter", "_pref", "date"], ascending=[True, True, False])
              .groupby("quarter", as_index=False).first().sort_values("quarter"))
    cum_by = {q: (float(v) if pd.notna(v) else None)
              for q, v in zip(piv["quarter"], pd.to_numeric(piv[metric], errors="coerce"))}
    out = []
    for q in piv["quarter"]:
        cum = cum_by.get(q)
        single = cum
        m = re.match(r"(20\d{2})q([1-4])", str(q))
        if m and int(m.group(2)) > 1:
            pv = cum_by.get(f"{m.group(1)}q{int(m.group(2)) - 1}")
            single = (cum - pv) if (cum is not None and pv is not None) else None
        out.append({"quarter": q, "cumulative": cum, "single": single})
    return out


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if a >= 1e4:
        return f"{v / 1e4:.1f}万"
    return f"{v:.2f}"


def _bars_svg(series: list[dict], w: int = 720, h: int = 300) -> str:
    """单季度柱状图（正绿负红）+ 零轴 + 季度标签 + 值标注。纯 SVG。"""
    pts = [s for s in series if s.get("single") is not None]
    if not pts:
        return "<text x='20' y='40' fill='#9aa'>无可用季度数据</text>"
    ml, mr, mt, mb = 54, 20, 24, 46
    iw, ih = w - ml - mr, h - mt - mb
    vals = [s["single"] for s in pts]
    vmax, vmin = max(vals + [0.0]), min(vals + [0.0])
    span = (vmax - vmin) or 1.0
    y0 = mt + (vmax / span) * ih                       # 零轴 y
    n = len(pts)
    bw = iw / n * 0.62
    gap = iw / n
    parts = [f"<line x1='{ml}' y1='{y0:.1f}' x2='{w - mr}' y2='{y0:.1f}' stroke='#3a4150' stroke-width='1'/>"]
    for i, s in enumerate(pts):
        v = s["single"]
        cx = ml + gap * i + gap * 0.5
        bh = abs(v) / span * ih
        by = y0 - bh if v >= 0 else y0
        col = "#22c55e" if v >= 0 else "#ef4444"
        parts.append(f"<rect x='{cx - bw / 2:.1f}' y='{by:.1f}' width='{bw:.1f}' height='{bh:.1f}' rx='2' fill='{col}' opacity='0.9'/>")
        vy = by - 5 if v >= 0 else by + bh + 13
        parts.append(f"<text x='{cx:.1f}' y='{vy:.1f}' fill='#c8d0dc' font-size='11' text-anchor='middle'>{_fmt(v)}</text>")
        parts.append(f"<text x='{cx:.1f}' y='{h - mb + 18:.1f}' fill='#8b93a2' font-size='11' text-anchor='middle'>{s['quarter']}</text>")
    return f"<svg viewBox='0 0 {w} {h}' width='100%' xmlns='http://www.w3.org/2000/svg' font-family='-apple-system,sans-serif'>{''.join(parts)}</svg>"


def render_quarterly_chart(symbol: str, cache: pd.DataFrame, metric: str = "is_n_income_attr_p",
                           name: str = "", quarters: int = 8) -> str:
    """自包含 HTML：某票某指标的单季度柱状图 + 累计对照表。"""
    series = quarterly_series(cache, symbol, metric)[-quarters:]
    mzh = METRIC_ZH.get(metric, metric)
    svg = _bars_svg(series)
    rows = "".join(f"<tr><td>{s['quarter']}</td><td class='r'>{_fmt(s['cumulative'])}</td>"
                   f"<td class='r' style='color:{'#22c55e' if (s['single'] or 0) >= 0 else '#ef4444'}'>{_fmt(s['single'])}</td></tr>"
                   for s in series)
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{symbol} {mzh} 单季趋势</title><style>
body{{background:#0f1115;color:#e6e6e6;font-family:-apple-system,'PingFang SC',sans-serif;margin:0;padding:20px}}
h1{{font-size:17px;margin:0 0 2px}} .sub{{color:#8b93a2;font-size:12px;margin-bottom:14px}}
.chart{{background:#171a21;border-radius:10px;padding:14px}} .lg{{color:#9aa;font-size:12px;margin:6px 0 0}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:14px}}
td,th{{padding:4px 8px;border-bottom:1px solid #232733;text-align:left}} .r{{text-align:right;font-variant-numeric:tabular-nums}}
th{{color:#8b93a2;font-weight:600;font-size:11px}}
</style></head><body>
<h1>{name or symbol} · {mzh} 单季度趋势</h1>
<div class="sub">单季 = 本季累计 − 上季累计（Q1 累计即单季）。<span style="color:#22c55e">■</span>正 <span style="color:#ef4444">■</span>负。数据源 PandaData（PIT 原始公告口径）。仅研究示例、非投资建议。</div>
<div class="chart">{svg}<div class="lg">柱=单季度 {mzh}</div></div>
<table><tr><th>季度</th><th class="r">累计</th><th class="r">单季</th></tr>{rows}</table>
</body></html>"""


if __name__ == "__main__":
    demo = pd.DataFrame([
        {"symbol": "X.SH", "quarter": f"2025q{q}", "date": f"2025{q*3:02d}15", "if_adjusted": 0,
         "is_n_income_attr_p": v} for q, v in [(1, 1e7), (2, 3e7), (3, 8e7), (4, 1.4e8)]])
    print(quarterly_series(demo, "X.SH"))
    html = render_quarterly_chart("X.SH", demo, name="示例")
    print("HTML 长度:", len(html), "| 含 svg:", "<svg" in html)
