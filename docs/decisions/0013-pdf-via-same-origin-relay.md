# 0013 PDF は、画面の Worker の同一オリジンの中継で iframe に渡す（保存名を資料名にするため）

- 状態: 承認済み・実装済み（1ac0573、2026-11-18。方針と、`frontend/CLAUDE.md` の例外の追加、`GET /api/documents/{id}/file` の中継）
- 日付と決めた人: 2026-11-18 起案（実験と調査）。利用者が同日に承認（「例外の追加と ADR を進めて、保存名は `?name=` で」）

## 背景
- 画面は PDF を、認証付きの `fetch`（`Authorization: Bearer`）で取り、Blob URL にして iframe に渡している（`frontend/src/api/documents.ts` の `fetchDocumentPdf`、`hooks/useDocuments.ts` の `useDocumentPdf`、`components/DocumentPanel.tsx` の `OriginalPdf`）。`<iframe src>` は `Authorization` を送れないため。
- この形だと、Chrome 内蔵ビューアの保存ボタンの名前は、Blob URL の ID（UUID）になる。ヘッダー右上のアイコンは `<a download>` で資料名にできている（`DocumentPanel.tsx` のヘッダー、`pdfDownloadName`）が、ビューア内蔵の保存ボタンは直せない。
- API はすでに `Content-Disposition: inline; filename="document-{id}.pdf"; filename*=UTF-8''…` を返す（`app/api/documents.py` の `_content_disposition`）。ただし `filename*` に入るのは題名ではなく `source`（ファイル名）で、Blob URL ではヘッダー自体が使われない。
- 認証は、Cookie（Better Auth。`docunavi-web` のオリジンに置かれる）でセッションを持ち、API には JWT（ES256・15 分。`GET /api/auth/token`）を Bearer で送る。FastAPI は Bearer だけを読む（`app/auth.py` の `_bearer_token`）。
- 制約: `frontend/CLAUDE.md` は Route Handlers を足さないと定める（例外は `app/api/auth/[...all]/route.ts` だけ）。署名付き URL は作らない方針（[roadmap.md](../roadmap.md) の「条件待ち」）。

## 実験の証拠（2026-11-18、Chrome 157、ローカル、証拠等級 A）
- 静的サーバーが、同一オリジンの iframe に PDF を返し、ヘッダー `Content-Disposition: inline; filename*=UTF-8''<資料名>.pdf` を付けた。`Cache-Control: no-store` を付けた場合も、内蔵ビューアの保存名は資料名になった。ダウンロード履歴に `資料名テスト.pdf`（118 KB・完了）が 1 件。全体が保存できていた。
- 再実験（2026-11-18、Chrome の Claude プロファイル。ローカルの静的サーバー）: 本番と同じ形の応答（`Content-Disposition` に `filename` と `filename*`、`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、chunked で Content-Length なし）を、`?name=x#page=2` 付きの同一オリジン iframe で返した。内蔵ビューアは 2 ページ目で開き、保存ボタンの保存ダイアログの名前欄は `filename*` の資料名（拡張子は macOS が隠す）だった（ユーザーが画面で確認。ダイアログはキャンセルし、保存していない）。これでケース 4 が実測できた。証拠等級 A（1 回）。
- 実験の作ったファイルは、作業用の場所の `pdfexp/`・`pdfexp2/` にあり、リポジトリには入っていない。
- **この実験が確かめていないこと:** 実際の Worker・Cloud Run 経由の挙動、4xx/5xx のときの iframe の挙動、Range の扱い、Chrome 以外のブラウザー（回数は各 1 回）。

## 決定（提案）
- `frontend` に Route Handler を 1 本足す: `GET /api/documents/{id}/file`（`app/api/documents/[id]/file/route.ts`）。`/documents/:path*` の rewrites（ローカルの `API_PROXY_TARGET`）と重ならない名前にする。
- 流れ: ブラウザーの iframe が、同一オリジンのこの URL を `GET`（Cookie が付く）→ ハンドラーが、**Cookie だけ**を付けて `${AUTH_PROXY_TARGET}/api/auth/token` から JWT を取る → Cloud Run の `GET /documents/{id}/file` に **Bearer だけ**を付けて呼ぶ → 本文を読まずに流し、`Content-Disposition: inline; filename*=UTF-8''<資料名>.pdf` を付けて返す。
- API の認証は Bearer のみのまま変えない。資料グループの権限は、これまでどおり API（`request_access`）が毎回決める。ハンドラーは権限を判断しない。
- 署名付き URL・Cookie で通る API・長い期間のトークンは作らない。JWT は要求ごとに取り直し、保存・共有キャッシュしない。
- 保存名は、画面が `?name=`（`pdfDownloadName(document)` の結果）を付け、ハンドラーが再検査して使う（利用者が決定。規則を 1 か所に保てる。API は変えない）。資料名が URL に載り、Worker のログ（`observability` 有効）に残る点は、この決定で受け入れる。API が題名で `filename*` を作る案は採らない。
- 失敗時は、ステータスを保ったまま、固定の日本語だけの短い HTML を返す（API の本文は通さない。`frontend/CLAUDE.md` の「サーバーのエラー本文を画面に出さない」を守る）。
- フレームに入れてよいのは同一オリジンだけにする（`frame-ancestors 'self'` と `X-Frame-Options: SAMEORIGIN` を応答に付ける案。実験で内蔵ビューアが動くかの確認が要る）。

