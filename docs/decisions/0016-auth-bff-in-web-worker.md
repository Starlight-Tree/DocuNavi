# 0016 認証を、画面のサーバー側（BFF）へ寄せる（下書き）

- 状態: 提案（下書き。2026-11-21 起案。コード・設定には何も入っていない。決定済みの点と未決の点は「決定」で分けた）
- 日付と決めた人: 2026-11-21 起案。利用者が同日に、(1) 60 秒チケットを許す、(2) ADR を先に書く、(3) 動機を XSS 対策にして案 A の方向で進める（提案を承認。設計の確定は、CPU の再計測のあと）、を承認した。**SSE の経路（案 B）と、チケットの渡し方は未決。**

## 背景
- いまの認証: Better Auth は、データ用 Worker `docunavi-api`（Hyperdrive 経由の Postgres。ES256 の鍵）にある。画面の Worker `docunavi-web`（OpenNext）は、`/api/auth/*` を中継するだけ（`app/api/auth/[...all]/route.ts`）。ブラウザーは `GET /api/auth/token` で 15 分の JWT（ES256）を取り、`Authorization: Bearer` で API と Go の SSE 中継へ送る。FastAPI は Bearer だけを読む（[0013](0013-pdf-via-same-origin-relay.md)）。
- 画面は SPA で、サーバー側の処理（Server Components・Route Handlers・Server Actions）を足さない規則（`frontend/CLAUDE.md`。例外は中継 2 本）。
- 動機（**1 に決定。利用者が承認、2026-11-21**）:
  1. XSS 対策: JWT がブラウザーの JavaScript から読める状態を、なくす（HttpOnly の Cookie だけにする）。外部の AI の回答の文章、PDF など、外部の内容を画面に出すので、XSS の入口がある。**採用する動機。**
  2. 姉妹プロジェクト JobNavi（Better Auth を Next.js の中に置く。`/api/auth/token` を画面のサーバーが使う）と、形を揃える。**動機にしない。** JobNavi の形は案 C に当たり、Free の CPU に最も危ない（下の案）。揃えるために寄せる理由にならない。
- 参考: RFC 10017（BCP 212）が、ブラウザーのアプリの認可に BFF を挙げる。Better Auth の公式の統合は Next.js の route handler。Cloudflare の文書は、新規の Next.js に vinext を勧め、OpenNext は既存の維持向けとする（いずれも 2026-11-21 の調査の要約。原文は未確認の点がある。証拠等級 B）。

## 制約（調べたこと）
- 実行環境を変える決め手は見つからなかった。Workers のバンドル上限は 64 MiB（2026-09-04 以降、全プラン）。残る決め手は CPU 時間。
- **Cloudflare のプランは Free。** 利用者の回答（2026-11-21）と、ダッシュボードの Workers plans（「Current plan」が Free。CPU は 10 ms / リクエスト、リクエストは 10 万 / 日。2026-11-21 に読み取り。証拠等級 A）。サブリクエストは 50 本。Hyperdrive は 1 日 10 万問い合わせ（Free の枠。要再確認）。
- **`docunavi-api` の実績（2026-11-21 に、ダッシュボードの Metrics を読み取り。直近 30 日・全バージョン合算。証拠等級 A だが、下の限界がある）**
  - 呼び出し 6.91k（うち 1 バージョンが約 6k）、エラー 0、「Exceeded CPU Time Limits」0、サブリクエスト 180（宛先は `oauth2.googleapis.com` だけ。2xx が 11、4xx が 12）。
  - CPU 時間の P50 16.75・P90 41.99・P99 47.17・P999 54.91（単位の表示は無い。下の Logs の `cpuTimeMs` と整合し、ミリ秒と見られる）。壁時間の P50 160・P99 457（同）。メモリは P50 13.3 MB・P99 17.4 MB。カードの「CPU Time」は平均 854 µs で、上の P50 と合わない。
  - **この数字から、Free の CPU 10 ms に収まるかは言えない。** 限界: (1) 17 の版と 64 の版の配備のころに集中した、試験とみられる呼び出しが大半（利用者の通常の `/token` の使い方ではない）。(2) 現在の版 `d7a56ac9` は、直近 24 時間の呼び出しが 0（本番は [0012](0012-accept-prisma-free-limit-and-cut-operations.md) の上限で止まっている）。(3) P50 がミリ秒なら Free の 10 ms を超えるのに、「Exceeded CPU Time Limits」が 0 という、つじつまの合わない点がある（理由は未確認）。(4) Hyperdrive の問い合わせは、サブリクエストに出ない。
