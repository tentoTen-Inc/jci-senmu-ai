"""CSV インジェクション対策（app/csv_safe.py）のテスト。"""
from app import csv_safe


def test_cell_prefixes_formula_starts():
    for value in ("=1+1", "+81", "-5", "@SUM(A1)", "\t=1", "\r=1"):
        assert csv_safe.cell(value) == "'" + value
    assert csv_safe.cell("山田 太郎") == "山田 太郎"
    assert csv_safe.cell(-5) == -5 and csv_safe.cell(None) is None


def test_row_and_record():
    assert csv_safe.row(["a", "=b"]) == ["a", "'=b"]
    assert csv_safe.record({"q": "=x", "n": 1}) == {"q": "'=x", "n": 1}
