# 0016 認証を、画面のサーバー側（BFF）へ寄せる

- 状態: 提案（2026-11-21 起案。同日、保存済みの設計と反証の結果〔J7・J9。「出典」〕を取り込んで、案を具体にした。コード・設定には何も入っていない。「決定済み」は利用者の承認があるもの、「決定（案）」は推奨を 1 つ示したもので利用者の判断待ち、「未決」は利用者の判断か計測が要るもの。決めるのは利用者）
- 日付と決めた人: 2026-11-21 起案。利用者が同日に、(1) 60 秒チケットを許す、(2) ADR を先に書く、(3) 動機を XSS 対策にして案 A の方向で進める（提案を承認。設計の確定は、CPU の再計測のあと）、(4) Cloudflare は Free のままにする、を決めた。**「決定（案）」の各項は、利用者がまだ承認していない。**
- 置き換える範囲（0016 が決定になったときの予定。いまは何も置き換えていない）: [0013](0013-pdf-via-same-origin-relay.md) の決定のうち、「API の認証は Bearer のみ」（`/chat/stream` だけ、専用ヘッダーのチケットを足す）と、「JWT は要求ごとに取り直し、保存・共有キャッシュしない」（暗号化した Cookie に入れて最長 15 分使い回す）。**置き換えないもの:** PDF の中継そのもの、0013 の「署名付き URL は作らない」（チケットは URL に載せない）、[0005](0005-go-sse-relay-without-prod-limit.md)（中継は認証を持たず、検証は上流）。0013 の状態は、そのとき「一部置き換え済み」に直す（作業は末尾の「決定になったときの作業」。0013 の本文は書き換えない）。

## 背景
- いまの認証: Better Auth は、データ用 Worker `docunavi-api`（Hyperdrive 経由の Postgres。ES256 の鍵）にある。画面の Worker `docunavi-web`（OpenNext）は、`/api/auth/*` を中継するだけ（`app/api/auth/[...all]/route.ts`）。ブラウザーは `GET /api/auth/token` で 15 分の JWT（ES256）を取り、`Authorization: Bearer` で API と Go の SSE 中継へ送る。FastAPI は Bearer だけを読む（[0013](0013-pdf-via-same-origin-relay.md)）。
- いまの JWT の持ち方: 画面のタブごとのメモリ（`frontend/src/auth/betterAuth.ts` のクロージャ内の変数。`localStorage` にも `sessionStorage` にも入れない）。期限の 60 秒前に取り直し、同時の取得は 1 本にまとめる（J4『1』）。
- **JWT が JavaScript から読める経路は、2 つある（読んで確認、2026-11-21。「出典」の凡例）。** (a) `GET /api/auth/token`: 画面の Worker の中継が、パスを見ずに全部を認証用 Worker へ流すので、Cookie 付きの同一オリジンの `fetch` で JWT が返る。(b) `GET /api/auth/get-session` の応答ヘッダー `set-auth-jwt`: Better Auth の jwt プラグインの after フックが、`get-session` のたびに署名してこのヘッダーに載せる（`disableSettingJwtHeader` を設定していない）。画面はこのヘッダーを読んでいない（リポジトリ全体の grep で 0 件）。動機 1 を満たすには、両方を塞ぐ必要がある（J7 理由 4、J9 R2・R8）。
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
  - **中央値でも Free の 10 ms を超えている。** それでも「Exceeded CPU Time Limits」とエラーは 0 だった。Free の 10 ms を、この Worker がどう扱われているかは**未確認**（制限の掛かり方か、当時のプランが違ったか。確かめていない）。上の Metrics の P50 16.75 ms は、これと整合する（単位はミリ秒）。公式の文書（Limits）には、isolate に「まれな超過を許す柔軟さがあり、一貫して上限に当たると打ち切られる」とある（J5『6』、証拠等級 B）。説明候補ではあるが、**「一貫して」の定義は書かれておらず、因果は未確認。**
  - 限界: 件数が少ない。コールドスタート（初回の読み込み）と、サインインの直後の呼び出しを含む。ログに `Could not validate the database schema` のエラーが 2026-11-17〜18 にあり、そのあとの呼び出しは無い（[0012](0012-accept-prisma-free-limit-and-cut-operations.md) の上限）。通常の使い方の分布ではない。
  - **読み取れること:** `/token` は、1 回が重い（ES256 の署名とセッションの SELECT）。**API 呼び出しのたびに `/token` を呼ぶ設計は、Free では成り立たない見込みが強い。** 案 A は、15 分のトークンを**キャッシュして使い回す**設計が前提になる。それでも、1 回の 77〜126 ms が Free で許されるかは、実測が要る。**署名の経路は `/token` だけではない**（`get-session` のたびにも走る。上の「背景」）。重さの根が、署名なのか、リクエストごとの `betterAuth()` と `Pool` の組み立て（`workers/api/src/index.ts`）なのかは、いまのデータでは区別できない（J9 R2）。**Workers Paid（月 $5 と従量。CPU は 1 リクエスト最大 5 分）への切り替えも、選択肢に入れる**（利用者が決める。この ADR は決めない）。
- **画面の Worker `docunavi-web` の CPU は、1 件も測っていない（2026-11-21）。** 実測は `docunavi-api` だけ。OpenNext 越しの Route Handler の CPU は未計測で、外部の報告（C 等級。「OpenNext の fetch の中継で 60〜120 ms」。本文は 429 で未読、検索の要約だけ。J5『8b』）が、唯一の手がかり（J9 R1）。0013 の「未確認のまま」（Free の CPU・1102）も、いまだに未確認。
- **BFF にすると、API 呼び出し 1 回ごとに、画面の Worker のリクエストが 1 回増える**（いまは Cloud Run へ直送で、この枠に数えない）。Free の 1 日 10 万リクエストに、静的ファイルが数えられるかは、公式の Limits に明記が無く未確認。画面 1 枚あたりの API 呼び出し数は、数えていない（J9 R7）。
- **サイトの境界（公開サフィックスリストを取得して確認、2026-11-21、証拠等級 A）:** `workers.dev` が載っている（行 12806）。したがって、同じ Cloudflare アカウントの `*.workers.dev` の Worker は、画面の Worker と **same-site**（別オリジンだが同じサイト）。`*.run.app` も載っている（行 14066）ので、Cloud Run の各サービスは、画面の Worker から見ても、互いに見ても **cross-site**。同じアカウントに `docunavi` 以外の Worker があるかは未確認（J9 R5）。

