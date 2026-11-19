# 0014 PDF の表示を、ブラウザー内蔵の iframe から PDF.js に置き換える（出典の根拠を PDF 上で強調するため）

- 状態: 一部置き換え済み（2026-11-20 の追記で、1 ページずつの表示を連続スクロールに改めた）。実装済み（`main` に入っている〔c134982 まで。連続スクロールは f378b60〕。未配備）
- 日付と決めた人: 2026-11-19。利用者が「PDF.js に置き換えるで進めて」と決め、モックの案 A（細いツールバー）を選んだ。依存（`pdfjs-dist`）の追加は、この指示に含まれるものとして進めた（`frontend/CLAUDE.md` は依存の追加に承認を求める。差し戻しは `package.json` の差分を戻せばよい）
- 置き換える範囲: [0013](0013-pdf-via-same-origin-relay.md) の「代案」にあった「PDF.js で自前の表示」を退けた部分。**同一オリジンの中継（`GET /api/documents/{id}/file`）の決定は、そのまま有効**（PDF.js が、この URL を取る）

## 背景
- ブラウザー内蔵の PDF 表示（iframe）は、`#page=N` でページへ移れるだけで、PDF の中の語句を指定して強調できない（Chrome は `#search=` を無視する）。出典から開いたとき、根拠の箇所を PDF 上で示す手段が無かった。
- 抽出テキストの表示は、根拠のチャンクを強調できる（`DocumentPanel.tsx`）が、PDF の見た目（図・表・レイアウト）は出ない。
- 利用者が、PDF.js で表示を置き換えることを決めた。

## 決定
- 表示: `pdfjs-dist` 6.5.438（**版を固定**。`legacy` ビルド）で、1 ページずつ、絵（canvas）と選択用の透明な文字（text layer）を重ねて描く。ツールバーは「前へ・N / 総数・次へ・縮小・幅に合わせる・拡大・保存」だけ（検索・印刷・サムネイルは持たない）。「保存」は、同じオリジンの中継の URL に `download` を付けたリンク（保存名は資料名。認証なしの構成は API 直接なので、保存名は API の値）。
  - `legacy` を選んだ理由: 通常版は、ごく新しいブラウザーの機能（`Math.sumPrecise`・`Uint8Array.toHex` など）をそのまま使う。`legacy` は補う版（公式の FAQ の要約では Chrome 125+・Safari 18+。証拠等級 B）。サイズは約 7% 増。
- PDF.js との境界は `frontend/src/api/pdfjs.ts` の 1 ファイルだけ（`pdfjs-dist` を import するのはここだけ。初めて PDF を開くときに動的に読み込む）。テストは、この境界を差し替える（`src/test/fakePdf.ts`。jsdom には canvas・Worker が無い）。
- worker: バンドラーが `new URL('pdfjs-dist/legacy/build/pdf.worker.min.mjs', import.meta.url)` で出力する（`_next/static/media/`）。`pdfjs-dist` と必ず同じ版になる（版が違うと PDF.js は動かない）。
- 配信用のデータ（日本語の CMap・標準フォント・wasm・ICC）: `frontend/public/pdfjs/` に、同梱のコピーをコミットする（約 200 ファイル・2.4 MB。ライセンスのファイルも同梱）。同じオリジンの `/pdfjs/…` から読む。`pdfjs-dist` の版を上げたら、コピーも同じ版にそろえる（`src/pdfjsAssets.test.ts` が、版の印 `public/pdfjs/VERSION` と内容の一致を確かめる）。
  - コピーを選んだ理由: ビルドの前処理で生成する案は、`dev`・`build`・`build:e2e*` の全部に仕掛けを入れる必要があり、CI と `deploy-web.sh` は `npm ci --ignore-scripts` で動くので、`postinstall` も使えない。
  - 入れないもの: `quickjs-eval`（PDF のフォームのスクリプト用）、wasm が使えないときの代替の JS、`build/` のうち使わないファイル、`web/` の CSS・画像。text layer の CSS は、必要な分だけ `styles.css` に移した（PDF.js の CSS は全体にかかる規則が多く、アプリの CSS とぶつかる）。
