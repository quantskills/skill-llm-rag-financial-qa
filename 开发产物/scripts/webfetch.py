#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
skill-llm-rag-financial-qa · 全文按需通道（官方披露渠道，可降级）
================================================================================
底仓证据不足 或 问题点名要"原文/条款"时触发：只抓**官方披露平台**——
巨潮资讯网(A股法定披露) / 沪深交易所 / HKEXnews / SEC EDGAR。
**不抓研报/Wind/Choice 等付费源**（版权 + 社区规则 §3 红线）。

adapter 模式（一源一件）：search → fetch → extract → doc records（同底仓 schema，source=web）。
工程纪律：限速、每题抓取配额、失败降级并明示。抓过落 fulltext_cache/ 惰性积累。

⚠️ 巨潮/EDGAR 的具体检索入口需在有网环境实测确定（不同平台反爬策略不同）；
社区版默认 enable_web=False，未启用/失败时返回 []（qa 层据此声明"仅基于结构化字段"降级答案）。
纯离线可测（fixture HTML/PDF 走 extract_text）。免责：仅研究/教育示例。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Optional

# 官方披露渠道白名单（只允许这些域名）
OFFICIAL_HOSTS = {
    "cninfo": "www.cninfo.com.cn",        # 巨潮资讯网（A股法定披露平台）
    "sse": "www.sse.com.cn",              # 上交所
    "szse": "www.szse.cn",                # 深交所
    "hkex": "www1.hkexnews.hk",           # 港交所披露易
    "sec": "www.sec.gov",                 # 美国 SEC EDGAR
}
FETCH_QUOTA = 10          # 每题最多抓取公告数
RATE_LIMIT_SEC = 1.0      # 请求间隔
CACHE_DIR = Path(__file__).resolve().parents[1] / "生产产物" / "fulltext_cache"


def is_official(url: str) -> bool:
    return any(host in str(url) for host in OFFICIAL_HOSTS.values())


# ============================================================================
# 文本抽取（HTML / PDF）—— 纯离线可测
# ============================================================================
def extract_text(content: bytes, is_pdf: bool = False) -> str:
    """从 HTML/PDF 字节流抽正文。PDF 用 pdfminer；HTML 用 bs4。缺库时降级。"""
    if is_pdf:
        try:
            from pdfminer.high_level import extract_text as _pdf
            import io
            return _pdf(io.BytesIO(content)) or ""
        except Exception:  # noqa: BLE001
            return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
    except Exception:  # noqa: BLE001
        return content.decode("utf-8", "ignore") if isinstance(content, bytes) else str(content)


def chunk_by_section(text: str, symbol: str, title: str, limit: int = 512) -> list[str]:
    """按公告章节/条款切（标题/序号锚点），每片带'标题+章节'头部保证自足。"""
    if not text:
        return []
    # 以"一、/（一）/1./第X条"等章节锚点切
    parts = re.split(r"(?=(?:[一二三四五六七八九十]+[、.）]|（[一二三四五六七八九十]+）|第[一二三四五六七八九十\d]+条|\d+[.、]))", text)
    out, buf = [], ""
    for p in parts:
        if len(buf) + len(p) > limit and buf:
            out.append(f"[{symbol} {title}] {buf.strip()}")
            buf = ""
        buf += p
    if buf.strip():
        out.append(f"[{symbol} {title}] {buf.strip()}")
    return out


# ============================================================================
# adapter（search / fetch）—— 需实测确定具体入口；默认降级
# ============================================================================
def search_announcements(symbol: str, query: str, config: dict) -> list[dict]:
    """检索官方披露平台，返回公告元数据 [{symbol,title,url,date}]。
    ⚠️ 具体检索 API 需实测；默认返回 []（未实现/未启用则降级）。"""
    # 落地时按平台实现（如巨潮 cninfo 的公告查询接口）；保持白名单校验。
    return config.get("_stub_announcements", []) if config else []


def fetch_url(url: str, config: dict) -> Optional[tuple[bytes, bool]]:
    """下载 URL 内容（限速 + 白名单 + UA）。返回 (content, is_pdf)。失败/非官方源返回 None。"""
    if not is_official(url):
        return None
    try:
        import requests
        time.sleep(config.get("rate_limit", RATE_LIMIT_SEC))
        headers = {"User-Agent": "quantskills-rag/1.0 (research; contact via repo)"}
        resp = requests.get(url, headers=headers, timeout=config.get("timeout", 15))
        if resp.status_code != 200:
            return None
        return resp.content, url.lower().endswith(".pdf")
    except Exception:  # noqa: BLE001
        return None


def fetch_docs(symbols: list[str], query: str, config: Optional[dict] = None) -> list[dict]:
    """主入口：官方披露全文 → doc records（同底仓 schema）。失败/未启用返回 []（触发降级）。"""
    cfg = config or {}
    if not cfg.get("enable_web"):
        return []
    docs, fetched = [], 0
    for sym in (symbols or []):
        for ann in search_announcements(sym, query, cfg)[:FETCH_QUOTA]:
            if fetched >= FETCH_QUOTA:
                break
            cached = _load_cache(ann.get("url"))
            content_text = cached if cached is not None else None
            if content_text is None:
                got = fetch_url(ann.get("url", ""), cfg)
                if got is None:
                    continue
                content_text = extract_text(got[0], is_pdf=got[1])
                _save_cache(ann.get("url"), content_text)
                fetched += 1
            for j, chunk in enumerate(chunk_by_section(content_text, sym, ann.get("title", ""))):
                docs.append({"doc_id": f"web:{sym}:{ann.get('date')}:{j}", "symbol": sym,
                             "name": ann.get("title", ""), "info_date": str(ann.get("date", "")),
                             "source_api": "web", "doc_type": "fulltext", "field": "section",
                             "text": chunk, "url": ann.get("url"), "numeric_ctx": {}})
    return docs


def _load_cache(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    f = CACHE_DIR / (re.sub(r"\W+", "_", url)[:120] + ".txt")
    return f.read_text() if f.exists() else None


def _save_cache(url: Optional[str], text: str) -> None:
    if not url:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / (re.sub(r"\W+", "_", url)[:120] + ".txt")).write_text(text)


if __name__ == "__main__":
    # 离线自测：extract + chunk（不发真实请求）
    html = "<html><body><h1>x</h1>一、回购方案。二、资金来源为自有资金。三、实施期限12个月。</body></html>".encode("utf-8")
    txt = extract_text(html)
    chunks = chunk_by_section(txt, "000001.SZ", "回购公告")
    print("extract:", txt[:40])
    print("chunks:", len(chunks), "|", chunks[0][:50] if chunks else None)
    print("白名单校验 cninfo:", is_official("http://www.cninfo.com.cn/x"), "| 非官方:", is_official("http://evil.com"))