## 案
| 案 | 中身 | 評価 |
|---|---|---|
| A: 薄い BFF | 認証（Better Auth・鍵・JWKS・`iss`）は `docunavi-api` に残す。画面の Worker は、Cookie を受けて、API 呼び出しに Bearer を付けて流すだけにする | **推奨。利用者が方向を承認（2026-11-21）。** 鍵と `iss` を動かさない。**画面の Worker の CPU は未計測**（起案時の「画面側の CPU は小さい」は、根拠が無かった。J9 R1）。トークンの置き場・チケット・CSRF・縮退は、下の「決定（案）」 |
| B: SSE も BFF 経由 | `/chat/stream` も画面の Worker を通す | **当面は見送る。** 切断の伝播（OpenNext 越し）・Free の CPU 10 ms・中継のレート制限のキー（中継が見る接続元が、利用者ではなく画面の Worker になる）が未計測。ステップ 2 の計測で成り立つなら、B を採ればチケットが丸ごと要らなくなるので、そこで再評価する。それまで `/chat/stream` は、60 秒のチケットで Go の中継へ直結 |
| C: Better Auth を Next.js の中へ | JobNavi と同じ形 | **やらない。** Next＋Better Auth＋ES256 を 1 リクエストで動かすと、Free の CPU に最も危ない。鍵・`iss` の移行も要る |

## 決定済み（利用者の承認、2026-11-21）
- この ADR を、実装より先に書く。
- **60 秒の短命チケットを許す。** 用途は、`/chat/stream`（Go の SSE 中継）への接続。承認のときの理由は「Bearer を付けられない経路のため」（外部の AI の回答の案）だった。**この理由は当たらない**: 現行の画面は、`fetch` の POST で `Authorization` を付けて中継へ送っている（`EventSource` ではない。読んで確認）。BFF のあとは、画面が JWT を持たないので、別の資格が要る。これが実際の理由（草案との食い違い 12）。
  - 条件（暫定。外部の AI の回答の案で、利用者が個別に確認した条件ではない）: URL のクエリには載せない（ログ・履歴・Referer に残るため）。1 回だけ使える。60 秒で切れる。用途を限る。**このうち「1 回だけ」は、「決定（案）」6 で緩める案を出した（利用者の判断待ち）。**
  - 起案時は、ここに「0013 の『署名付き URL は作らない』『API の認証は Bearer のみ』を、この用途に限って置き換える」と書いた。専用ヘッダーで渡すなら URL に載らないので、前者は破らない（草案との食い違い 4。置き換える範囲は、冒頭の状態の欄）。
- **動機は XSS 対策。方向は案 A（薄い BFF）。案 C はやらない。**
- **Cloudflare は Free のまま。** `/token` の回数は、トークンのキャッシュで減らす。Paid は、実測で収まらないと分かったときだけ相談する（再提案はしない）。

## 決定（案）: 推奨と、選ばなかった案
以下は、起案後の調査（J4・J5）、設計（J7）、反証（J9）を、worktree のソースで確かめて（「出典」の凡例）まとめた案。**推奨は論点ごとに 1 つ。利用者が承認するまで、決定ではない。**

| # | 論点 | 推奨 |
|---|---|---|
| 1 | トークンの置き場 | 暗号化した HttpOnly Cookie（BFF は状態を持たない） |
| 2 | 署名の回数（`/token` の外） | `disableSettingJwtHeader: true`（**最初の小さな 1 手**）。`definePayload` は `sub` だけ |
| 3 | JS から JWT を読めなくする | 認証の中継を許可リストにして、`/token` を塞ぐ |
| 4 | BFF の中継の作り | 許可リスト・厳密な同一オリジン検査・上流の `Set-Cookie` の転送・`__Host-` の Cookie |
| 5 | SSE の経路 | 当面は直結（チケット）。B はステップ 2 の計測のあとで再評価 |
| 6 | 60 秒チケット | 専用ヘッダー・MAC（別関数で検証）・インスタンス内の 1 回限り・BFF が発行 |
| 7 | 縮退 | 更新は要求のときだけ。期限内は古い JWT を使う。尽きたら 503（ログアウトにしない） |

### 1. トークンのキャッシュの置き場（J7 候補 A）
**推奨: 暗号化した HttpOnly Cookie。BFF は状態を持たない。**
- JWT を AES-GCM（Web Crypto）で暗号化して、Cookie 1 本に入れる。鍵は画面の Worker の新しい Secret。AAD にセッショントークンの SHA-256 を入れて、セッションに結ぶ。
- 使い方: API 呼び出しのたびに復号する。期限まで 120 秒以上なら、その JWT を Bearer にして流す。未満なら、同じ要求の中で `/token` を呼び直し、`Set-Cookie` を付けて返す（ストリームの応答は、ヘッダーを確定したあとに `Set-Cookie` を付けられないので、更新は流す前に済ませる）。復号できないときは「Cookie なし」と同じ扱いで取り直す（ログアウトにしない。Secret を替えても、全員が 1 回取り直すだけ）。
- PDF の中継も同じ Cookie を使う（いまは、PDF を開くたびに `/token` が 1 回走る。読んで確認）。

**理由**
1. 基準の「BFF が毎回 `/token`」は不可: 中央値 17 ms が Free の 10 ms を超え、署名のたびに `jwks` の SELECT が 1 本走って、Prisma Free の操作数にも響く（[0012](0012-accept-prisma-free-limit-and-cut-operations.md)。J7 理由 1、J9 生存 1。SELECT は読んで確認）。
2. ブラウザーが写しを持つので、効き目が isolate の寿命に左右されない。Workers KV・Durable Objects の Free の枠を使わない。変更が Secret 1 つと Cookie 1 本で済む。
3. いまは、タブごとのメモリ保持がある。Cookie は全タブで共有なので、`/token` の回数は「タブごと」から「ブラウザーごと」へ、同等以下に減る（J7 理由 3）。
4. 暗号化（AAD にセッション）にする理由は、秘匿より、**偽造と取り違えの防止**: 別アカウントの有効な JWT を、同じサイトの別の Worker（上の「サイトの境界」）などが Cookie に置いても、鍵が無ければ作れないので通らない。平文の JWT の Cookie だと、利用者の操作が別アカウントで行われる恐れがある（この ADR の補足。J7 は「別の利用者でログインし直した後の取り違えを防ぐ」と書いた）。サインアウトのあと、セッションの Cookie が無ければ AAD を作れず復号できないので、JWT の Cookie を消し忘れても使われない（J7 の設計からの読み取り。実装で確かめる）。

**選ばなかった案**
- isolate のメモリ（J7 候補 B）: 追い出し・複数の拠点・配備で外れる。寿命の時間は文書に無く、Free の小さな利用では効かない恐れがある（J5『1』、証拠等級 B）。値だけを置けば I/O のエラーは避けられるが、同時のミスの重複は抑えられない。**主案にも補助にもしない**（効くかを測れない）。
- Workers KV（候補 C）: Free は書き込みが 1 日 1,000 回で、結果整合（他の拠点で最大 60 秒以上）。OpenNext の文書自身が、KV を増分キャッシュには非推奨とする。JWT を共有の保存先に置くことにもなる（J5『2』、B）。
- Durable Objects（候補 D）: 同時更新の直列化は最も強いが、クラスの export・migration・カスタムの Worker エントリが要り（OpenNext での扱いは未確認）、単一障害点になる。実測で重複更新が問題と分かったときだけ、更新の調整にだけ使う形で再検討する（J7）。
- Cache API: workers.dev で動く保証が公式の文書に無い（独自ドメインでは動くとだけ）。利用者別の Bearer を拠点単位の共有キャッシュへ入れる設計は、キーを誤ると漏れる（J5『3』、B。コミュニティの「動かない」は C で、未読）。
- JWT の有効期限を延ばす（J7・J9 に無い。この ADR の補足）: `/token` の回数は減るが、下の「失効の限界」が期限に比例して広がり、0013 の「長い期間のトークンは作らない」にも反する。