- **`/api/auth/token` 1 回あたりの CPU（2026-11-21 に、Observability の Logs から読み取り。直近 7 日。`$workers.cpuTimeMs`。単位はミリ秒。証拠等級 A）**
  - 47 件（2026-11-14〜17 の試験とみられる呼び出し。エラー 0）。**中央値 17 ms・P90 77 ms・P99 126 ms・最大 126 ms。** 最新の 1 件は 8 ms。
  - **中央値でも Free の 10 ms を超えている。** それでも「Exceeded CPU Time Limits」とエラーは 0 だった。Free の 10 ms を、この Worker がどう扱われているかは**未確認**（制限の掛かり方か、当時のプランが違ったか。確かめていない）。上の Metrics の P50 16.75 ms は、これと整合する（単位はミリ秒）。
  - 限界: 件数が少ない。コールドスタート（初回の読み込み）と、サインインの直後の呼び出しを含む。ログに `Could not validate the database schema` のエラーが 2026-11-17〜18 にあり、そのあとの呼び出しは無い（[0012](0012-accept-prisma-free-limit-and-cut-operations.md) の上限）。通常の使い方の分布ではない。
  - **読み取れること:** `/token` は、1 回が重い（ES256 の署名とセッションの SELECT）。**API 呼び出しのたびに `/token` を呼ぶ設計は、Free では成り立たない見込みが強い。** 案 A は、15 分のトークンを画面の Worker 側で**キャッシュして、使い回す**設計が前提になる（セッションごとに 15 分に 1 回）。それでも、1 回の 77〜126 ms が Free で許されるかは、実測が要る。**Workers Paid（月 $5 と従量。CPU は 1 リクエスト最大 5 分）への切り替えも、選択肢に入れる**（利用者が決める。この ADR は決めない）。

## 案
| 案 | 中身 | 評価 |
|---|---|---|
| A: 薄い BFF | 認証（Better Auth・鍵・JWKS・`iss`）は `docunavi-api` に残す。画面の Worker は、Cookie を受けて、API 呼び出しに Bearer を付けて流すだけにする | **推奨。利用者が方向を承認（2026-11-21）。設計の確定は、トークンのキャッシュの設計と、Free か Paid かの判断のあと。** 鍵と `iss` を動かさない。画面側の CPU は小さい。ただし API 呼び出しのたびに `/token`（セッションの SELECT＋ES256 の署名）を呼ぶ設計だと、`docunavi-api` の CPU・サブリクエスト・Hyperdrive の枠に響く。15 分のトークンのキャッシュ設計が要る |
| B: SSE も BFF 経由 | `/chat/stream` も画面の Worker を通す | **今は見送る。** 切断の伝播（OpenNext 越し）・Free の CPU 10 ms・中継のレート制限のキー（いまは IP だけ）が、未計測。計測してから再検討する。それまで、`/chat/stream` は 60 秒のチケットで Go の中継へ直結する |
| C: Better Auth を Next.js の中へ | JobNavi と同じ形 | **やらない。** Next＋Better Auth＋ES256 を 1 リクエストで動かすと、Free の CPU に最も危ない。鍵・`iss` の移行も要る |

