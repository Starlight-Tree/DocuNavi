# ザーハード設定資料チャットボット（FastAPI × LangGraph × Gemini）

架空の惑星「ザーハード」の設定資料（`storage/chunk/*.txt`）を根拠に答える RAG チャットボットです。
バックエンド（FastAPI）に加えて、チャット画面（Vite + React + TypeScript、`frontend/`）を同じサーバーから配信します。

- **FastAPI**: HTTP API（`/chat`, `/health`）
- **LangGraph**: 入力ガード → エージェント（ツール呼び出しのループ）→ 出力ガード
- **Gemini**（`gemini-3.1-flash-lite`）: 回答生成。Gemini API キーまたは Vertex AI（ADC）で認証し、どちらも未設定ならモックモードで動く
- **RAG**: 設定資料を文単位に分けて BM25 で検索（外部の埋め込み API・ベクトル DB は不使用）
- **画面**: `frontend/`（Vite + React + TypeScript）。ビルド成果物 `frontend/dist` を FastAPI が `/` で配信する

## 起動

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # GEMINI_API_KEY を設定（未設定ならモックモード）
uvicorn app.main:app --reload
```

Docker Compose を使う場合は、ホスト側で `docker compose up --build` を実行します（`.env` が無くても起動し、その場合はモックモードです）。
イメージのビルド時にフロントエンドもビルドされるので、起動後は <http://localhost:8000> で画面を開けます。

### 画面（フロントエンド）

```bash
cd frontend
npm ci
npm run build        # frontend/dist を作る。FastAPI が / で配信する
npm run dev          # 開発サーバー（http://localhost:5173）。/chat と /health は localhost:8000 へ転送
```

- `frontend/dist` は Git 管理外です。無い状態でも API は動き、`/` はビルド方法を示す 404 を返します（起動後にビルドしても再起動は不要）。
- 配信の仕組み: どのルートにも当たらなかった `GET`/`HEAD` だけが画面側に回り、拡張子のないパスは `index.html`（SPA のルーティング）、
  無いファイル（`/assets` 配下や拡張子付き）は 404 になります。`/chat`・`/health`・`/docs` などの API は影響を受けません。
- 詳細は [frontend/README.md](frontend/README.md)。

| 環境変数 | 既定値 | 説明 |
|---|---|---|
| `GEMINI_API_KEY` | （空） | 空または `your_api_key_here` のときはモックモード |
| `GOOGLE_GENAI_USE_VERTEXAI` | （空） | `true` で Vertex AI を使う。キー不要で ADC（Cloud Run ならサービスアカウント）で認証し、キーの有無にかかわらずモックにならない |
| `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | （空） | Vertex AI 利用時のプロジェクトとロケーション。未設定なら SDK が ADC やメタデータサーバーから解決する |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | モデル名の上書き（Vertex AI はリージョンごとに提供モデルが違う） |
| `CORS_ORIGINS` | `*` | 許可するオリジン（カンマ区切り） |
| `LLM_TIMEOUT_SECONDS` | `60` | 1 回の `/chat` で LLM を待つ上限秒数（ヘッジ・再試行を含む全体。クライアントが切断したらその時点で取り消す）。不正値は既定値に戻る |
| `LLM_HEDGE_DELAY_SECONDS` | `8` | LLM がこの秒数たっても応答しなければ、追加の試行を投げる（下記）。`0` でヘッジを無効にする。小さくしすぎると、毎回 `LLM_MAX_ATTEMPTS` 本をほぼ同時に投げる。不正値（数値でない・負）は既定値に戻る |
| `LLM_ATTEMPT_TIMEOUT_SECONDS` | `30`（ヘッジなしは `15`） | LLM への 1 回の試行を待つ上限秒数（接続・送信・受信の段階ごとの上限）。不正値は既定値に戻る |
| `LLM_MAX_ATTEMPTS` | `3` | 1 回の LLM 呼び出しで投げる試行の最大数（最初の 1 回を含む）。`1` で追加・再試行なし。不正値は既定値に戻る |