**穴（この ADR の補足）:** ログイン直後など、Cookie が空のときに、画面が複数の API 呼び出しを同時に出すと、その数だけ `/token` が走る（いまの画面の `inflight` の共有は、サーバー側の Cookie 方式では効かない。複数のタブの同時更新も同じ形。J7 の失敗の形 1）。isolate の中で進行中の Promise を共有する案は、別の要求の I/O になって失敗する恐れがある（J7 候補 B の失敗の形、J5『1』）ので採らない。**対策: 画面は、最初に「準備」の要求を 1 本だけ出して Cookie を用意し、完了してから、ほかの要求を出す。** 重複しても害は `/token` の CPU の重複だけ（API は `jti` を見ず、両方の JWT が通る。読んで確認）。本数は、ステップ 3 で数える。

### 2. 署名の回数を減らす（BFF と無関係に効く）
**推奨: (a) `disableSettingJwtHeader: true`。(b) `definePayload` で `sub` だけにする。**
- (a) 画面は `set-auth-jwt` を読まないので、いまの `get-session` ごとの署名と `jwks` の SELECT は、ほぼ無駄とみられる。画面は `get-session` を起動時と、ウィンドウのフォーカス復帰・オンライン復帰のたびに呼ぶ（既定の `refetchOnWindowFocus` が真で、`sessionOptions` を指定していない。5 秒の間隔の制限だけ。読んで確認）。効果の大きさは**未計測**。BFF を採らなくても意味があり、戻しも 1 行。**最初の小さな 1 手。**
- (b) CPU の削減は主張しない（署名の対象が数百バイト減るだけ。J9 R2）。目的は、Cookie の大きさと、Bearer に入る個人情報（メール・名前・画像）を減らすこと。FastAPI が使うのは `sub` だけで（`app/auth.py`。`app/` の中でトークンの中身を扱うのはここだけ。読んで確認）、画面が JWT から読むのは `exp` だけ（`betterAuth.ts`。読んで確認）。
- 選ばなかった案: なし。(a)(b) とも、これ以外にする理由が見つからなかった。
- 未定: 重さの根が、リクエストごとの `betterAuth()` と `Pool` の組み立てだった場合は、isolate で `createAuth` を使い回す案がある（`workers/api/src/node.ts` は 1 回だけ作る）が、Hyperdrive の接続の寿命と I/O の制約があり、別の検討になる。ステップ 0・1 の計測のあとで要るかを決める。

### 3. XSS 対策を成り立たせる前提（JS から JWT を読めなくする）
**推奨: 認証の中継（`/api/auth/*`）を、拒否リストではなく許可リストにする。** 通すのは、画面が実際に使う経路だけ: `get-session`・`sign-in/social`・`callback/google`・`sign-out`・`update-user`・`jwks`（FastAPI が `{AUTH_BASE_URL}/api/auth/jwks` から取る。`AUTH_BASE_URL` は画面の Worker のオリジンにする設計。J4。本番の実値は未確認）。自動テスト用ログインを使う間は、`sign-in/email`・`sign-up/email` も（`workers/api/src/auth.ts`。J9 の一覧には無かった）。**`/token` は通さない。** BFF は、認証用 Worker の `/api/auth/token` を、この中継を通さずに直接呼ぶ（PDF の中継が、いまそうしている）。`set-auth-jwt` は、2(a) で止める。
- 理由: 拒否リストは、パスを正規化して比べれば迂回されない（Better Auth のルーターは URL を復号せず厳密一致で、`//` と末尾のスラッシュは 404。読んで確認。`/%74oken`・`/Token` なども 404 とは、J9 が報告しているが、この ADR では実行していない）。それでも、許可リストなら、将来のプラグインのエンドポイントが自動で塞がる。
- 選ばなかった案: 拒否リスト（`/token` と `set-auth-jwt` だけ）。実装は小さいが、新しいエンドポイントの塞ぎ忘れが起きる。
- **限界（動機を言い切らない）:** (a) `get-session` の JSON に `session.token`（セッションの生の値）が含まれ、JS から読める（読んで確認。`returned: false` が無い）。Cookie は署名つきで（`BETTER_AUTH_SECRET` の HMAC）、Bearer プラグインも使っていないので、生の値だけでは使えないとみられるが、実行して確かめていない。「JS から読める長寿命の値が無い」とは書かない。(b) チケットの発行 API（6）は JS から呼べる。XSS は、ページが開いている間、60 秒のチケットを作って外へ出し、別の場所から `/chat/stream` を使える（1 回のチャット分。J9 R8）。

### 4. BFF の中継の作り
**推奨:**
- **許可リスト。** 通すのは、許可した（メソッド, パス）だけ。パスを見ない素通しは、認証の迂回の面が広いので採らない（この ADR の補足）。上流へ送るヘッダーは `Authorization`（Bearer）・`Accept`・`Content-Type` だけ。`Cookie`・`Origin` は送らない。リダイレクトはたどらない。上流の `Set-Cookie` は通さない。応答は `Cache-Control: no-store`、CORS のヘッダーは付けない。
- **同一オリジンだけ通す。** `Sec-Fetch-Site` が `same-origin`。ヘッダーの無い古いブラウザーは、`Origin` が自分のオリジンと**完全一致**するときだけ。両方無い要求は、GET 以外は断る。POST などの本文は JSON（`Content-Type`）だけ。**0013 の PDF の中継の検査（`cross-site` だけを断る）では足りない**: 同じサイトの別の Worker からの要求を通してしまう（J9 R5。「サイトの境界」）。PDF の中継も、この検査にそろえる。
- **上流の `Set-Cookie` を、ブラウザーへの応答へ全部転送する。** BFF が内部で呼ぶ `/token` は、セッションの確認を通る（`sessionMiddleware`）ので、`session_data` の写し（5 分）の再発行と、`updateAge`（1 日）を過ぎたときの `session_token` の Max-Age（7 日）の延長を、`Set-Cookie` で返す（読んで確認。`api/routes/session.mjs`）。これを捨てると、DB の期限だけ延びて、ブラウザーの Cookie は延びない。そのまま使い続けても、最初のログインの 7 日後にログアウトになり、`session_data` が戻らないので、以後の確認が毎回 DB を引く（0012 の操作数と、5 分の写しの狙いが崩れる）見込み（J9 R4。**コードを読んだ推論で、実機の応答は未確認**）。複数の `Set-Cookie` は 1 本ずつ追加する。PDF の中継も同じ（いまは応答のヘッダーを見ていない。読んで確認）。
- **JWT の Cookie:** 名前は `__Host-` で始め、`Path=/`・`HttpOnly`・`Secure`・`SameSite=Lax`。`Domain` は付けない。理由: 同じサイトの別の Worker が、`Domain` つきで同名の Cookie を置く（cookie tossing）のを、`__Host-` が防ぐ（`__Host-` は `Domain` 無し・`Path=/` を強制する。一般知識）。J7 の `Path=/api` は、`__Host-` と両立せず、認証の中継（`/api/auth/*`）にも付く（J9 R9）。**認証の中継は、この Cookie を上流へ転送しない**ことで解く。代償: 静的ファイルの要求にも付く。大きさは未計測（`sub` だけの JWT なら数百バイトとみられる。推測）。
- **上流が 401 のとき:** 再送できる要求だけ、`/token` で 1 回取り直して再送する（いまの `authFetch` と同じ形）。取り直せなければ 401（画面がログイン画面へ戻す）。
- 経路名の接頭辞は、`/api/auth/*` と、ローカルの `/documents/:path*` の rewrites と重ならない名前にする（実装時に決める）。