## 検討した代案
| 案 | 退けた（または後回しの）理由 |
|---|---|
| 署名付き URL（短命トークンを URL に載せる） | roadmap の方針（作らない）に反する。URL が漏れると、期限内は誰でも取れる。トークンの発行と検証を API に足す変更も要る |
| Cookie で通る API（FastAPI が Cookie を読む） | 「API は Bearer のみ」を崩す。Cloud Run と画面は別オリジンで、Cookie は `docunavi-web` のオリジンにしかない。CSRF の検討が API 全体に及ぶ |
| PDF.js で自前の表示（Blob のまま） | 保存ボタンも自前になり、保存名は直る。しかし依存の追加（承認が要る）、バンドルの増加、表示・検索・日本語フォントの再検証が要る。ブラウザー内蔵の表示を使う現在の利点を捨てる |
| 今のまま（Blob URL） | 変更なし。ビューア内蔵の保存だけ UUID 名。ヘッダーのアイコンは資料名で保存できる（`docs/features.md`）。**この案を選んでもよい**（後述の費用との比較） |

## 結果と限界（予想。実装していないので、すべて未実測）
- 直る: 内蔵ビューアの保存名が資料名になる。API の変更なしで（既定の案なら）。
- 戻るもの・増えるもの:
  - エラー表示が、`Status` の「読み込み中／固定文／再試行」から、iframe 内の固定 HTML に変わる（再試行ボタンは使えない）。`original_pdf` の事前確認（`/chunks`）があるので、404・409・20 MiB 超は通常 iframe に届かない。残るのは競合・503・401・ネットワーク断。
  - Blob は 1 回の取得で何度も使えたが、素の URL は iframe を作り直すたび（出典を押し直す・ページが変わる）に取得が走る。`Cache-Control: no-store` のままではキャッシュも効かない。1 回ごとに、API で SQL 1 回＋原本の読み出し・ハッシュ確認。[0012](0012-accept-prisma-free-limit-and-cut-operations.md) の操作数の削減に逆行しうる（要測定）。
  - 要求ごとに Worker のサブリクエストが 2 本（`/token`、Cloud Run）。
  - Cookie だけで資料を取れる経路が 1 本できる（副作用のない `GET`）。
- Workers の制限（**未確認**）: Free の CPU 10 ms（待ち時間は数えない）、128 MB（公式の Limits、2026-11-13 の更新を要約で読んだ。証拠等級 B）。本文を読まずに流せば 20 MiB でもメモリには載らない（公式の Streams、B）。OpenNext 越しに本当にバッファされないか、Free で CPU に収まるかは、実測が要る。既存の素の Worker で 5 MB のバイナリが届いた記録はある（[target-architecture.md](../target-architecture.md)、等級 A。OpenNext の `docunavi-web` ではない）。契約が Free かも未確認（[configuration.md](../configuration.md)）。
- 戻し方: ハンドラーと画面の変更を戻せば Blob URL に戻る。データ・API・認証には変更がない（既定の案）。デプロイは、ユーザーが手動で実行する `deploy.yml` だけ。

## 出典
- 実験: 2026-11-18 の会話（Chrome 157。ローカル。上の「実験の証拠」）
- コード: `frontend/app/api/auth/[...all]/route.ts`、`frontend/src/api/documents.ts`、`frontend/src/hooks/useDocuments.ts`、`frontend/src/components/DocumentPanel.tsx`、`frontend/src/components/documentDisplay.tsx`、`app/api/documents.py`、`app/auth.py`、`workers/api/src/auth.ts`
- 文書: [roadmap.md](../roadmap.md)、[architecture.md](../architecture.md)、[api.md](../api.md)、[0012](0012-accept-prisma-free-limit-and-cut-operations.md)
- Cloudflare 公式（Workers の Limits・Streams・Pricing。2026-11-13・2026-06-15 の更新。要約経由で読んだ、証拠等級 B）
