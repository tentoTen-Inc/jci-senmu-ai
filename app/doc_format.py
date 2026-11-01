"""議案書（Google ドキュメント）の体裁の点検と自動修正（docs/proposal-ledger-design.md §11）。

内容は変えず、体裁だけを直す。

- フォント: 本文のすべての文字を決まったフォント（既定は Arial）にそろえる
- 文字の大きさ: 表の中で大きさの指定がない文字（貼り付けなどで既定の 11pt になったもの）を、
  同じ行の項目名の大きさにそろえる（7pt の年間スケジュールのように、指定してある大きさは変えない）
- 全角の英数字・記号: 記入欄（読み取り専用でない項目の内容）だけ半角にする。
  項目名などテンプレートの文字は変えない

数字の誤りや必須項目の抜けのように、内容にかかわるものは直さない（委員会で直す）。
LLM を使わない決定的な処理。読み書きは committee_docs（doc-editor の権限借用）を使う。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import committee_docs
from .format_check import FULLWIDTH_CHARS

#: Google ドキュメントの既定のフォント（名前付きスタイルで指定がないとき）
DOCS_DEFAULT_FONT = "Arial"


def doc_font() -> str:
    """議案書のフォント（DOC_FONT。未設定なら Arial）。"""
    return os.environ.get("DOC_FONT", "").strip() or DOCS_DEFAULT_FONT


def to_halfwidth(text: str) -> str:
    return "".join(chr(ord(c) - 0xFEE0) if c in FULLWIDTH_CHARS else c for c in text)


@dataclass
class _Run:
    start: int
    end: int
    text: str
    style: dict


@dataclass
class FormatFix:
    """体裁の直し。requests は batchUpdate にそのまま渡す。"""

    fonts: dict[str, int] = field(default_factory=dict)  # 直す前のフォント → か所数
    sizes: int = 0  # 大きさをそろえる か所数
    halfwidth: list[str] = field(default_factory=list)  # 半角にする文字（直す前）
    requests: list[dict] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.requests

    def summary(self, font: str) -> list[str]:
        """直したことの説明（画面と LINE の知らせに使う）。"""
        lines = []
        if self.fonts:
            before = "・".join(sorted(self.fonts))
            lines.append(f"フォントを {font} にそろえました（{before} → {font}、"
                         f"{sum(self.fonts.values())}か所）")
        if self.sizes:
            lines.append(f"文字の大きさを項目名にそろえました（{self.sizes}か所）")
        if self.halfwidth:
            chars = " ".join(f"{c}→{to_halfwidth(c)}" for c in sorted(set(self.halfwidth)))
            lines.append(f"全角の英数字・記号を半角にしました（{chars}、{len(self.halfwidth)}文字）")
        return lines


def _runs(paragraph: dict) -> list[_Run]:
    out = []
    for e in paragraph.get("elements", []):
        run = e.get("textRun")
        if run is None or "startIndex" not in e:
            continue
        out.append(_Run(e["startIndex"], e["endIndex"], run.get("content", ""),
                        run.get("textStyle", {})))
    return out


def _cell_runs(cell: dict) -> list[_Run]:
    """セルの中の文字（セルの中の表も含む）。"""
    out = []
    for el in cell.get("content", []):
        if "paragraph" in el:
            out += _runs(el["paragraph"])
        for row in el.get("table", {}).get("tableRows", []):
            for c in row.get("tableCells", []):
                out += _cell_runs(c)
    return out


def _all_runs(content: list[dict]) -> list[_Run]:
    return _cell_runs({"content": content})


def _size(run: _Run) -> dict | None:
    return run.style.get("fontSize")


def _row_size_fixes(content: list[dict]) -> list[tuple[_Run, dict]]:
    """表の中で大きさの指定がない文字と、そろえる大きさ（同じ行の項目名の大きさ）。"""
    out: list[tuple[_Run, dict]] = []
    for el in content:
        for row in el.get("table", {}).get("tableRows", []):
            cells = row.get("tableCells", [])
            for c in cells:  # セルの中の表を先に見る
                out += _row_size_fixes(c.get("content", []))
            if not cells:
                continue
            label = next((_size(r) for r in _cell_runs(cells[0]) if r.text.strip() and _size(r)),
                         None)
            if label is None:
                continue
            for c in cells[1:]:
                for el2 in c.get("content", []):
                    for r in _runs(el2.get("paragraph", {})):
                        if r.text.strip() and _size(r) is None:
                            out.append((r, label))
    return out


def _named_font(tab: dict) -> str:
    for s in tab.get("namedStyles", {}).get("styles", []):
        if s.get("namedStyleType") == "NORMAL_TEXT":
            family = s.get("textStyle", {}).get("weightedFontFamily", {}).get("fontFamily")
            return family or DOCS_DEFAULT_FONT
    return DOCS_DEFAULT_FONT


def _first_tab(doc: dict) -> dict:
    tabs = doc.get("tabs") or []
    return tabs[0].get("documentTab", {}) if tabs else doc


def _text_style_request(start: int, end: int, style: dict, fields: str) -> dict:
    return {"updateTextStyle": {"range": {"startIndex": start, "endIndex": end},
                                "textStyle": style, "fields": fields}}


def _halfwidth_requests(runs: list[_Run], editable: list[tuple[int, int]]
                        ) -> tuple[list[str], list[dict]]:
    """記入欄の全角英数字・記号を半角に置き換える（文字数は変わらないので位置はずれない）。

    置き換えた文字は、もとの文字の書式に戻す（挿入した文字は前の文字の書式を引き継ぐため）。
    """
    chars: list[str] = []
    spans: list[tuple[int, str, dict]] = []  # (開始位置, もとの文字列, 書式)
    for run in runs:
        index = run.start
        for c in run.text:
            width = len(c.encode("utf-16-le")) // 2
            if c in FULLWIDTH_CHARS and any(s <= index < e for s, e in editable):
                chars.append(c)
                last = spans[-1] if spans else None
                if last and last[0] + len(last[1]) == index and last[2] is run.style:
                    spans[-1] = (last[0], last[1] + c, last[2])
                else:
                    spans.append((index, c, run.style))
            index += width
    requests: list[dict] = []
    for start, text, style in sorted(spans, key=lambda s: s[0], reverse=True):
        end = start + len(text)
        requests += [
            {"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}},
            {"insertText": {"location": {"index": start}, "text": to_halfwidth(text)}},
            _text_style_request(start, end, style, "*"),
        ]
    return chars, requests


def plan_fix(doc: dict, font: str | None = None) -> FormatFix:
    """ドキュメント（documents.get の結果）の体裁の直しを組み立てる。"""
    font = font or doc_font()
    tab = _first_tab(doc)
    content = tab.get("body", {}).get("content", [])
    runs = [r for r in _all_runs(content) if r.text.strip()]
    fix = FormatFix()

    editable = [(f.start, f.end) for f in committee_docs.extract_fields(doc) if f.editable]
    fix.halfwidth, fix.requests = _halfwidth_requests(runs, editable)

    # 書式の直しは置き換えの後に送る（置き換えで文字数は変わらないので、位置はそのまま使える）
    default_font = _named_font(tab)
    for r in runs:
        family = r.style.get("weightedFontFamily", {})
        current = family.get("fontFamily") or default_font
        if current == font:
            continue
        fix.fonts[current] = fix.fonts.get(current, 0) + 1
        weighted = {"fontFamily": font}
        if family.get("weight"):
            weighted["weight"] = family["weight"]
        fix.requests.append(_text_style_request(
            r.start, r.end, {"weightedFontFamily": weighted}, "weightedFontFamily"))
    for r, size in _row_size_fixes(content):
        fix.sizes += 1
        fix.requests.append(_text_style_request(r.start, r.end, {"fontSize": size}, "fontSize"))
    return fix


def fix_document(doc_id: str, token: str, font: str | None = None) -> tuple[FormatFix, dict]:
    """文書を読み、体裁を直す。直した内容と、直した後の文書を返す（直す点がなければ読んだまま）。"""
    doc = committee_docs.read_doc(doc_id, token)
    fix = plan_fix(doc, font)
    if fix.empty:
        return fix, doc
    committee_docs.apply(doc_id, doc["revisionId"], fix.requests, token)
    return fix, committee_docs.read_doc(doc_id, token)