### 5. SSE（`/chat/stream`）の経路
**推奨: 当面は、案 A のまま（チケットで Go の中継へ直結）。案 B は、ステップ 2 の計測のあとで再評価する。**
- 理由: B には 3 つの未計測がある（切断の伝播・Free の CPU・レート制限のキー）。直結なら、ブラウザーが中継へ直接つなぐので、中継のレート制限のキー（クライアント IP だけ）は、いまのまま意味を持つ（`ops/sse-relay/main.go`。読んで確認）。B にすると、中継が見る接続元は画面の Worker の送信元になり、利用者ごとの制限が効かなくなる恐れがある（Cloudflare の送信元 IP が利用者ごとに違うかは未確認。`SSE_RATE_LIMIT_TRUST_XFF` の扱いも要る）。
- **ただし、B が計測で成り立つなら、チケット（Secret・FastAPI の検証・発行）が丸ごと要らなくなる。** チケットの実装は、ステップ 2 の計測よりあとに置く（実装の順番）。これが本案の要点。
- 選ばなかった案: 最初から B。計測が無いので見送る。

### 6. 60 秒チケット（直結の場合）
**(a) 渡し方。推奨: 専用のリクエストヘッダー**（名前は実装時に決める。例: `X-Stream-Ticket`）。
- 現行のチャットは、`fetch` の POST で `Authorization` を付けて、Go の中継へ直接送る（`frontend/src/hooks/useChat.ts` の `DefaultChatTransport` の `fetch` が `createAuthedFetch`。接続先は `api/chat.ts` の `streamEndpoint`。読んで確認）。同じ経路でヘッダーを付けられる（`EventSource` はヘッダーを付けられないが、使っていない）。Go の中継は `ReverseProxy` で、ヘッダーをそのまま上流へ渡し、`OPTIONS` も上流へ渡す。FastAPI の CORS は `allow_headers=["*"]`（読んで確認）。**中継のコードは変えなくてよい見込み**（既存の試験は、`Authorization` の転送を確かめ、独自ヘッダー `X-Json` が上流へ届くことを別の試験の前提に使っている。専用ヘッダーの転送そのものを確かめる試験は無いので、足す）。**したがって 0005（中継は認証を持たず、検証は上流）は変わらない**（J4・J7 は「中継にも変更が要る」「0005 の一部を変える」と書いたが、ヘッダー渡しなら当たらない）。
- 専用ヘッダーにする理由: ほかのエンドポイントは `Authorization: Bearer` の JWT だけを読むので、チケットは `/chat/stream` 専用の依存だけが読む形になり、用途の限定が構造で守られる。JWT と形が混ざらない。
- 選ばなかった案:
  - URL のクエリ: **使わない決まり**（ログ・履歴・Referer に残る）。パスやフラグメントに入れる案も、同じ理由で見送る。
  - Cookie: 中継は別のサイト（`*.run.app`）なので、画面の Worker の Cookie は届かない。`SameSite=None`・`credentials: 'include'`・中継の `Allow-Credentials` が要るが、中継は意図して付けず（コードのコメント）、サードパーティの Cookie の制限にも当たる。起案時の「Cookie で渡す」は、成り立たない。
  - `Authorization: Bearer <チケット>`（JWT と同じヘッダー）: `current_user` が両方を読み分けることになり、チケットが全エンドポイントで通る恐れがある。
  - リクエストの本文: 中継は本文を上限（1 MiB）まで読み切ってから上流へ送る。`ChatRequest` の型も汚れる。

**(b) 署名と検証。推奨: BFF が HMAC-SHA-256 で署名し、FastAPI が、JWT 検証とは別の関数で検証する。**
- 専用の Secret を、画面の Worker と Cloud Run の両方に置く（`BETTER_AUTH_SECRET` は流用しない）。形式は JWT にしない（`alg` を持たない固定の形。実装時に決める）。中身は `sub`・`exp`（発行から 60 秒）・`jti`・用途（`chat-stream`）。FastAPI は、`/chat/stream` 以外では受け付けない。
- 理由: 発行のときに、`docunavi-api`（17 ms 以上と DB の SELECT）も Cloud Run も呼ばない。`sub` は、Cookie の JWT（AEAD で認証済み）から取る。状態が要らない。チャットのたびに Better Auth の ES256 の署名を使うと、Free では成り立たない（J7 note 3）。
- **方針との関係（J9 R6）:** FastAPI は、共有秘密による検証を意図して持たない（`ALLOWED_ALGORITHMS` は ES256・RS256 だけで、鍵が無いときは共有秘密へ黙って切り替えず、起動を失敗させる。`app/auth.py`、[configuration.md](../configuration.md) の「ログイン（認証）」。読んで確認）。この方針は「JWT の検証方式を黙って落とさない」ためのもので、別の用途の MAC のチケットは、方針の例外ではなく別の追加と読める。**ただし、読み方は利用者の判断。** 承認を得るときの条件: JWT と別の検証関数にする。`alg` を持たない固定の形にする。JWKS の経路と混ぜない。この ADR に例外として書く。取り違えのテストを足す（JWT をチケットの欄に入れる・チケットを `Authorization` に入れる、など）。
- **限界:** HMAC の Secret を画面の Worker に置くので、その Worker が侵害されると、任意の `sub` のチケットを偽造できる（JWT なら、署名の鍵は認証用 Worker だけが持つ）。ただし、画面の Worker が侵害された時点で、全利用者の Cookie を読めるので、増える害は `/chat/stream`（LLM の費用と、他人の会話の履歴への書き込み）に限られる（この ADR の補足）。
- 選ばなかった案:
  - FastAPI が不透明な乱数を発行する（BFF が JWT で発行用の API を呼ぶ）: 状態を Cloud Run の共有の保存先に置く必要がある。DB なら、チャット 1 回に 1〜2 operation で、0012 の月 20 万に響く。インスタンスをまたぐのに、プロセス内のメモリでは足りない（インスタンス数の現状は未確認）。HMAC 案の利点（無状態）を、そのまま打ち消す（J9 R6）。
  - ES256 の非対称チケット（画面の Worker が秘密鍵を持ち、FastAPI は公開鍵だけを持つ。J7・J9 に無い。この ADR の補足）: FastAPI の既存の方針と合うが、Worker に秘密鍵を置く点は同じで、Web Crypto の P-256 の対応と CPU が未確認（J5『5』）。**利用者が方針の例外を望まないときの代案として残す。**
  - Better Auth の `/token` でチケットも作る: チャットのたびに 17 ms 以上。

