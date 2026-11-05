# OSS 公開の告知素材（note 記事・動画）

git には入れていない手元の素材です。

| ファイル | 内容 |
|---|---|
| `note_article.md` | note 記事の原稿「【青年会議所 \| JC】地方で頑張る全ての専務へ - 専務AIを無料公開しました」 |
| `images/` | 記事の画像（`00_header.png` は見出し画像。ほかは本文の該当箇所に） |
| `video/専務AI_プロモーション_*.mp4` | プロモーション動画（約 2 分） |
| `video/専務AI_マニュアル_*.mp4` | マニュアル動画（約 9 分・章立て） |
| `video/youtube_chapters.txt` | YouTube の説明欄に貼る目次（チャプター） |
| `video/youtube_description_*.txt` | YouTube の説明欄にそのまま貼る文面（GitHub・ご意見フォームの URL 入り。マニュアルは目次つき） |
| `scripts.md` | 動画の台本 |
| `voices/` | ナレーションの声の見本（声3 = Orus に決定） |

## 投稿の手順（案）

1. 動画 2 本を YouTube に「限定公開」または「公開」でアップロードし、説明欄に `youtube_description_promo.txt`・`youtube_description_manual.txt` を貼る
2. note で新しい記事を作り、`note_article.md` の本文を貼る
   - `<!-- … -->` のところに、動画の URL を貼る（note は URL を貼ると埋め込みになる）。GitHub の URL は入れ済み
   - `![…](images/…)` のところに、`images/` の画像をアップロードする
   - 見出し画像に `images/00_header.png` を設定する
3. 記事を公開する（GitHub のリポジトリは公開済み: https://github.com/tentoTen-Inc/jci-senmu-ai）

## 撮影について

- 画面はすべて、架空の LOM「サンプル青年会議所」と架空の会員のデモ環境で撮影（本番のデータは使っていない）
- ナレーションは Gemini TTS（Vertex AI、声: Orus）、BGM は Lyria（Vertex AI）で生成
