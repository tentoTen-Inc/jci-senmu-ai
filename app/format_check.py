"""議案の形式チェック（docs/dashboard-design.md §3.2/§6）。

- 事業計画書の必須項目（見出し）の欠落検出
- 表記規定：英数字・記号は半角（全角英数記号の混入検出）
LLM を使わない決定的チェック。F6-4。
"""
from __future__ import annotations

import re

from .models import FormatCheckResult, Proposal

# 事業計画書テンプレートの必須見出し（2024様式より抜粋）
REQUIRED_SECTIONS_BY_DOCTYPE: dict[str, list[str]] = {
    "事業計画書": [
        "事業名",
        "事業実施に至る背景",
        "事業の対象者",
        "事業目的",
        "KPI",
        "実施日時",
        "実施場所",
        "予算",
        "事業内容",
        "討議",  # 討議・協議・審議のポイント
    ],
}

# 全角英数（Ａ-Ｚ ａ-ｚ ０-９）と代表的な全角記号
_FULLWIDTH_ALNUM = re.compile(r"[Ａ-Ｚａ-ｚ０-９]")
_FULLWIDTH_SYMBOLS = "％＆＠＃＋－＝／（）！？．，：；"
#: 半角にする文字（どれも U+FF01〜U+FF5E。U+FEE0 を引くと半角になる。doc_format で直す）
FULLWIDTH_CHARS = frozenset(
    [chr(c) for c in range(ord("Ａ"), ord("Ｚ") + 1)]
    + [chr(c) for c in range(ord("ａ"), ord("ｚ") + 1)]
    + [chr(c) for c in range(ord("０"), ord("９") + 1)]
    + list(_FULLWIDTH_SYMBOLS)
)


def check_required_sections(content: str, doc_type: str) -> list[str]:
    required = REQUIRED_SECTIONS_BY_DOCTYPE.get(doc_type, [])
    missing = [s for s in required if s not in content]
    return [f"必須項目が見当たりません: {s}" for s in missing]


def check_fullwidth(content: str) -> list[str]:
    issues: list[str] = []
    alnum = sorted(set(_FULLWIDTH_ALNUM.findall(content)))
    if alnum:
        issues.append(f"全角英数字が使われています（半角に統一）: {' '.join(alnum)}")
    symbols = sorted({c for c in content if c in _FULLWIDTH_SYMBOLS})
    if symbols:
        issues.append(f"全角記号が使われています（半角に統一）: {' '.join(symbols)}")
    return issues


def run_format_check(proposal: Proposal) -> FormatCheckResult:
    issues: list[str] = []
    content = proposal.content or ""
    if not content.strip():
        issues.append("本文(content)が空です。")
    else:
        issues += check_required_sections(content, proposal.doc_type)
        issues += check_fullwidth(content)
    return FormatCheckResult(passed=len(issues) == 0, issues=issues)