**(c) 1 回限り。推奨: 「インスタンス内で 1 回（ベストエフォート）」に緩める。** FastAPI のプロセス内のメモリで、`jti` を 60 秒だけ控え、同じインスタンスでの再利用を断る。インスタンスをまたぐ再利用は防げない。
- 理由: ヘッダーで渡す（URL に載らない）ので、漏れる経路（ログ・履歴・Referer）が小さい。XSS は、1 回限りでも、新しいチケットを作れる（防げない）。厳密な 1 回限りは、共有の状態（DB）が要り、チャット 1 回に 1〜2 operation で 0012 に響く。
- 起案時の「1 回だけ使える」は、暫定で、利用者が個別に確認した条件ではない（決定済みの節）。**緩めてよいかは、利用者の判断（未決）。**

**(d) 発行。** 同一オリジンの BFF の `POST`（許可リストと同じ検査）。Cookie の JWT が期限間近なら、先に更新してから発行する。応答は `{ticket, expiresAt}`。キャッシュしない。画面は、送信のたびに取る（再送・401 のあとの再試行も、新しく取る）。
**(e) 失敗。** 中継が 429・503・413 で断ったときは、チケットは使われていないので、同じものを再送してよい。FastAPI に届いたあと 401 なら、新しいチケットで 1 回だけ再試行する（いまの `authFetch` と同じ形）。Cloud Run のコールドスタートで 60 秒を超えたときも、新しく取って再試行する。
**(f) 接続の途中。** 認証は、`/chat` と `/chat/stream` の FastAPI の依存（`Depends(current_user)`）が接続の開始に 1 回行うだけで、ストリーム中の再検査は無い（`verify_token` を呼ぶのは `current_user` だけ。読んで確認）。チケットも開始のときだけ検査すれば、ストリームの長さは、15 分の JWT の期限から切り離される（J9 生存 3）。

### 7. 縮退（`/token` が失敗する・打ち切られるとき。J9 R3）
**推奨:**
- 更新は、要求が来たときだけ行う（タイマーで一斉に動かさない）。利用者ごとに更新の時刻が散るので、打ち切りが始まっても、全員が同時に失敗しない。
- 更新が失敗した（1102・5xx・通信の失敗）とき、手元の JWT が `exp` と leeway（10 秒）のうちは、それを使う（更新は期限の 120 秒前から試すので、2 分近い余裕がある）。尽きたら 503（固定の日本語の文と `Retry-After`）。画面は、ログアウトにせず、いまの `TokenUnavailableError` と同じ扱い（`authFetch.ts` の「ログイン状態を確認できませんでした」）。自動の再試行の連打はしない。
- 打ち切り（`exceededCpu`・1102）が Observability に出たら、ステップ 7 のゲートを戻す（`/token` の許可を戻す＝従来の Bearer 直送へ）。それでも収まらないときだけ、Paid を相談する（決定済みの節）。

## 草案との食い違いの整理
起案時の草案（2026-11-21 の最初の版）と、J4・J5・J7・J9 の記述が食い違う点。「読んで確認」は、2026-11-21 に worktree のソースか依存の配布コードを読んだもの（実行はしていない）。

| # | 草案・調査の記述 | 食い違い | 正しいと見る側と根拠 | 反映 |
|---|---|---|---|---|
| 1 | 草案の案 A:「画面側の CPU は小さい」。J7:「Free に収まる見込み（probably）」 | J9 R1:「unknown」 | **J9。** 画面の Worker の CPU は 1 件も測っていない（草案の「制約」自身も、実測は `docunavi-api` だけと書く）。J7 自身の理由 6 も「最大の未確認」と書き、probably と矛盾する | 案 A の評価を「未計測」に直した。ステップ 0・2 |
| 2 | 草案:「`/token` の回数をキャッシュで減らせば、収まる方向」 | J9 R2: `get-session` のたびの署名が残る | **J9。** `get-session` の after フックが署名し、`disableSettingJwtHeader` は未設定で、画面は `set-auth-jwt` を読まない（読んで確認）。ただし、署名が重いのか組み立てが重いのかは、どちらの説も残る（未測定） | 決定（案）2(a)。最初の 1 手 |
| 3 | J7:「`definePayload` で `/token` が軽くなる（3 点の 1 つ）」 | J9 R2: CPU の根拠が無い | **J9。** 署名の対象が数百バイト減るだけ（コードを読んだ推論）。効くのは Cookie の大きさ | 目的を Cookie の大きさと個人情報に限った（2(b)） |
| 4 | 草案:「0013 の『署名付き URL は作らない』『API の認証は Bearer のみ』を置き換える」 | 設計（J7 note 1）はヘッダー渡しで、URL に載せない | **設計に合わせて直す。** URL に載せないので、「署名付き URL は作らない」は破らない（0013 の本文と、[roadmap.md](../roadmap.md) の方針）。置き換わるのは、「Bearer のみ」（`/chat/stream` だけ）と、「JWT は保存・共有キャッシュしない」（暗号化した Cookie に保存） | 状態の欄の「置き換える範囲」 |
| 5 | 草案:「1 回だけ使える」 | J7 note 4・J9 R6: 厳密な 1 回限りは共有の状態が要る | **緩める案。** ヘッダー渡しでは、1 回限りの価値が小さい（上の 6(c)）。暫定で、利用者の確認を経ていない条件だった | 6(c)。未決 |
| 6 | 草案:「Cookie で渡す」は意味が曖昧 | J7 note 1: ヘッダー | **J7。** Cookie は、中継が別のサイトなので届かない（公開サフィックスリストと中継のコードで確認） | 6(a) |
| 7 | 草案の未確認:「チケットを使う場合の、Go の中継のレート制限のキー」 | 直結なら、ブラウザーが直接つなぐ | 直結では**変わらない**（キーはクライアント IP。読んで確認）。問題になるのは案 B のときだけ | 5。未確認の欄を直した |
| 8 | J4:「チケットで直結するには、FastAPI と中継の両方に変更が要る」。J7:「0005 の決定の一部を変える」 | 中継は、ヘッダーをそのまま渡す | **中継は無変更の見込み。** `ReverseProxy` が全ヘッダーを渡し、`OPTIONS` も上流へ渡す（読んで確認）。独自ヘッダーの転送は、既存の試験が前提に使っているが、転送そのものを確かめる試験は無い | 6(a)。0005 は変えない |
| 9 | 草案:「`Origin`・`Sec-Fetch-Site` の検査を設計に入れる（0013 の中継は、`cross-site` を 403 で断っている）」。検査の中身は未定で、0013 と同じ検査で足りるように読める | J9 R5: `workers.dev` は公開サフィックス | **J9。** 公開サフィックスリストで確認（行 12806）。`cross-site` だけを断ると、同じサイトの別の Worker からの要求を通してしまう | 4。PDF の中継も直す |
| 10 | J7: JWT の Cookie は `Path=/api` | J9 R5・R9: `__Host-` と両立しない。認証の中継にも付く | **J9。** `__Host-`（`Path=/`）にして、認証の中継で Cookie を落とす。代償は、静的ファイルにも付くこと | 4 |
| 11 | J7:「期限切れ直前の SSE は、JWT が接続の始めにだけ検査される見込み（推測）」 | J9 生存 3: 確認できた | **推測から確認へ（読んで確認）。** 依存は接続の開始に 1 回だけ。ストリーム中の再検査は無い | 6(f) |
| 12 | 承認のときの、チケットの理由:「Bearer を付けられない経路のため」 | 現行の画面は、`fetch` の POST で `Authorization` を付けて中継へ送る | **コードが正しい**（読んで確認。`useChat.ts`・`api/chat.ts`）。`EventSource` ではないので、ヘッダーを付けられる。チケットが要るのは、BFF のあとに、画面が JWT を持たないから | 決定済みの節に注記。6(a) |

