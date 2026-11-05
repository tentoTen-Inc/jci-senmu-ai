"""メールのレイアウト記法 → HTML とプレーンテキスト（docs/meeting-notice-design.md §4.3）。

行の頭に `[右]` `[中]` `[題]`（中央・大きく・太字）`[字下げ]` を書くと、その行の寄せ方になる。
何もなければ左寄せ。空の行は空の行のまま。
行の中の `[文字](https://…)` はリンクになり、`https://…` もそのままリンクになる。
差し込んだ後の本文を変換する（差し込む値は HTML として扱わない）。
"""
from __future__ import annotations

import html
import re

from .models import EmailStyle

MARKS = {"[右]": "right", "[中]": "center", "[題]": "title", "[字下げ]": "indent"}
#: [文字](URL) か、そのままの URL
LINK = re.compile(r"\[([^\[\]\n]+)\]\((https://[^\s()]+)\)|(https://[^\s<>「」（）、。]+)")
#: 題（[題]）の文字の大きさ（本文の倍率）
TITLE_SCALE = 1.8


def _split(line: str) -> tuple[str | None, str]:
    for mark, kind in MARKS.items():
        if line.startswith(mark):
            return kind, line[len(mark):].lstrip(" ")
    return None, line


def _inline_html(text: str) -> str:
    out, pos = [], 0
    for m in LINK.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        label, url, bare = m.group(1), m.group(2), m.group(3)
        href = url or bare
        out.append(f'<a href="{html.escape(href, quote=True)}">{html.escape(label or bare)}</a>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _inline_text(text: str) -> str:
    return LINK.sub(lambda m: f"{m.group(1)}（{m.group(2)}）" if m.group(1) else m.group(3), text)


def to_html(body: str, style: EmailStyle | None = None) -> str:
    """Google ドキュメントから貼った形に近い HTML（行ごとの段落・行間 1.2）。"""
    style = style or EmailStyle()
    font = re.sub(r"[^\w \-]", "", style.font_family) or "Arial"  # 設定の値。記号は入れない
    size = style.font_size
    paras = []
    for line in body.split("\n"):
        kind, text = _split(line)
        if not text.strip():
            paras.append("<br>")
            continue
        p_css = "line-height:1.2;margin-top:0pt;margin-bottom:0pt"
        span_css = (f"font-size:{size}pt;font-family:'{font}',sans-serif;color:rgb(0,0,0);"
                    "white-space:pre-wrap")
        if kind == "right":
            p_css += ";text-align:right"
        elif kind == "center":
            p_css += ";text-align:center"
        elif kind == "title":
            p_css += ";text-align:center"
            span_css = span_css.replace(f"font-size:{size}pt",
                                        f"font-size:{round(size * TITLE_SCALE)}pt") + \
                ";font-weight:700"
        elif kind == "indent":
            p_css += f";text-indent:{size}pt"
        paras.append(f'<p dir="ltr" style="{p_css}"><span style="{span_css}">'
                     f"{_inline_html(text)}</span></p>")
    return '<div dir="ltr">' + "".join(paras) + "</div>"


def to_text(body: str) -> str:
    """HTML を読めないメールソフト向けの代替本文（記法を外し、リンクは URL を添える）。"""
    lines = []
    for line in body.split("\n"):
        kind, text = _split(line)
        text = _inline_text(text)
        if kind == "indent":
            text = "　" + text
        lines.append(text)
    return "\n".join(lines)