## 決定
**決定済み（利用者の承認、2026-11-21）**
- この ADR を、実装より先に書く。
- **60 秒の短命チケットを許す。** 用途は、`/chat/stream`（Go の SSE 中継）への接続。Bearer を付けられない経路のため。
  - 条件（暫定。外部の AI の回答の案で、利用者が個別に確認した条件ではない）: URL のクエリには載せない（ログ・履歴・Referer に残るため）。1 回だけ使える。60 秒で切れる。用途を限る。
  - **[0013](0013-pdf-via-same-origin-relay.md) の「署名付き URL は作らない」「API の認証は Bearer のみ」を、この用途に限って置き換える。** 0013 の PDF の中継の決定は有効のまま。0016 が決定になる時点で、0013 の状態を「一部置き換え済み（0016）」に直す。

- **動機は XSS 対策（利用者が承認、2026-11-21）。方向は案 A（薄い BFF）。案 C はやらない。**

**未決（利用者の判断が要る）**
- SSE の経路: いまの方針は、チケットで Go の中継へ直結（案 A のまま）。画面の Worker を通すか（案 B）は、計測してから。
- チケットの渡し方の詳細（URL のクエリを使わない場合、何で渡すか。外部の回答の「Cookie で渡す」は意味が曖昧で、確認していない）。
- API 呼び出しごとの `/token` を、15 分のトークンのキャッシュでどう減らすか（`/token` の CPU は計測済み。下の「実装の前に要ること」）。

**実装の前に要ること**
- ~~Workers Observability で `docunavi-api` の CPU 実績を読む~~ → **読んだ（2026-11-21。制約の節）。** `/token` は 1 回が中央値 17 ms・P90 77 ms と重い。
- **利用者の判断:** Free のまま、15 分のトークンのキャッシュで `/token` の回数を減らす設計にするか、Workers Paid に切り替えるか。
- 案 A のトークンのキャッシュの設計（どこに持つか。Worker のメモリは、リクエスト間で保証されない。KV・暗号化した Cookie などの候補は、これから調べる。未調査）。
- 案Aの Cookie 認証の汎用中継は、0013 が CSRF で退けた形に近い。`Origin`・`Sec-Fetch-Site` の検査を設計に入れる（0013 の中継は、`Sec-Fetch-Site: cross-site` を 403 で断っている）。
- `frontend/CLAUDE.md` の SPA 規則（Server Components などを足さない）を、どこまで改めるかを書く。

## 結果と限界（予想。実装していないので、すべて未実測）
- 直る（動機 1）: JWT が JavaScript から読めなくなる。XSS で Bearer を盗まれる（持ち出される）経路が減る。
- **直らない:** XSS が起きている間に、攻撃者が、その利用者のセッションで API を操作すること（Cookie は、同じ画面から送られる）。守れるのは、トークンの持ち出しであって、XSS そのものではない。CSP・出力のエスケープなど、XSS を防ぐ対策は別に要る。
- 増える: 画面の Worker の CPU・サブリクエスト。Cookie だけで API を叩ける経路（CSRF の検討が全 API に及ぶ）。チケットの発行と検証（API に足す変更）。
- 戻し方: BFF の経路を外せば、いまの Bearer 直送に戻る（データ・鍵は動かさない案Aの場合）。
- 未確認: Free の CPU の実測。OpenNext 越しの SSE の切断。チケットを使う場合の、Go の中継のレート制限のキー。

## 出典
- 調査: 2026-11-21 の会話（実行環境の調査 Workflow とその批評。原文の確認が済んでいない要約を含む）
- 実績: 2026-11-21 の Cloudflare ダッシュボード（Workers & Pages の `docunavi-api` の Metrics〔直近 30 日〕、Observability の Logs〔直近 7 日。`/api/auth/token` の `$workers.cpuTimeMs` の集計〕、Workers plans）。読み取りだけ。
- 文書: [0005](0005-go-sse-relay-without-prod-limit.md)、[0012](0012-accept-prisma-free-limit-and-cut-operations.md)、[0013](0013-pdf-via-same-origin-relay.md)、`frontend/CLAUDE.md`
