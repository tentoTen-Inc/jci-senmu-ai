# コントリビュートの手引き

本プロジェクトへの参加を歓迎します。不具合の報告、機能の提案、ドキュメントの改善、Pull Request のどれでも助かります。
やり取りは日本語で行います。

## はじめる前に

- 脆弱性は Issue に書かず、[SECURITY.md](SECURITY.md) の方法で知らせてください。
- GitHub のアカウントがない方は、[ご意見フォーム](https://docs.google.com/forms/d/e/1FAIpQLSeNLtWm7ROaQBxM1acLK0tuvkFAP1HMK8vWrHvWwsnBk7r_HQ/viewform) から送れます（開発元が確認して Issue にします）。
- 大きめの変更（新機能、データの持ち方の変更）は、先に Issue で相談してください。
  本プロジェクトでは、新機能は**設計書（`docs/*-design.md`）→ Issue → Pull Request** の順で進めています。

## 開発環境

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

Pull Request を出す前に、次がすべて通ることを確認してください（CI でも同じものを実行します）。

```bash
.venv/bin/ruff check .
.venv/bin/pytest
cd web && npm ci && npm run build   # 管理画面を変えた場合
```

LIFF の画面（`app/static/*.js`）を変えた場合は `node --check` で構文を確認してください。

## 書き方の決まり

### コード

- Python 3.12、行の長さは 100 文字まで（ruff の設定は `pyproject.toml`）。
- コメント・docstring は日本語で、「なぜそうしているか」を書きます。
- 外部サービス（LINE・Firestore・Google API・Vertex AI）はテストで呼ばないでください。
  リポジトリはインメモリ実装（`app/repository.py` の `InMemoryRepository`）、外部 API はモックに置き換えます。
- 時刻は日本時間（JST）で扱います。

### 他の LOM でも使えるように

本プロジェクトは全国の LOM で使えることを目指しています。

- **LOM 固有の値を直書きしない**でください（LOM 名、会員名、メールアドレス、ドメイン、LINE のグループ ID、ドライブやカレンダーの ID など）。
  環境変数か Firestore の設定に置きます（[docs/configuration.md](docs/configuration.md)）。
- **実在の個人情報をコミットしない**でください。テストやサンプルには架空の名前・値を使います。
- 新しい設定項目を足したら、`docs/configuration.md` と `.env.example` も更新してください。

### 会員を守るための決まり

LINE への送信は取り消せず、相手は LOM の会員です。次の決まりを崩す変更は受け付けられません。

- 自由文のチャットからはデータを書き換えない（検索・回答のみ）。書き込みはボタン（リッチメニュー・LIFF）から始める。
- グループへの送信は、送る前に送り先を確認できるようにする。
- 夜間の停止・送信数の上限・緊急停止スイッチを通さずに送らない。
- 会員が見てよい情報だけを AI の回答に使う（役員向けの資料をグループや一般会員への回答に使わない）。

### コミットと Pull Request

- コミットメッセージは `feat:`・`fix:`・`docs:`・`refactor:`・`test:`・`chore:` などで始め、内容は日本語で書けます。
- Pull Request には「何を・なぜ変えたか」と「どう確認したか」を書いてください。
- 1 つの Pull Request には 1 つの目的だけを入れてください。

## ライセンス

コントリビュートしていただいた内容は、本プロジェクトと同じ [MIT License](LICENSE) で公開されます。