### LLM の遅延・失敗への対処

2026-10 に開発環境から観測した範囲では、Gemini（特に `gemini-3.1-flash-lite`）は、1〜3 秒で返る回と、10 秒を超える回・応答がないまま 60 秒でサーバー側に打ち切られる回が混在していました。
待ち続けると 1 回の遅延で `/chat` が 504 になるため、遅い呼び出しは待たずに**同じ要求をもう一度投げて、最初に成功したものを採ります**（ヘッジ）。

- `LLM_HEDGE_DELAY_SECONDS` 秒たっても応答がなければ、追加の試行を投げる（`LLM_MAX_ATTEMPTS` 本まで）。成功したら残りは取り消す。
- 試行が失敗（無応答・接続の失敗・408/500/502/503/504）したときは、0.5 秒後に次を投げる。ほかの試行が結果待ちなら、その結果か次のヘッジの時刻まで待つ。認証や不正なリクエストなど、投げ直しても同じ失敗は即座に返す。
- 429（レート制限）を受けたら、追加を止めて間隔（2 秒、4 秒…）を空けて再試行する。全て失敗したときは 429 を返す（上流が待ち時間を示していれば `Retry-After` が付く）。
- 追加・再試行が起きるとアプリのログに `WARNING` が出る（`app.agents.resilience`）。頻発するなら上流（Gemini）が不安定。
- クライアントが切断したら、追加の試行は投げず、進行中の試行を取り消す（下記「クライアントの切断」）。

実 API で同じ時間帯に交互に流した比較（各 12 回、Gemini が不安定だった時間帯）では、成功率はヘッジなし 5/12、ヘッジあり 8/12 でした。小さな標本なので目安です。

**コストの注意:** 遅い呼び出しでは、1 回の LLM 呼び出しに最大 `LLM_MAX_ATTEMPTS` 本のリクエストが投げられ、その分だけ入力トークンが増えます
（取り消した試行の課金・クォータの扱いは Gemini 側の仕様次第です）。速く返っているときは 1 本のままです。
クライアントが切断したときは、追加の試行を投げるのをやめます。
抑えたいときは `LLM_MAX_ATTEMPTS` を下げるか、`LLM_HEDGE_DELAY_SECONDS=0` で無効にします（その場合は SDK が最大 `LLM_MAX_ATTEMPTS` 回（最初の 1 回を含む）を順に試し、試行の上限は短い `15` 秒になります）。

**容量の注意:** Gemini が全体に遅いときは、1 呼び出しあたり最大 `LLM_MAX_ATTEMPTS` 本が同時に飛びます。SDK の HTTP 接続プール（既定 100 接続）を超えると、追加の試行が待たされて効果が薄れます。
同時に処理する `/chat` の数 × `LLM_MAX_ATTEMPTS` が 100 を大きく超える環境（Cloud Run の同時実行数を上げるときなど）では、確認してください。

### クライアントの切断

クライアントが `/chat` の応答を待たずに切断したとき（タブを閉じる・リロードする・「新しい会話」で中断する・`curl` を Ctrl-C するなど）は、
その LLM 呼び出しを取り消します。切断を検知したあとは新しい試行（ヘッジ・再試行）を投げず（検知と同時に開始した 1 本を除く）、進行中の試行は取り消し、通常は即座に戻ります。
クライアントには何も返りません。記録は `app.api.routes` ロガーの `INFO`（`クライアントが切断したため LLM 呼び出しを取り消しました`、`thread_id` と経過秒のみ）が 1 行です。
uvicorn の既定のログ設定では `app.*` の `INFO` は出力されません（`--log-level` は `uvicorn.*` にしか効きません）。見たいときは、アプリ側でルートロガーを `INFO` にします。