## 反証で見つかった穴と対策
J9 の反証（R1〜R9。出典の「R の番号」）と、この ADR で足した穴（R10 以降）。

| # | 穴 | 確認の状態 | 対策（節） |
|---|---|---|---|
| R1（重要） | 画面の Worker の CPU が未計測で、「Free に収まる」と言えない | 計測が無いことを確認（草案の制約）。実測は未 | ステップ 0・2。合格の目安は、ステップ 2 |
| R2（重要） | `get-session` のたびの署名が残る。`definePayload` に CPU の根拠が無い | 読んで確認 | 2。ステップ 1 |
| R3（重要） | 「一貫して」の定義が無く、打ち切りが始まったときの縮退が設計に無い | 公式の文書の読み（B）。打ち切りの条件は未確認 | 7 |
| R4（重要） | 内部の `/token` が返す `Set-Cookie` を捨てると、7 日でログアウトになる見込み | コードを読んだ推論。実機は未確認 | 4。ステップ 3・5 の試験 |
| R5（重要） | `workers.dev` が公開サフィックスで、同じアカウントの Worker は same-site。`SameSite=Lax`・`cross-site` の検査では防げない。cookie tossing | 公開サフィックスリストで確認。同じアカウントの別の Worker は未確認 | 4（厳密な同一オリジン・`__Host-`） |
| R6（重要） | HMAC のチケットは、FastAPI の共有秘密を持たない方針と食い違う。1 回限りの状態の置き場 | 読んで確認 | 6(b)(c)。未決 |
| R7（軽微） | 1 日 10 万リクエストの枠が、設計で触れられていない | 草案の制約にある。静的ファイルの扱いは未確認 | 「結果と限界」。ステップ 3 で画面 1 枚の API 呼び出し数を数える |
| R8（軽微） | `session.token` が読める。チケットの発行 API は JS から呼べる。許可リストのほうが堅い | 読んで確認（`session.token`）。迂回の試験は J9 の報告 | 3 |
| R9（軽微） | `Path=/api` の Cookie が、認証の中継にも付き、上流へ転送される | 読んで確認（中継は `host` 以外の全ヘッダーを転送） | 4（認証の中継で落とす） |
| R10（追加） | Cookie が空のときの並行要求が、その数だけ `/token` を呼ぶ | 設計からの推論。未測定 | 1 の「穴」。ステップ 3 で数える |
| R11（追加） | 自動テスト用ログイン（`sign-in/email`・`sign-up/email`）が、許可リストから漏れる | 読んで確認（`workers/api/src/auth.ts`） | 3 |
| R12（追加） | `set-auth-jwt` を使う別のクライアントが、このリポジトリの外にあれば、2(a) で壊れる | このリポジトリは grep で 0 件。外は未確認 | ステップ 1 の前に確かめる |

## 実装の順番と確認方法
**各ステップは、独立してマージ・配備できる単位。** 配備は、手動の `deploy.yml` を、そのたびに利用者が承認して行う（前の承認は次に及ばない）。Secret の登録は、利用者が承認して実行する（`wrangler secret put` は `deploy.yml` の経路に含めない。Cloud Run 側は、本番と共用の Secret Manager なので、理由と影響を示して承認を得る）。実 API・課金は使わない。ステップ 3 以降は、ステップ 2 の結果のあとに始める。

