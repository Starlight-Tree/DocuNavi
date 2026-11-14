# docs/（文書）

全体の方針は、ルートの [CLAUDE.md](../CLAUDE.md)。文書の入口は [README.md](../README.md) の「ドキュメント」の表と、全文書の索引（状態つき）の [README.md](README.md)。文書を足す・状態が変わるときは、索引も直す。

## どの文書が何の正本か

| 知りたいこと | 文書 |
|---|---|
| いま動いている構成・処理の流れ・環境 | [architecture.md](architecture.md) |
| 将来の構成の方向と、採用する条件 | [target-architecture.md](target-architecture.md)（実装の契約ではない） |
| 順序と状態（何が済み、何が待ちか） | [roadmap.md](roadmap.md)（**設計の詳細は置かない**） |
| 重要な決定と、その理由 | [decisions/](decisions/README.md)（1 件 1 ファイル） |
| API の仕様 | [api.md](api.md)。環境変数と LLM の挙動は [configuration.md](configuration.md) |
| 検索の設計 | [search-design.md](search-design.md)。ガードレールは [guardrails.md](guardrails.md) |
| 開発・テスト・CI | [development.md](development.md)。配備と本番運用は [deploy.md](deploy.md) |
| 領域ごとの設計・手順 | `access-control-plan.md` `ingestion-stage4-design.md` `r2-*.md` `go-architecture-plan.md` `mcp-tool-design.md` |
| 画面のバックログ・UX の調査 | [backlog.md](backlog.md)、[ux-research.md](ux-research.md) |

領域ごとの規約は、各領域の `CLAUDE.md`（`app/` `frontend/` `evals/` `tests/` `ops/django-saas/` `ops/sse-relay/`）。コードの置き場所の表は architecture.md。

## 書くときの決まり

- **事実と未確認を分ける。** 確認していないことを「確認済み」と書かない。測定は、日付・質問の数・何回測ったか・証拠等級（A: 実機・実測 / B: 公式の文書 / C: 第三者の記事 / 不明）を添える。
- **日付を入れる。** 状態や数字は変わる。「2026-11-01 に確認」のように書く。
- **現状を書く文書（`roadmap.md` `architecture.md` `deploy.md` など）は、現状に合わせて更新する。** 古くなった記述は直すか消す。完了済みの詳細な経緯と、重複した記録は消してよい（経緯は Git の履歴にある）。経緯を残すためだけの新しい文書は作らない。
- **消す前に、参照元を調べる。** 他の文書・コードのコメント・`.claude/rules/` から、その節や項目番号（`roadmap.md` の ①〜⑧、段階 0〜5）が参照されていないかを `grep` で確かめる。番号は参照されるので、項目を消しても番号は残す。測定の結果で、ほかの文書が根拠にしているものは、移すか要約を残してから消す。
- **ロードマップに設計の詳細を足さない。** 設計・測定の記録は別の文書に置き、ロードマップの行からリンクする（2026-11-01 に、段階 4 の設計と ⑧ の計画を分けた）。
- **重要な設計判断は decisions/ に残す。** 判断とその理由は、消す前に 1 件 1 ファイルで残す。決定を変えるときは、古いファイルを書き換えず、新しいファイルで置き換える（`decisions/` だけは、経緯が正本）。
- **文書を動かす・改名するときは、リンクを全部直す。** `grep -rn "<ファイル名>" . --include='*.md' --include='*.py' --include='*.ts*' --include='*.yml'` で、文書・コード内のパス・コメントの参照を探す。
- **秘密を書かない。** 実キー・トークン・接続 URL の値、`.env` の中身を書かない（`AIza…` の形も）。登録語は `scripts/check-sensitive-terms.sh` で検査する。
- 画像・図は、実データか、そうでないかを明記する。実機で確認していない競合の画面を「確認済み」と書かない（`.claude/rules/ui-review.md`）。
- 文書だけの変更は、テスト不要。リンク切れだけは確かめる。