- 根拠の強調:
  - 対象は、根拠の語句（あれば）、無ければ根拠のチャンクの本文の全体（抽出テキストの「根拠の箇所」と同じ単位）。**いまの画面は、出典カードから開くので語句（`span`）を渡さない**（コミット 4d76366 で、回答本文の引用の記法を外した）。実際に出るのは、チャンクの全体の強調。
  - 照合: PDF.js の text layer の文字を、NFKC に正規化し空白を除いて、対象と突き合わせる（`lib/pdfText.ts`）。**ページの中で全体が 1 か所に見つかるときだけ**強調する。見つからない・複数あるときは、強調せず、その旨を示す（ページは開く）。部分一致・先頭だけの強調はしない。
  - 強調するのは、出典が持つページ（`page`）を見ているときだけ。出典がページを持たないときは、ページを推測しないので強調もしない（これまでの規則のまま）。
  - 出典を押したときは、強調の先頭が見える位置までスクロールする。
- 読み込み・失敗の表示は、これまでの `Status`（読み込み中・固定の文・再試行）に戻る。PDF.js の例外の文や、サーバーの本文は画面に出さない（固定の文）。

## 検討した代案
| 案 | 退けた理由 |
|---|---|
| 内蔵ビューアのまま、ページ移動だけ | 根拠の強調ができない（これが動機） |
| 案 B（出典バー + サムネイルの列） | 利用者が案 A を選んだ。案 B は画面が重く、パネルの幅（約 380 px）に収まりにくい |
| PDF.js の標準ビューア（`PDFViewer`・`PDFFindController`）を使う | 約 322 KB が増え、全体にかかる CSS・`globalThis.pdfjsLib` の読み込み順の制約が付く。1 つの根拠の強調には、text layer の文字との照合だけで足りる（多ページの検索・一致件数の移動が要るなら、別に決める） |
| 配信用のデータを、ビルド前に生成する | 上の「コピーを選んだ理由」 |
| 通常版（`legacy` でない）の `pdfjs-dist` | ごく新しい JS の機能を、ポリフィルなしで使う。対応ブラウザーの下限が上がる |

## 結果と限界
- **測ったこと（証拠等級 A。ローカル、コンテナの Chromium・開発コンテナの Node。各 1 回）:**
  - テスト: Vitest 680 件が通り、型検査・`next build` が通る（ビルドが worker を `_next/static/media/` に出力することも確認）。
  - 実ブラウザー（`next start` と `next dev` の両方。評価用の実 PDF「あおば電機 AW-820 取扱説明書」6 ページと、チャットの出典カードから開く流れ）: PDF の描画・ページ送り・拡大・出典のページ（4 / 6）への移動・根拠のチャンクの強調が動き、コンソールの警告・エラーは無かった。ライト・ダークの両方。
  - 照合の実測: 評価用の PDF 6 冊の 203 チャンクのうち、チャンクの本文全体が、PDF.js の text layer の中で 1 か所に見つかったのは 199 件。残る 4 件は、表のチャンクで、文字の並びが PDF.js と違った（強調されず、その旨が出る）。
  - 日本語の非埋め込みフォント（手作りの PDF。`90ms-RKSJ-H`）: `/pdfjs/cmaps/90ms-RKSJ-H.bcmap` と `Adobe-Japan1-UCS2.bcmap` が 200 で読まれ、文字が正しく取り出せた。コンテナには日本語のシステムフォントが無く、その PDF の見た目（字形）は確かめていない（利用者の Mac では、PDF が指定する日本語フォントを OS が補う）。
- **未確認・限界:**
  - **本番（Worker `docunavi-web` / OpenNext）で、静的アセット（`/pdfjs/…`・worker）が配信され、Cookie 付きの中継から PDF を取れるか。** 配備していない。`.bcmap` の Content-Type と、アセットの既定のキャッシュ（毎回の再検証）も未確認。[0013](0013-pdf-via-same-origin-relay.md) の未確認（Free の CPU 時間・OpenNext 越しの流し込み）も、そのまま残る。
  - 中継は `Range`・`Content-Length` を返さないので、PDF.js は毎回、全体を取る（`no-store`。20 MiB まで）。ページごとの遅延読み込みは効かない。表示の速さは未測定。
  - Chrome（コンテナの Chromium）以外のブラウザーは未確認。`legacy` の下限（Chrome 125+・Safari 18+）は公式 FAQ の要約（B）。
  - 内蔵ビューアにあった検索・印刷・サムネイル・しおりは無い。保存は元の PDF をそのまま保存する。
  - 強調: ページをまたぐ表のチャンク（`page_end` あり）は、1 ページの文字に収まらないので強調されない。語句（`span`）の強調の実装はあるが、いまの画面からは呼ばれない。
  - 認証なしの構成は API を直接指す（別オリジン）。`download` は効かず、保存名は API の値（`source`）になる。
  - Before のスクリーンショットは撮れていない（ヘッドレスの Chromium は内蔵の PDF ビューアを出せない）。After は `.playwright-mcp/pdfviewer-after-*.png`（Git の管理外）。
  - 依存を足したため、`npm audit` の件数に変化がありうる（足す前後は比べていない）。