| 手 | 内容 | 確認方法 |
|---|---|---|
| 0 | **測る（読み取りだけ。コードなし）。** `docunavi-web` の Observability で、CPU（P50・P99）と `exceededCpu`・1102 を、いまある `/api/auth/*` の中継と PDF の中継について読む。`docunavi-api` は、`get-session` と `/token` を分けて読む（J9 の open question 1） | ダッシュボードの Metrics・Logs の読み取り。結果は、日付つきでこの ADR の「制約」へ書く。**サンプルが無いときは、「測れなかった」と書く**（本番は、DB の上限で、12-01 まで止まっている。2026-11-21 時点で `docunavi-api` の直近 24 時間の呼び出しは 0）。その場合は、ステップ 2 の計測が先になる |
| 1 | **最初の小さな 1 手。** `workers/api/src/auth.ts` の `jwt()` に `disableSettingJwtHeader: true` を足す（1 行。BFF と無関係。戻しも 1 行）。先に、このリポジトリの外の `set-auth-jwt` の利用が無いかを確かめる（R12） | `cd workers/api && npm run typecheck && npm test`。ローカルの標準構成（`scripts/dev-local.sh`。手順は [development.md](../development.md)）で、`get-session` の応答に `set-auth-jwt` が無いこと、`/token` は従来どおり `token` を返すこと、ログインと API の呼び出しが動くこと（`cd frontend && npm run test:e2e`。偽の LLM）。効果は、配備のあとの Observability で、`get-session` の CPU を前後で比べる（本番の DB は 12-01 まで止まっているので、比べるのはローカルか 12-01 以降） |
| 2 | **実験（製品に入れない）。薄い中継だけの CPU と、SSE の切断。** (i) OpenNext の Route Handler 経由、(ii) OpenNext の手前に置くカスタムの Worker のエントリ経由（OpenNext での扱いは未確認）の 2 通りで、復号・暗号化・`Set-Cookie` を含む 1 リクエストの CPU。20 MiB の PDF の素通し（本文を読まずに流す）。利用者が切れたときに、上流が取り消されるまでの時間（案 B の未計測）。AES-GCM と HMAC の 1 回の CPU。`sub` だけの JWT と、暗号化後の Cookie の実サイズ（既存の Cookie との合計） | `wrangler dev` の CPU プロファイラ（配備した Worker では、`performance.now()` と `Date.now()` が I/O でしか進まないので、前後の差では測れない。J5『6』）。配備して測るなら Observability（承認）。**目安（案）: P99 が 10 ms 以内なら進む。** 超えるなら、カスタムのエントリを試す。それでも超えるなら、この案を進めず、利用者に相談する（見送り・Paid）。B が成り立つかも、ここで決める |
| 3 | **BFF の中継（GET の読み取り系だけ）と、Cookie のキャッシュ。** 許可リスト・厳密な同一オリジン検査・上流の `Set-Cookie` の転送・縮退・準備の要求。画面は、旧経路（Bearer 直送）のままにして、公開してよい真偽の値で切り替える | Vitest（`route.test.ts` と同じ形。`cd frontend && npm run typecheck && npm test`）: Cookie だけで Bearer に変わる。許可外のパスは 404。`cross-site`・同じサイトの別の Worker（`Sec-Fetch-Site: same-site`）・`Origin` の不一致は 403。上流の `Set-Cookie` が転送される。復号の失敗はログアウトにならず取り直す。`/token` の失敗で、期限内は古い JWT を使う。E2E（Playwright。偽の上流）: 2 つのタブと、時計を進めたときの `/token` の本数。Cookie が空で並行 N 本のとき、1 本になること。画面 1 枚の API 呼び出し数（1 日 10 万リクエストの見積り） |
| 4 | **GET 以外（チャットの非ストリーム・会話の削除など）。** `Origin` の厳密な照合と、JSON の `Content-Type` の検査 | ステップ 3 の試験を、POST・DELETE へ広げる（`Origin` の不一致・`text/plain` の本文・プリフライトに CORS のヘッダーを返さないこと） |
| 5 | **PDF の中継を、同じ Cookie の JWT に切り替える。** 同一オリジンの検査をそろえ、`Set-Cookie` を転送する | `route.test.ts` を更新。PDF を 1 回開くたびの `/token` の数（Cookie が温まっていれば 0）を、E2E で数える |
| 6 | **SSE。** ステップ 2 で B が成り立てば B（チケット不要）。成り立たなければ、チケット: FastAPI の検証（別の関数・用途の限定・`jti` のインスタンス内の控え）、BFF の発行、画面は送信のたびに取って専用ヘッダーで送る。Secret の登録 | `pytest tests/test_auth.py` の隣に、期限切れ・別の用途・改ざん・別の Secret・`sub` の形・同じインスタンスでの再利用・JWT をチケットの欄に入れた場合・チケットを `Authorization` に入れた場合を足す。Go の中継: 独自ヘッダーが上流へ届く試験（`cd ops/sse-relay && go vet ./... && go test -race -count=1 ./...`）。E2E: JS のメモリ・`localStorage`・`sessionStorage` に JWT が無い状態で、チャットが動く。停止ボタン（切断）で、サーバーが取り消す（既存の規則） |
| 7 | **ゲート（最後）。** 認証の中継を許可リストにして `/token` を塞ぎ、画面の旧経路（`getAccessToken`・Bearer 直送）を外す。`frontend/CLAUDE.md` などを同時に直す（末尾） | E2E で、ページ内の `fetch('/api/auth/token')` が 404。`get-session` の応答に `set-auth-jwt` が無い。ページが `run.app` への要求に `Authorization` を付けていない（ネットワークのログ）。`sign-in/social` から `sign-out` まで通る。戻し方は、許可リストを戻して再配備 |

検査は、まとめて `scripts/verify.sh [py|frontend|worker|go|all]` でも実行できる（CI と同じ読み取り検査。ビルド・E2E は含まない）。

## 結果と限界（予想。実装していないので、すべて未実測）
- 直る（動機 1）: JWT が JavaScript から読めなくなる（`/token` と `set-auth-jwt` を塞いだあと）。XSS で、15 分ぶんの Bearer を持ち出される経路が減る。持ち出せる資格は、60 秒・チャット専用のチケットだけになる（ステップ 6 のあと。B が成り立てば、それも無くなる）。
- **直らない:** XSS が起きている間に、攻撃者が、その利用者のセッションで API を操作すること（Cookie は、同じ画面から送られる）。守れるのは、トークンの持ち出しであって、XSS そのものではない。CSP・出力のエスケープなど、XSS を防ぐ対策は別に要る。`session.token` は JS から読める（決定（案）3 の限界）。
- **失効の限界（全案に共通）:** API は JWT の `exp`・`iss`・`aud`・`sub` だけを見て、セッションを見ない（読んで確認）。サインアウトのあとも、発行済みの JWT は `exp`（最長 15 分）まで有効。Cookie が盗まれても同じ。サーバー側でセッションを失効させても、Cookie の写し（5 分）のあいだは、`/token` が新しい JWT を出せる（[configuration.md](../configuration.md)）。有効期限を短くすれば、`/token` の回数が増える（トレードオフ）。
- 増える: 画面の Worker の CPU・リクエスト（1 日 10 万の枠。いまは API 呼び出しを Cloud Run へ直送していて数えない）・サブリクエスト（通常 1 本、更新のとき 2 本。50 本に対して余裕）。Secret が 2 つ（Cookie の鍵・チケットの MAC。後者は直結の場合）。Cookie だけで API を叩ける経路（CSRF の検討が、BFF の全経路に及ぶ）。チケットの発行と検証（API に足す変更）。
- 減る: `/token` の回数（タブごとからブラウザーごとへ。PDF を開くたびの呼び出しは、Cookie が温まっていればなくなる見込み）。それに伴う `jwks` の SELECT（0012 の操作数）。`get-session` のたびの署名（2(a)）。
- 戻し方: ステップ 7 より前なら、画面を旧経路へ戻す（切り替えの真偽の値）。ステップ 7 のあとは、許可リストを戻して再配備する。データ・鍵・`iss` は動かさない。
- **未確認:** 画面の Worker の CPU（Free の 10 ms）。OpenNext 越しの SSE の切断。AES-GCM・HMAC の 1 回の CPU。JWT と Cookie の実サイズと、ブラウザーごとの Cookie の上限（RFC 6265 の 4096 バイトは最小の目安）。Free の「一貫して」の意味と、`docunavi-api` で超過のエラーが 0 件の理由。1 日 10 万リクエストに静的ファイルが数えられるか。`Set-Cookie` を転送しないと 7 日でログアウトになることの、実機での再現。同じアカウントの別の Worker の有無。Cloud Run の FastAPI のインスタンス数。`set-auth-jwt` を使う、このリポジトリの外のクライアントの有無。

