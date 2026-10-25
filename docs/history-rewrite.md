# 履歴の書き換え（2026-10-25）

Git の履歴を整理し直し、`main` を強制プッシュで置き換えた。文書にある古いハッシュを見つけたら、この文書で対応を確かめる。

## 何を変えたか

- コミットは 322 件から 194 件になった（このコミットを含む）。同じ話題の小さなコミットを束ね、差し引きゼロの組は消した。
- 作者とコミッターは、全コミットで `Starlight <154563471+Starlight-Tree@users.noreply.github.com>` にした。以前は `old-name` と `old-account` の 2 つの名義だった。
- 作者日時とコミッター日時は元のままにした。束ねたコミットは、束ねた組の先頭のコミットの日時を使う。
- 束ねたコミットの本文には、元の件名と本文を `Squashed commits:` として残した。
- 最終ツリーは書き換え前の `main` と同一。このコミットだけが、次の差分を持つ。
  - 履歴を指すハッシュの付け替え（下の「付け替えたもの」）。
  - この文書の追加。

## 付け替えたもの

コードの履歴を指していたハッシュは、新しいハッシュに直した。

| 古い | 新しい | 場所 |
|---|---|---|
| `be0a305` | `579d80e` | CLAUDE.md（`scripts/mcp-proxy.sh` の確認） |
| `e8f82c0` | `66fe9d7` | docs/search-design.md、evals/README.md、scripts/eval_answers.py |
| `2ec854f` | `1807839` | docs/target-architecture.md |
| `48de586` `b353e4a` `6fde00b` | `ab95451` `2b8aeee` `efe3c97` | docs/r2-rollout.md |
| `56330d7` `8bc9683` `d6125bc` | `2976401` `dd58ef1` `4e4c91e` | docs/roadmap.md |
| `558ee8b` | `daad41c` | evals/results/topk_probe.json |

## 付け替えなかったもの

次のハッシュは、その時点の外部の記録なので、書き換え前のまま残した。履歴の中の新しいハッシュとは一致しない。

| 古い | 新しい（対応するコミット） | 記録の中身 |
|---|---|---|
| `cc5709c` | `4039f51` | 本番のイメージのタグ（docs/deploy.md、docs/r2-rollout.md） |
| `dc969b9` | `cf68f7f` | プレビューのビルドとイメージ（docs/deploy.md、docs/r2-rollout.md）。tests/test_preview_script.py の `dc969b9` は例の文字列 |
| `3d3ab79` | `9028551` | プレビュー時点の `HEAD`（docs/deploy.md） |
| `8154585` | `32c733f` | 配備中のイメージのタグ（docs/r2-ingest-design.md） |
| `bf480b1` | `aa0211c` | Vercel が READY にしたコミット（docs/roadmap.md） |

## 退避

書き換え前の履歴は、ローカルのブランチ `backup-pre-rewrite` に残してある（リモートには無い）。