- 戻し方: このコミットを戻せば、iframe の表示（`OriginalPdf`）と内蔵ビューアに戻る。中継・API・データには変更がない。`package.json`・`package-lock.json` と `public/pdfjs/` も戻る。

## 更新の手順（`pdfjs-dist` の版を上げるとき）
1. `frontend/package.json` の `pdfjs-dist` を新しい版に固定し、`npm install --ignore-scripts`。
2. `frontend/public/pdfjs/` を作り直す。`node_modules/pdfjs-dist/` の `cmaps/`・`standard_fonts/`・`iccs/` を丸ごと、`wasm/` から `jbig2.wasm`・`openjpeg.wasm`・`qcms_bg.wasm` と `LICENSE*` を、`LICENSE` をコピーし、`public/pdfjs/VERSION` を新しい版にする。
3. `npm test`（`pdfjsAssets.test.ts` が、版と内容の一致を確かめる）。実ブラウザーで、日本語の PDF を 1 冊、描画と強調を確かめる。

## 出典
- 調査（2026-11-19。読み取りのみ。`pdfjs-dist` のソースと型、Next / OpenNext の配信、既存のコード・テスト。証拠等級は、読んだソース A、公式 FAQ・Cloudflare の文書の要約 B）
- コード: `frontend/src/api/pdfjs.ts`、`components/PdfViewer.tsx`、`hooks/usePdfDocument.ts`、`lib/pdfText.ts`・`pdfHighlight.ts`・`pdfView.ts`、`src/test/fakePdf.ts`、`public/pdfjs/`
- 文書: [0013](0013-pdf-via-same-origin-relay.md)、`frontend/CLAUDE.md`、`.claude/rules/ui-review.md`

## 追記（2026-11-20）: 連続スクロールに改めた
- **理由:** 利用者の指摘（1280px の画面）。パネルが画面の下端に届かない、ページを下スクロールで進められない、保存がダウンロードのアイコンでない。モックの案 C を、利用者が承認した（「全て承認」）。
- **改めた点（本文の「1 ページずつ」と、ツールバーの構成）:** 全ページを 1 つのスクロール領域に縦に並べる。枠の大きさは、先頭のページの大きさ × 倍率で決め、描いたページは、そのページの実際の大きさになる。canvas と text layer を持つのは、見えている範囲の前後 1 ページ（`RENDER_BUFFER`）だけで、それより遠いページの絵は捨てる。ツールバーは下部に浮かべ、ページ番号は入力欄、保存はダウンロードのアイコン（`aria-label` は「PDF を保存」）。
- **動きの規則:** ページへ移るのは、明示の合図（`page`・`cite`、ツールバー、番号の入力）のときだけ。スクロールは、現在のページと描く範囲を更新するだけ。倍率を変えたときは、スクロール位置を割合で保つ。出典の強調は、引用されたページが描かれてから 1 回、`cite` ごとに、強調を表示の上から 1/4 の高さへ寄せる。
- **変えていない点:** PDF.js との境界（`src/api/pdfjs.ts`）、worker、版の固定、同一オリジンの中継（0013）、強調の方式。
- **確かめた（2026-11-20、証拠等級 A）:** Vitest 719 件、型検査、ローカルの実 Chromium（1280px。下端まで届く、連続スクロール、ページ表示の追従、移動、拡大縮小、出典の強調、拡大した暗い表示。コンソールにエラーなし）、E2E 17 件。
- **未確認:** Safari・Firefox、スマホ、本番（未配備）。ページの大きさが混在する PDF では、描いたあとに高さが変わる（その場合のスクロールの安定は、実機で確かめていない）。