- すでに Gemini へ送った要求の処理や課金は止められません（ヘッジで負けた試行と同じ扱いです）。
- 切断がアプリまで届かない環境（途中のプロキシやロードバランサーが切断を伝えない場合）では、従来どおり、成功するか `LLM_TIMEOUT_SECONDS` に達するまで続きます。
- 取り消したターンのユーザーの発言は履歴に残ります。検索（ツール呼び出し）のあとで取り消したときは、そこまでのツール呼び出しと検索結果も残ります。同じ `thread_id` で続けると、それらも次の発言と一緒に LLM へ渡ります（LLM が失敗・タイムアウトしたターンも同じです）。
  ただし、ツールの実行中に取り消されて結果が付かなかった呼び出しだけは、次のターンの先頭で履歴から外されます（その発話に本文が無ければ定型の一文に置き換わります）。
- リクエストを送ったあと、応答を待たずに閉じるクライアント（投げっぱなし）は、LLM の処理も止まります。応答が要るなら、接続を開いたまま待ってください。

## API

### `GET /health`

```json
{"status": "ok", "mode": "mock"}
```

`mode` は `mock`（ダミー応答）または `gemini`（本物の LLM。Vertex AI の場合も `gemini`）。

### `POST /chat`

```json
{"message": "ザーハードの大気について教えて", "thread_id": "optional-conversation-id"}
```

| フィールド | 条件 |
|---|---|
| `message` | 必須。前後の空白を除いて 1〜2000 文字 |
| `thread_id` | 省略可。`[A-Za-z0-9_-]` で 128 文字以内。省略すると UUID が採番される。同じ値を送ると会話が続く |

レスポンス:

```json
{
  "reply": "…（出典: ザーハード1.txt）",
  "thread_id": "…",
  "context": "[ザーハード1.txt]\n…",
  "mode": "mock"
}
```

`context` は今回のターンで検索された設定資料（検索しなかった・該当なしの場合は空）です。

エラー（`detail` は固定の日本語メッセージで、例外の内容は含まれません。詳細はサーバーログのみ）:

| ステータス | 原因 |
|---|---|
| 422 | 入力検証エラー（空のメッセージ、長すぎる `message`、不正な `thread_id` など） |
| 429 | LLM のレート制限。上流が待ち時間を返した場合は `Retry-After` ヘッダ付き |
| 504 | LLM の応答がタイムアウト |
| 500 | API キー・モデル設定の誤り、想定外の例外 |
| 502 | その他の LLM 呼び出し失敗 |

クライアントが切断した場合は、LLM 呼び出しを取り消して何も返しません（ステータスはクライアントに届かないため、上の表にはありません）。

## ガードレール

ルールベース（LLM を使わない）の入出力チェックです。`app/agents/guardrails.py` にあります。

- **入力**: 指示の上書き、システムプロンプトの開示要求、役割の乗っ取り、チャットテンプレートの偽装を検出すると、LLM とツールを呼ばずに定型文で拒否します。拒否したターンの発言は履歴から伏せ字に置き換えます（NFKC 正規化と不可視文字の除去、空白・記号の挿入に対応）。切断などで検査の前に取り消されたターンの発言が未回答のまま残っていても、次のターンの入力検査で拒否対象なら伏せ字に置き換えます（拒否した発言の原文が、後続の LLM 入力に出ない）。
- **出力**: 回答が `SYSTEM_PROMPT` をほぼそのまま含んでいる場合は定型文に置き換えます。

これは典型的な手口を安く弾くためのもので、プロンプトインジェクションを完全に防ぐものではありません。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

フロントエンドは Vitest です（`cd frontend && npm test`）。

GitHub Actions（`.github/workflows/ci.yml`）が、`main` へのプッシュと PR で、バックエンドのテスト（Python 3.12）、
フロントエンドのテストとビルド、Docker イメージのビルドと起動確認（`/health`・画面・`/chat`）を行います。デプロイは含みません。
アクションのコミット SHA 固定は、Dependabot（`.github/dependabot.yml`）が週次で更新の PR を出します（公開から 7 日待ちます）。手動実行もできます（`workflow_dispatch`）。

