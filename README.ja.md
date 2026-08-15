# 42 subject.pdf crawler

> English: [README.md](README.md)

42 intra の **"Not registered"** な課題の `subject.pdf` を一括ダウンロードするクローラー。

## しくみ（ハイブリッド構成）

| ステップ | データ源 | 理由 |
|---|---|---|
| 1. Not registered 一覧 | **42 API** (`api.intra.42.fr/v2`) | 構造化されていて確実 |
| 2. subject.pdf のURL抽出 + DL | **HTML** (`projects.intra.42.fr`) | PDFリンクはAPIに存在せず、詳細ページのHTMLにしか無い |

そのため認証情報が **2種類** 必要です。

## 必要な認証情報（環境変数で渡す。コードには埋め込まない）

| 変数 | 中身 | 取得方法 |
|---|---|---|
| `INTRA_TOKEN` | api.intra.42.fr の Bearer トークン | 42 OAuth アプリのトークン（~2時間で失効） |
| `INTRA_LOGIN` | 自分の42 login（例 `saraki`） | アプリトークンは `/v2/me` 不可なので `/v2/users/<login>` を使う |
| `INTRA_CURSUS_ID` | 対象 cursus id（例 `21`=42cursus, `1`=旧42） | `/v2/cursus/:id/projects` で一覧する |
| `INTRA_SESSION` | `_intra_42_session_production` Cookie の値 | ログイン済みブラウザの DevTools → Application → Cookies → `https://projects.intra.42.fr` → `_intra_42_session_production` |

### トークン種別による分岐
- **アプリトークン**（`client_credentials`）… `/v2/me/*` は401。`INTRA_LOGIN` と `INTRA_CURSUS_ID`（または `--user` / `--cursus-id`）を指定すると `/v2/cursus/:id/projects` と `/v2/users/:login/projects_users` を使います。
- **ユーザートークン**（`authorization_code`）… `INTRA_LOGIN`/`INTRA_CURSUS_ID` を省略すれば `/v2/me/projects` と `/v2/me/projects_users` を使います。

## 使い方（uv 推奨。依存は自動解決）

```bash
# まず一覧とPDF URLの解決だけ（DLしない）で動作確認
INTRA_TOKEN=xxxxx INTRA_SESSION=yyyyy uv run crawler.py --dry-run

# 本実行（subjects/<slug>/subject.pdf に保存）
INTRA_TOKEN=xxxxx INTRA_SESSION=yyyyy uv run crawler.py --out subjects
```

### オプション

| フラグ | 既定 | 説明 |
|---|---|---|
| `--out DIR` | `subjects` | 保存先ディレクトリ |
| `--delay SEC` | `0.6` | リクエスト間隔（APIは2 req/s 制限） |
| `--cursus-id N` | なし | 一覧を特定 cursus に限定（既定は `/v2/me/projects`） |
| `--user LOGIN` | なし | `/v2/users/:user/projects_users` 用の 42 login（既定は `/v2/me/projects_users`） |
| `--all` | off | 「Not registered」フィルターを外して全プロジェクトを取得 |
| `--limit N` | なし | 先頭 N 件だけ処理（試走用） |
| `--dry-run` | off | 一覧とPDF URL解決のみ。DLしない（Cookie不要） |
| `--adopt-existing` | off | 既にディスクにある PDF を再DLせず manifest に取り込む |
| `--force` | off | manifest を無視して全ての `subject.pdf` を再取得 |

## 挙動メモ
- `<out>/manifest.json` に、各 `subject.pdf` をどの CDN URL から取得したかを
  `sha256` / `size` / `etag` / `last_modified` / `fetched_at` とともに記録する。
  再実行時は、詳細ページ上の URL が記録と一致しない課題だけを再取得する
  （この URL はストレージキーを含むため、subject が再アップロードされると変わる）。
  一致するものは "up to date" としてスキップ。
- manifest は1件DLするごとに書き出すので、途中で中断しても取得済みの分は残る。
  DL 自体も `.part` に書いてから rename する。
- manifest 導入前に溜めた `subjects/` は、一度 `--adopt-existing` で実行すると
  既存ファイルを（ハッシュを取り、現在の URL を正として）取り込むので、
  いま全部再DLせずに以降の更新だけを検知できる。フラグ無しなら一度だけ再DLされる。
- `subject.pdf` が無い課題・失敗した課題は最後のサマリに一覧表示。
- 401（API）→ `INTRA_TOKEN` 失効、sign-in リダイレクト → `INTRA_SESSION` 失効、を明示エラーで通知。
- レート制限（429/5xx）は指数バックオフで自動リトライ。

## 検証ポイント
`/v2/me/projects` のレスポンス形（`id`/`slug`/`name`）と `/v2/me/projects_users` の `project.id` を前提にしています。
トークン種別によって `/v2/me/...` が使えない場合は `--cursus-id` を指定して `/v2/cursus/:id/projects` 経路に切り替えてください。
