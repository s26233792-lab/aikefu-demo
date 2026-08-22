"""RAG 检索层（关键词检索，零额外依赖）。

三个知识源：
- products.json    商品资料（名称/参数/卖点/价格/库存）
- faq.md           FAQ（发货/退换货/尺码/发票等）
- aftersale_rules.md  售后规则（退款/补偿/物流/证据/隐私）

用简单的「关键词命中打分」检索，返回最相关的知识片段，供对应 Agent 引用。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"


def _load_products() -> list[dict]:
    return json.loads((_KNOWLEDGE_DIR / "products.json").read_text(encoding="utf-8"))


def _load_md(name: str) -> str:
    return (_KNOWLEDGE_DIR / name).read_text(encoding="utf-8")


def _split_md_sections(md: str) -> list[dict]:
    """把 markdown 按 `## 标题` 拆成片段，返回 [{title, content}]。"""
    sections: list[dict] = []
    current_title = "概述"
    current_lines: list[str] = []
    for line in md.splitlines():
        if line.startswith("## "):
            if current_lines:
                sections.append({"title": current_title, "content": "\n".join(current_lines).strip()})
            current_title = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append({"title": current_title, "content": "\n".join(current_lines).strip()})
    return sections


def _tokenize(text: str) -> set[str]:
    """简单中文分词：提取连续中文词、英文词、数字词作为 token。"""
    tokens = set(re.findall(r"[a-zA-Z0-9_-]+|[\u4e00-\u9fa5]{1,4}", text.lower()))
    return tokens


def _score(text: str, query_tokens: set[str]) -> float:
    """关键词命中打分：命中 token 数 / 查询 token 数。"""
    text_tokens = _tokenize(text)
    if not query_tokens:
        return 0.0
    hit = len(query_tokens & text_tokens)
    return hit / len(query_tokens)


class RAG:
    """关键词检索 RAG，返回与查询最相关的知识片段。"""

    def __init__(self) -> None:
        self.products = _load_products()
        self.faq_sections = _split_md_sections(_load_md("faq.md"))
        self.aftersale_sections = _split_md_sections(_load_md("aftersale_rules.md"))

    def search_products(self, query: str, top_k: int = 3) -> list[dict]:
        """按查询检索商品，返回最相关的商品列表。"""
        qtokens = _tokenize(query)
        scored = []
        for p in self.products:
            # 拼合商品的所有可检索字段
            haystack = " ".join(
                [p["name"], p.get("category", ""), p.get("material", ""),
                 " ".join(p.get("selling_points", [])), p.get("detail", ""),
                 " ".join(p.get("sizes", []))]
            )
            s = _score(haystack, qtokens)
            scored.append((s, p))
        scored.sort(key=lambda x: -x[0])
        return [p for s, p in scored[:top_k] if s > 0]

    def search_faq(self, query: str, top_k: int = 2) -> list[dict]:
        qtokens = _tokenize(query)
        scored = []
        for sec in self.faq_sections:
            # FAQ 每行 "问：... / 答：..."，把标题也纳入检索
            haystack = sec["title"] + " " + sec["content"]
            s = _score(haystack, qtokens)
            scored.append((s, sec))
        scored.sort(key=lambda x: -x[0])
        return [sec for s, sec in scored[:top_k] if s > 0]

    def search_aftersale(self, query: str, top_k: int = 2) -> list[dict]:
        qtokens = _tokenize(query)
        scored = []
        for sec in self.aftersale_sections:
            haystack = sec["title"] + " " + sec["content"]
            s = _score(haystack, qtokens)
            scored.append((s, sec))
        scored.sort(key=lambda x: -x[0])
        return [sec for s, sec in scored[:top_k] if s > 0]
