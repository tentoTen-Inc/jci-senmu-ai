"""資料フォルダに紐づく議案（予算書など）の読み取りとチェック（docs/folder-proposal-check-design.md）。

作業文書（doc_id）を持たない議案は、資料の場所（storage_uri）の Drive フォルダかファイルだけを持つ。
そのフォルダ直下の Google ドキュメントとスプレッドシートを drive-reader で読み（書き込みはしない）、
台帳の本文に写し、スプレッドシートの数式のエラー・古い年の表記・合計の手入力・文字として入った数字を調べる。
スプレッドシートは金額にかかわるので自動では直さない。

`_get_json` / `drive.export_text` / `drive._token` がテストのモック境界。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import drive

GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
#: 台帳の本文の上限（AI の確認に渡す量。超えたら切って、切ったことを書く）
CONTENT_LIMIT = 60_000
#: 1つのチェックで、ファイル・シートごとに挙げるセルの数（ほかは「ほか N か所」）
MAX_CELLS = 5

_FOLDER_URL = re.compile(r"/folders/([A-Za-z0-9_-]+)")
_FILE_URL = re.compile(r"/(?:d|file/d)/([A-Za-z0-9_-]+)")
_ERRORS = ("#REF!", "#DIV/0!", "#VALUE!", "#N/A", "#NAME?", "#NUM!", "#ERROR!", "#NULL!")
#: 「2027年1月1日から」の期間
_PERIOD = re.compile(r"(?<!\d)(20\d{2})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*から")
_YEAR = re.compile(r"(?<!\d)(20\d{2})\s*年")
#: 合計の行（「会計」の「計」は除く）
_TOTAL = re.compile(r"小計|合計|(?<!会)計")
#: 文字として入った数字（全角数字・桁区切り・「円」付き）
_TEXT_NUMBER = re.compile(
    r"^[\s¥￥]*[-−△▲(（]?[0-9０-９][0-9０-９,，]*(?:[.．][0-9０-９]+)?[)）]?\s*円?\s*$")
_FULLWIDTH_DIGIT = re.compile(r"[０-９，]")
_FISCAL_YEAR = re.compile(r"(20\d{2})\s*年度")


@dataclass
class Sheet:
    title: str
    values: list[list[str]]  # 表示どおりの値
    formulas: list[list]  # 数式（数式でなければ値。数字は数字のまま）


@dataclass
class SourceFile:
    file_id: str
    name: str
    kind: str  # "sheet" | "doc"
    url: str
    sheets: list[Sheet] = field(default_factory=list)
    text: str = ""


@dataclass
class FolderRead:
    files: list[SourceFile] = field(default_factory=list)
    error: str | None = None


def parse_location(uri: str | None) -> tuple[str, str] | None:
    """資料の場所の URL から ("folder" | "file", ID)。Drive の URL でなければ None。"""
    if not uri or "google.com" not in uri:
        return None
    if match := _FOLDER_URL.search(uri):
        return "folder", match.group(1)
    if match := _FILE_URL.search(uri):
        return "file", match.group(1)
    return None


def _get_json(url: str, token: str) -> dict:
    """GET して JSON を返す。テストでモックする境界。"""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _url(kind: str, file_id: str) -> str:
    if kind == "sheet":
        return f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
    return f"https://docs.google.com/document/d/{file_id}/edit"


def _files(location: tuple[str, str], token: str) -> list[dict]:
    """読むファイル（フォルダならその直下のドキュメントとスプレッドシート。名前の順）。"""
    kind, target = location
    common = {"supportsAllDrives": "true"}
    if kind == "file":
        params = {**common, "fields": "id,name,mimeType"}
        return [_get_json(f"{DRIVE_FILES}/{target}?{urllib.parse.urlencode(params)}", token)]
    params = {
        **common,
        "q": (f"'{target}' in parents and trashed=false and "
              f"(mimeType='{GOOGLE_DOC}' or mimeType='{GOOGLE_SHEET}')"),
        "fields": "files(id,name,mimeType)", "pageSize": 100, "orderBy": "name",
        "includeItemsFromAllDrives": "true",
    }
    return _get_json(f"{DRIVE_FILES}?{urllib.parse.urlencode(params)}", token).get("files", [])


def _sheets(file_id: str, token: str) -> list[Sheet]:
    meta = _get_json(f"{SHEETS_API}/{file_id}?fields=sheets.properties.title", token)
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if not titles:
        return []

    def batch(render: str) -> list[list[list]]:
        params = [("ranges", f"'{t}'") for t in titles] + [("valueRenderOption", render)]
        data = _get_json(f"{SHEETS_API}/{file_id}/values:batchGet?"
                         f"{urllib.parse.urlencode(params)}", token)
        return [r.get("values", []) for r in data.get("valueRanges", [])]

    values, formulas = batch("FORMATTED_VALUE"), batch("FORMULA")
    return [Sheet(t, v, f) for t, v, f in zip(titles, values, formulas, strict=False)]


def read(uri: str | None) -> FolderRead:
    """資料の場所のドキュメントとスプレッドシートを読む。読めなければ error に理由を入れる。"""
    location = parse_location(uri)
    if location is None:
        return FolderRead(
            error="資料の場所（Drive のフォルダかファイルの URL）が設定されていません。")
    try:
        token = drive._token()
        out = []
        for f in _files(location, token):
            if f.get("mimeType") == GOOGLE_SHEET:
                out.append(SourceFile(f["id"], f["name"], "sheet", _url("sheet", f["id"]),
                                      sheets=_sheets(f["id"], token)))
            elif f.get("mimeType") == GOOGLE_DOC:
                out.append(SourceFile(f["id"], f["name"], "doc", _url("doc", f["id"]),
                                      text=drive.export_text(f["id"], token)))
    except Exception as exc:  # noqa: BLE001 - 共有されていない・消えたなど。理由を画面に出す
        return FolderRead(error=f"資料を読み取れませんでした（共有されていないか、場所が"
                                f"変わった可能性があります）: {type(exc).__name__}")
    if not out:
        return FolderRead(error="資料の場所に、Google ドキュメントもスプレッドシートもありません。")
    return FolderRead(files=out)


# --------------------------------------------------------------------------- #
# 本文への写し（§3）
# --------------------------------------------------------------------------- #
def _sheet_text(sheet: Sheet) -> str:
    lines = []
    for row in sheet.values:
        cells = [str(c).replace("\n", " ").strip() for c in row]
        cells = [c for c in cells if c]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def to_content(files: list[SourceFile], limit: int = CONTENT_LIMIT) -> str:
    """ファイルごと・シートごとに見出しを付けた本文。長すぎるときは切る。"""
    parts = []
    for f in files:
        if f.kind == "doc":
            parts.append(f"【{f.name}】\n{f.text.strip()}")
        for sheet in f.sheets:
            text = _sheet_text(sheet)
            if text:
                parts.append(f"【{f.name}／{sheet.title}】\n{text}")
    content = "\n\n".join(parts)
    if len(content) > limit:
        content = content[:limit] + "\n\n（資料が長いため、ここから後は省略しました）"
    return content


# --------------------------------------------------------------------------- #
# チェック（§4）
# --------------------------------------------------------------------------- #
def fiscal_year(title: str, fallback: int) -> int:
    """議案の年度（議案名の「2027年度」。なければ fallback）。"""
    match = _FISCAL_YEAR.search(title or "")
    return int(match.group(1)) if match else fallback


def _cell_name(row: int, col: int) -> str:
    letters, n = "", col + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return f"{letters}{row + 1}"


def _at(grid: list[list], row: int, col: int):
    return grid[row][col] if row < len(grid) and col < len(grid[row]) else ""


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _row_label(values: list[str], formulas: list) -> str:
    """行の項目名（最初の数字より左にある文字のセル）。"""
    for value, formula in zip(values, formulas + [""] * len(values), strict=False):
        if _is_number(formula):
            return ""
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _sheet_findings(sheet: Sheet, year: int) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {"error": [], "year": [], "total": [], "text_number": []}
    rows = max(len(sheet.values), len(sheet.formulas))
    for r in range(rows):
        values = sheet.values[r] if r < len(sheet.values) else []
        formulas = sheet.formulas[r] if r < len(sheet.formulas) else []
        total_row = bool(_TOTAL.search(_row_label(values, list(formulas))))
        for c in range(max(len(values), len(formulas))):
            value, formula = str(_at(sheet.values, r, c)), _at(sheet.formulas, r, c)
            name = _cell_name(r, c)
            if value.strip() in _ERRORS:
                found["error"].append(name)
                continue
            if _is_number(formula):
                if total_row and formula != 0:
                    found["total"].append(name)
                continue
            if not value.strip():
                continue
            period = [int(y) for y in _PERIOD.findall(value)]
            years = [int(y) for y in _YEAR.findall(value)]
            if any(y != year for y in period) or any(y < year - 1 for y in years):
                found["year"].append(name)
            elif (isinstance(formula, str) and not formula.startswith("=")
                  and _TEXT_NUMBER.match(value)
                  and (_FULLWIDTH_DIGIT.search(value) or "," in value or "円" in value)):
                found["text_number"].append(name)
    return found


_MESSAGES = {
    "error": "数式のエラー（#REF! など）があります",
    "year": "古い年の表記があります（議案の年度は{year}年度）",
    "total": "合計の行に、数式でなく数字が入っています（直したときに合計がずれます）",
    "text_number": "数字が文字として入っていて、合計に入りません（全角数字・「円」付きなど）",
}


def _cells(names: list[str]) -> str:
    shown = "・".join(names[:MAX_CELLS])
    return shown + (f" ほか{len(names) - MAX_CELLS}か所" if len(names) > MAX_CELLS else "")


def check(files: list[SourceFile], year: int) -> list[str]:
    """スプレッドシートの直す点（ファイル名・シート名・セル番地つき）。"""
    issues = []
    for f in files:
        for sheet in f.sheets:
            for kind, names in _sheet_findings(sheet, year).items():
                if names:
                    message = _MESSAGES[kind].format(year=year)
                    issues.append(f"{message}: {f.name}／{sheet.title} {_cells(names)}")
    return issues


def doc_text(files: list[SourceFile]) -> str:
    """ドキュメントの部分の本文（表記のチェックに使う。スプレッドシートの様式の文字は含めない）。"""
    return "\n\n".join(f.text for f in files if f.kind == "doc")
