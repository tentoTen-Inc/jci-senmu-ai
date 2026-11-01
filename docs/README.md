# ドキュメント一覧

## 使い始める・運用する

| 文書 | 内容 |
|---|---|
| [setup.md](setup.md) | 新しい LOM で立ち上げる手順 |
| [configuration.md](configuration.md) | 環境変数・シークレット・運用設定の一覧 |
| [runbook.md](runbook.md) | デプロイ・緊急停止・ロールバック・調査の手順 |

## 要件

| 文書 | 内容 |
|---|---|
| [requirements.md](requirements.md) | 要件定義（背景・スコープ・機能一覧） |

## 機能ごとの設計書

新機能は、ここに設計書を足してから実装します（[CONTRIBUTING.md](../CONTRIBUTING.md)）。

| 文書 | 機能 | 主なコード |
|---|---|---|
| [mvp-design.md](mvp-design.md) | 予定・出欠確認・自動催促・集計 | `attendance.py`・`reminders.py`・`delivery.py`・`guardrails.py`・`summary.py` |
| [calendar-design.md](calendar-design.md) | Google カレンダーとの同期 | `gcal.py`・`calendar_sync.py`・`calendar_import.py` |
| [schedule-poll-design.md](schedule-poll-design.md) | 日程調整（LIFF） | `schedule_poll.py`・`polls_api.py` |
| [line-link-design.md](line-link-design.md) | LINE 連携の申請と承認、予約投稿 | `line_link.py`・`link_api.py` |
| [liff-menu-design.md](liff-menu-design.md) | 立場ごとのリッチメニューと LIFF 画面 | `rich_menu.py`・`liff_api.py`・`static/` |
| [proposal-ledger-design.md](proposal-ledger-design.md) | 議案台帳と議案書の同期 | `proposal_*.py`・`meeting_package.py` |
| [committee-docs-edit-design.md](committee-docs-edit-design.md) | 委員会資料を LINE から編集 | `committee_docs.py`・`doc_edit_intent.py` |
| [doc-session-design.md](doc-session-design.md) | チャットは検索のみ、書き込みはボタンから | `doc_session.py`・`member_menu.py` |
| [external-notice-design.md](external-notice-design.md) | 対外連絡メールの取込と伝達 | `gmail.py`・`gmail_import.py`・`notice_actions.py` |
| [survey-design.md](survey-design.md) | 例会アンケートの集計と催促 | `survey_*.py`・`forms.py` |
| [nl-assistant-design.md](nl-assistant-design.md) | 自然文での問い合わせ応答 | `assistant.py`・`rag.py`・`llm.py` |
| [datalake-design.md](datalake-design.md) | LINE のデータレイク（BigQuery・Cloud Storage） | `lake.py`・`line_worker.py`・`lake_query.py` |
| [lake-ai-design.md](lake-ai-design.md) | 会話・ファイルの索引と検索（RAG） | `lake_knowledge.py`・`file_links.py` |
| [drive-knowledge-design.md](drive-knowledge-design.md) | ドライブの資料の取込と検索 | `drive_knowledge.py`・`drive_import.py` |
| [feedback-box-design.md](feedback-box-design.md) | 意見箱 | `feedback_box.py`・`feedback_api.py` |
| [dashboard-design.md](dashboard-design.md) | 管理画面（構成・認証） | `admin_api.py`・`web/` |
| [admin-ui-design.md](admin-ui-design.md) | 管理画面のデザイン | `web/src/` |

コードはすべて `app/` の下にあります。

## LOM ごとの運用記録

本番の値（URL・チャネル ID など）や導入時の調査の記録は、個人情報や内部の情報を含むため、このリポジトリには入れず LOM ごとに管理します。
