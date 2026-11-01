"""LINE で送る資料リンクを外部（アプリ・ブラウザ）で開かせる書き換えのテスト。"""
from linebot.v3.messaging import FlexContainer, FlexMessage, TextMessage, TextMessageV2

from app.doc_links import externalize, externalize_text

DOC = "https://docs.google.com/document/d/abc_DEF-1/edit"


def test_doc_and_drive_urls_open_outside_line():
    text = (f"議案書を作りました。\n{DOC}\n"
            "出典: https://drive.google.com/open?id=xyz（Google ドライブ）")
    assert externalize_text(text) == (
        f"議案書を作りました。\n{DOC}?openExternalBrowser=1\n"
        "出典: https://drive.google.com/open?id=xyz&openExternalBrowser=1（Google ドライブ）")


def test_fragment_forms_and_other_urls_are_kept():
    assert externalize_text(f"{DOC}#heading=h.1") == f"{DOC}?openExternalBrowser=1#heading=h.1"
    form = "https://docs.google.com/forms/d/e/1/viewform"
    assert externalize_text(form) == form
    assert externalize_text("https://admin.example.org") == "https://admin.example.org"
    once = externalize_text(DOC)
    assert externalize_text(once) == once


def test_messages_text_and_flex_buttons_are_rewritten():
    bubble = {"type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": [
        {"type": "button", "action": {"type": "uri", "label": "議案書を開く", "uri": DOC}}]}}
    plain = TextMessage(text="こんにちは")
    flex = FlexMessage(alt_text="x", contents=FlexContainer.from_dict(bubble))
    out = externalize([TextMessage(text=DOC), flex, TextMessageV2(text=f"資料 {DOC}"), plain])
    assert out[0].text == f"{DOC}?openExternalBrowser=1"
    button = out[1].to_dict()["contents"]["body"]["contents"][0]
    assert button["action"]["uri"] == f"{DOC}?openExternalBrowser=1"
    assert out[2].text == f"資料 {DOC}?openExternalBrowser=1"
    assert out[3] is plain