## 未決
**利用者の判断が要る**
1. チケットの「1 回限り」を、インスタンス内の控え（ベストエフォート）に緩めてよいか（推奨: 緩める。6(c)）。
2. FastAPI に、JWT と別の MAC のチケットの検証を足すことを、方針の例外として認めるか（推奨: 条件つきで認める。6(b)）。認めないなら、ES256 の非対称チケットか、不透明な乱数（状態は DB）。
3. JWT の Cookie を `__Host-`・`Path=/`（静的ファイルにも付く）にしてよいか（4）。
4. ステップ 2 の合格の目安（P99 が 10 ms 以内）を、この値にしてよいか。
5. 自動テスト用ログイン（`sign-in/email` など）を、許可リストに残すか（3）。使わなくなったなら、外せる。

**計測のあとで決まる**
- SSE の経路（B か、チケットか。ステップ 2）。
- 重さの根（署名か、`betterAuth()` と `Pool` の組み立てか）に応じた、追加の軽量化（2。ステップ 0・1）。
- 画面の Worker のエントリを、OpenNext の手前に置くか（ステップ 2）。

## 決定になったときの作業（今は行わない）
0016 が「決定」になったとき（利用者の承認のあと）に行う。いまは、どれも直していない。
- **[0013](0013-pdf-via-same-origin-relay.md) の状態の行を直す。** 本文は書き換えない（`decisions/` の決まり）。例: 「一部置き換え済み（0014 が表示の方式〔iframe〕を、0016 が認証の扱いの一部〔`/chat/stream` に、Bearer 以外のチケットを許す。JWT を暗号化した Cookie に保存して使い回す〕を置き換えた。中継は有効）」。そのうえで、末尾の追記に、0016 への参照を 1 行足す。
- [README.md](README.md) の 0013・0016 の行（状態の語は、同じ README の「書き方」に従う）。
- `frontend/CLAUDE.md`（先頭の SPA の規則と例外の一覧。「層の境界」の `fetch` の規則。環境変数の規則）と、`.claude/rules/frontend.md`。**改める範囲:** 足してよいサーバー側の処理は、認証の中継（許可リスト化）・PDF の中継・BFF の中継・チケットの発行の、4 種に限る。Server Components・Server Actions・画面のサーバー側の描画は、引き続き足さない。`fetch` を `api/` と `auth/` に閉じる規則は、画面の `fetch` が同一オリジンの BFF を向くだけで、保たれる。
- [architecture.md](../architecture.md) の「ログインの流れ」（Bearer 直送の図と文）、[configuration.md](../configuration.md) の「ログイン（認証）」（チケットの Secret・検証）、[api.md](../api.md)（`/chat/stream` のヘッダー）、[deploy.md](../deploy.md)（Secret の登録）。`ops/sse-relay/CLAUDE.md` は、変更が要らないことを確かめる。

## 出典
- **J4・J5・J7・J9:** 保存済みの Workflow の結果（Claude Code のセッション `ae14f0d7-092b-4a19-a85a-21e8b35f5d18` の `subagents/workflows/wf_a4e4a5e9-cfe/journal.jsonl`。ホストの `~/.claude/projects/` の下にあり、リポジトリには入っていない）。JSON Lines で、番号は行番号（1 始まり。`started` の行も数える）。**4 行目: research:repo（リポジトリの調査）、5 行目: research:platform（Cloudflare 等の公式文書の調査）、7 行目: design（設計）、9 行目: critique（反証）。** 各エージェントの戻り値は、調査の報告であって事実ではない。
  - 引用の書き方: J4・J5 は、戻り値の `findings` の `topic`（例: J4『3. /token と get-session の処理』）。J7 は、`candidates`・`recommendation`・`rationale`（理由 n）・`must_measure`・`sse_ticket_note`（note n）。J9 は、`refutations` の n 番目を **R n**（1 始まり。R1〜R9）、`survives` の n 番目を「生存 n」、`open_questions` を「open question n」。
  - 外部の公式の文書（Cloudflare・RFC・OpenNext）の内容は、J5 の要約（証拠等級 B）。**この ADR では再確認していない。**
- **読んで確認（2026-11-21。実行はしていない）:** worktree のソース `frontend/src/auth/betterAuth.ts`・`frontend/src/auth/authFetch.ts`・`frontend/src/hooks/useChat.ts`・`frontend/src/api/chat.ts`・`frontend/app/api/auth/[...all]/route.ts`・`frontend/app/api/documents/[id]/file/route.ts`・`frontend/wrangler.jsonc`・`frontend/deploy-web.sh`・`workers/api/src/auth.ts`・`workers/api/src/index.ts`・`workers/api/wrangler.jsonc`・`app/auth.py`・`app/api/routes.py`・`app/main.py`・`ops/sse-relay/main.go`・`ops/sse-relay/main_test.go`・[configuration.md](../configuration.md)。依存 `better-auth` 1.7.18（`workers/api/package.json` と `frontend/package.json` で固定）の配布コードは、worktree に `node_modules` が無いので、本体のチェックアウトの同じ版を読んだ（`dist/plugins/jwt/index.mjs`・`sign.mjs`・`adapter.mjs`・`types.d.mts`、`dist/api/routes/session.mjs`、`dist/api/middlewares/origin-check.mjs`、`dist/cookies/index.mjs`、`dist/client/session-refresh.mjs`、`@better-auth/core` の `dist/db/get-tables.mjs`、`better-call` の `dist/router.mjs`）。`set-auth-jwt`・`disableSettingJwtHeader`・チケットの語は、この ADR を書く前のリポジトリ全体（`node_modules` を除く）の grep で 0 件。
- **公開サフィックスリスト**（`publicsuffix.org` の `public_suffix_list.dat` を 2026-11-21 に取得。証拠等級 A）: `workers.dev` と `pages.dev`（Cloudflare の項）、`*.run.app`。
- 実績: 2026-11-21 の Cloudflare ダッシュボード（Workers & Pages の `docunavi-api` の Metrics〔直近 30 日〕、Observability の Logs〔直近 7 日。`/api/auth/token` の `$workers.cpuTimeMs` の集計〕、Workers plans）。読み取りだけ。
- 文書: [0005](0005-go-sse-relay-without-prod-limit.md)、[0009](0009-drop-custom-domain-and-worker-front.md)、[0012](0012-accept-prisma-free-limit-and-cut-operations.md)、[0013](0013-pdf-via-same-origin-relay.md)、[0014](0014-pdfjs-viewer.md)、`frontend/CLAUDE.md`、`ops/sse-relay/CLAUDE.md`