依存のバージョンは `requirements.txt` と `requirements-dev.txt` に固定しています（Python 3.12、全プラットフォーム向け）。
どちらも `requirements.in` / `requirements-dev.in` から [uv](https://docs.astral.sh/uv/) で生成したファイルなので、直接は編集しません。
更新するときは `.in` を編集して、次を実行します（`pip install uv` で入ります）。

```bash
uv pip compile requirements.in --universal --python-version 3.12 -o requirements.txt
uv pip compile requirements-dev.in --universal --python-version 3.12 -c requirements.txt -o requirements-dev.txt
```

`tests/conftest.py` が `GEMINI_API_KEY` を空にしてからアプリを読み込むため、`.env` に実キーがあってもテストは常にモックモードで動き、Gemini API を呼びません。

RAG の精度は `tests/rag_eval_cases.py` の評価セット（関連 21 件・無関係 8 件）で回帰テストしています。2-gram 照合では拾えない言い換え（「気温差」→「温度差」など）は既知の取りこぼしです。

## 開発環境の gcloud（ログインとガード）

開発コンテナには gcloud が入ります（`.devcontainer/install-gcloud.sh`）。GCP の参照（Cloud Build のトリガー、Cloud Run のログなど）は、
**あなた自身のアカウントでログイン**して行います。コンテナにログイン情報があるので、次の層で守っています。

| 層 | 内容 | 効くもの |
|---|---|---|
| 日常の ID | ログイン後、`auth/impersonate_service_account` で閲覧専用の `agent-viewer` に固定します。書き込みは 403 になります | 誤操作、素朴な指示の埋め込み |
| PreToolUse フック | `.claude/hooks/guard-cloud-cli.py` が、実行前に gcloud などの呼び出しを検査します。削除・IAM・課金の変更・認証情報の取得・ID の切り替えは拒否し、書き込み系は確認を強制します | 同上、設定の書き換え |
| 権限ルール | `.claude/settings.json`: 読み取り系の gcloud だけ許可し、`~/.config/gcloud` と `.gcloud-token` の読み取りを拒否し、bypass モードを無効にします | 同上 |
| GCP 側 | 予算アラート、プロジェクトの削除防止（lien） | 最後の砦 |

**限界**: これらはセキュリティ境界ではありません。コンテナにはあなたのログイン情報があるので、フックを通らない経路
（スクリプトが認証情報のファイルを直接読むなど）までは防げません。権限ルールもコマンド文字列の照合で、Claude Code の公式文書も
「境界ではない」と明記しています。`--dangerously-skip-permissions` は使わないでください（設定で無効にしています）。

### 初回の設定

> ログインと設定は、あなたがターミナルで実行します。Claude Code の VS Code 拡張のチャット欄は、コマンドを直接実行する
> `!` の入力（シェルモード）に対応していません（CLI の `claude` では使えます）。

1. **ログインする**（コンテナ内。あなたが入力します。VS Code の統合ターミナル、Cmd+`（Windows/Linux は Ctrl+`）で実行します。
   開発コンテナに接続した VS Code では、このターミナルがコンテナの中で動きます）

   ```bash
   gcloud auth login --no-launch-browser
   gcloud config set project <プロジェクト ID>
   ```

2. **閲覧専用のサービスアカウントを作る**（一度だけ。ホスト、Cloud Shell、または手順 1 の直後のコンテナの統合ターミナルで実行します）

   ```bash
   ./scripts/setup-agent-viewer.sh <プロジェクト ID>      # 内容を表示して確認を取ります。何度実行しても構いません
   ```

   実際の操作は次のコマンドと同じです。

   ```bash
   PROJECT_ID=<プロジェクト ID>
   ME=<あなたのアカウント>
   SA=agent-viewer@${PROJECT_ID}.iam.gserviceaccount.com

   gcloud services enable iamcredentials.googleapis.com --project "$PROJECT_ID"
   gcloud iam service-accounts create agent-viewer --display-name="Agent read-only" --project "$PROJECT_ID"
   for role in roles/cloudbuild.builds.viewer roles/run.viewer roles/logging.viewer; do
     gcloud projects add-iam-policy-binding "$PROJECT_ID" \
       --member="serviceAccount:${SA}" --role="$role" --condition=None
   done
   gcloud iam service-accounts add-iam-policy-binding "$SA" \
     --member="user:${ME}" --role=roles/iam.serviceAccountTokenCreator --project "$PROJECT_ID"
   ```

   `logging.viewer` はプロジェクト内のログをすべて読めます。同じプロジェクトに別用途のログがあるなら、外すか絞ってください。

3. **日常の ID を閲覧専用に固定する**（統合ターミナルで実行します）

   ```bash
   gcloud config set auth/impersonate_service_account agent-viewer@<プロジェクト ID>.iam.gserviceaccount.com
   ```

   以降の gcloud は、このサービスアカウントとして動きます。書き込み系を自分で実行するときは、ホスト側で実行するのが確実です
   （コンテナでは `gcloud config unset auth/impersonate_service_account` で外せますが、戻すのを忘れないでください）。

4. **確認する**

   ```bash
   gcloud auth list                                   # ACTIVE があなたのアカウント
   gcloud config list                                 # [auth] impersonate_service_account = agent-viewer@...
   gcloud builds triggers list --project <プロジェクト ID>
   gcloud builds triggers list --project <プロジェクト ID> --region=asia-northeast1   # リージョンは実際の値に読み替える
   ```

   出力の見方:

   - `WARNING: ... All API calls will be executed as [agent-viewer@...]` が出ていれば、閲覧専用の ID で動いています。
   - `PERMISSION_DENIED: Failed to impersonate` は、作成直後の IAM の反映待ちです（数分で解消します）。
   - `API [...] not enabled on project` は、そのプロジェクトで該当の API が無効、という意味です。なりすまし中の呼び出しは、
     プロジェクトの API の有効化状況に従います。使う API（Cloud Build、Cloud Run、Logging など）を有効にしてから確認してください。
   - 閲覧専用の ID は、付与した 3 つのロールの範囲しか読めません。IAM やサービスの一覧などが拒否されるのは、設計どおりです。

5. **終わったら**、コンテナの統合ターミナルでログアウトします。ログイン情報はボリュームに残しません（再ビルドでも消えます）。
   これはコンテナ内のログインを消す操作で、ホストの gcloud のログインを消す手順ではありません
   （ただし `gcloud auth revoke` はサーバー側でもトークンを失効させます。公式の説明には、同じアカウントで使っている
   他の端末への影響は書かれていません）。代替方式（ホストで短期トークンを発行する）を使うなら、
   ホストのログインが要るので、ホストでは `revoke` しないでください。

   ```bash
   gcloud auth revoke --all
   ```

### GCP 側の保護（最後の砦）

- **予算アラート**: Console の「お支払い → 予算とアラート」で、想定外の課金に気づけるようにします。
- **プロジェクトの削除防止（lien）**: `roles/resourcemanager.lienModifier` を持つアカウントで、次を実行します。
  lien が防ぐのはプロジェクトの削除だけで、中のリソースの削除は防ぎません
  （[公式ドキュメント](https://docs.cloud.google.com/resource-manager/docs/project-liens)）。

  ```bash
  gcloud alpha resource-manager liens create --project=<プロジェクト ID> \
    --restrictions=resourcemanager.projects.delete --reason="agent guard" --origin=agent-guard
  ```

### 代替：ログインを持たせない方式

ホストで短期トークン（約 1 時間）を発行し、`--access-token-file` で使う方式も残しています（`scripts/issue-agent-token.sh`）。
ログインを置かないので、いちばん安全です。ただし、フックは ID を切り替える `--access-token-file` を拒否するので、
この方式を使うなら `.claude/hooks/guard-cloud-cli.py` の `IDENTITY_FLAGS` から外してください。

## 開発環境の GitHub CLI（gh）

開発コンテナには `gh` が入ります（`.devcontainer/install-gh.sh`）。CI の実行結果やログ、ルールセットなど、GitHub の参照に使います。
gcloud と同じ考え方で、エージェントには**読み取り専用**だけを持たせ、書き込み（PR の作成、設定の変更）はあなたが行います。

| 層 | 内容 |
|---|---|
| トークン | **このリポジトリだけ**の細粒度トークン（fine-grained PAT）。`Actions` と `Metadata` の読み取り専用で、期限は 30 日。書き込みの権限は付けません。これが主な制御です |
| PreToolUse フック | `.claude/hooks/guard-cloud-cli.py` が gh の呼び出しも検査します（Bash と Monitor）。認証・トークンの取得、削除、秘密と設定の変更、拡張とエイリアス（コードの実行）、書き込みの GraphQL、URL 指定は拒否し、書き込み系は確認を強制します。`git credential`（保存された認証情報の取り出し）も拒否します。コマンドは、改行・`;`・パイプ・引用符・コマンド置換・ヒアドキュメントを読んで 1 つずつに分け、`bash -c`・`xargs`・`sudo`・`find -exec` などの中身も調べます |
| 権限ルール | `.claude/settings.json`: 読み取り系の gh（`run` / `pr` / `issue` / `workflow` / `ruleset` / `release` / `repo view`）だけ許可し、gh の設定ディレクトリの読み取りを拒否します。**`gh api` は許可リストに入れていません**（毎回確認します。フックの見落としが、そのまま実行にならないようにするためです） |

**gcloud との違い**: gh の設定（トークン）は、再ビルドしても残ります。名前付きボリュームを `~/.gh-config` にマウントし、
`GH_CONFIG_DIR` で gh にそこを使わせています（`~/.config/gh` に直接マウントすると、`~/.config` が root 所有で作られ、
gcloud などが書けなくなるおそれがあるためです）。読み取り専用で期限付きのトークンだけを置く前提なので、残しています。
コンテナにはキーリングが無く、トークンは平文のファイルとして保存されます。フックは境界ではありません（gcloud と同じ限界です）。
コマンド文字列の検査なので、変数やファイルに隠した操作（`$CMD`、書いてから実行するスクリプト）、パスの難読化
（`cd` した後の相対パスなど）、`ssh` や `docker exec` の先は追えません。主な制御は、トークンの権限（読み取り専用）です。

### 初回の設定

1. **トークンを作る**（GitHub のブラウザ操作）。Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token。
   - Repository access: Only select repositories → このリポジトリ
   - Repository permissions: `Actions` を Read-only（`Metadata` は自動で Read-only になります）。
     ルールセットを読みたいときは、`Administration` を Read-only にします（まず付けずに試し、読めなければ足します）
   - Expiration: 30 days
2. **ログインする**（VS Code の統合ターミナル。トークンはチャットに貼りません）

   ```bash
   gh auth login --with-token     # 実行して、トークンを貼り付け、Enter を押してから Ctrl+D
   gh auth status                 # Logged in と表示される
   ```

3. **確認する**

   ```bash
   gh run list --limit 3
   gh ruleset list                                 # Administration を付けた場合（読めなければ、ブラウザで確認）
   ```

期限（30 日）が切れると 401 になります。新しいトークンを作って、手順 2 をやり直してください。
やめるときは、`gh auth logout` でトークンを消せます（再ビルドでは消えません）。

## 構成

```
app/
  main.py            FastAPI アプリ（CORS、例外ハンドラ）
  api/routes.py      /health, /chat、入力検証、LLM エラーの分類、切断時の取り消し
  frontend.py        frontend/dist の配信と SPA フォールバック
  agents/graph.py    LangGraph のグラフ、SYSTEM_PROMPT、モック LLM
  agents/guardrails.py  入出力ガードレール
  agents/resilience.py  LLM 呼び出しのヘッジ付き再試行
  agents/tools.py    設定資料の検索（search_zahard_lore）
frontend/            チャット画面（Vite + React + TypeScript）。dist/ がビルド成果物
storage/chunk/       設定資料（RAG の検索対象）
tests/               pytest（モックモード固定）
```

会話履歴は `thread_id` ごとにプロセス内メモリ（`MemorySaver`）へ保持するため、再起動で消え、複数ワーカー間では共有されません。
