# Node.js チャットボット (Bot Framework + OpenAI GPT-4o-mini)

このプロジェクトは、Microsoft Bot Framework と OpenAI の **GPT-4o-mini API** を統合したチャットボットです。ユーザーからのメッセージを受け取り、OpenAI GPT-4o-mini を利用して返答を生成し、Bot Framework を通じて返答を返します。

## 使用技術

- **Node.js**: サーバーサイドの JavaScript ランタイム
- **Bot Framework SDK**: Microsoft の Bot Framework SDK を使用してボットを作成
- **OpenAI GPT-4o-mini API**: ユーザーの入力に基づいて自然言語で返答を生成
- **Restify**: 軽量な Node.js のウェブサーバーフレームワーク
- **Axios**: HTTP リクエストを送信するためのライブラリ

## コードの概要

### `app.js`

- **Restify サーバー**: Node.js 用の軽量な REST サーバーを作成。
- **BotFrameworkAdapter**: Microsoft の Bot Framework を使用してボットのリクエストを処理。
- **OpenAI GPT-4o-mini API**: ユーザーからのメッセージを受け取って、GPT-4o-mini API にリクエストを送信し、生成された返答を返します。

ngrok http 3978

ode app.js 