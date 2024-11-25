require('dotenv').config();  // .env ファイルから環境変数を読み込む

const restify = require('restify');
const { BotFrameworkAdapter, ActivityHandler } = require('botbuilder');
const fs = require('fs');
const path = require('path');
const axios = require('axios');

// 環境変数の読み込み
const openaiApiKey = process.env.OPENAI_API_KEY;
const openaiApiUrl = process.env.OPENAI_API_URL;  // .env から取得
const directLineSecret = process.env.DIRECT_LINE_SECRET;  // Direct Line シークレット

// サーバー作成
const server = restify.createServer();
server.listen(3978, () => {
    console.log('ボットサーバーが起動しました。ポート 3978 で待機中...');
});

// BotFrameworkAdapterを作成
const adapter = new BotFrameworkAdapter({
    appId: process.env.REACT_APP_AZURE_APP_CLIENT_ID,
    appPassword: process.env.REACT_APP_AZURE_APP_PASSWOR
});

// ボットの作成
const bot = new ActivityHandler();

// メッセージ処理
bot.onMessage(async (context, next) => {
    const userMessage = context.activity.text.trim();  // ユーザーからの入力を取得

    try {
        // GPTに基づいてキーワードや関連ファイルを検索し、関連する情報を取得
        const chunkResponse = await getChunkResponseFromUserMessage(userMessage);

        if (chunkResponse) {
            // チャンクテキストをGPTで処理して回答を生成
            const finalAnswer = await getGPT4ResponseWithChunk(userMessage, chunkResponse);
            await context.sendActivity(finalAnswer);
        } else {
            // チャンク情報が見つからなかった場合、通常のGPT-4で回答を生成
            const openaiResponse = await getGPT4Response(userMessage);
            await context.sendActivity(openaiResponse);
        }
    } catch (error) {
        console.error("Error in request:", error);
        await context.sendActivity("エラーが発生しました。もう一度試してください。");
    }

    await next();
});

// ユーザーのメッセージを解析して関連するチャンクテキストを取得する関数
async function getChunkResponseFromUserMessage(userMessage) {
    const keyword = userMessage.match(/ザーハード/);  // キーワードを抽出（例：ザーハード）
    if (keyword) {
        const chunkFile = `ザーハード1.txt`;  // 例えばザーハードに関連するファイル名を選定
        return await getTextFromFile(chunkFile);
    } else {
        return null;  // キーワードが見つからなかった場合
    }
}

// GPT-4を使用して、ユーザーのメッセージとチャンクファイルの情報を組み合わせて回答を生成する関数
async function getGPT4ResponseWithChunk(message, chunkResponse) {
    const prompt = `以下の情報を基に質問に対する回答を作成してください：\n\n${chunkResponse}\n\n質問: ${message}`;

    try {
        const response = await axios.post(openaiApiUrl, {
            model: 'gpt-4o-mini',
            messages: [{ role: 'user', content: prompt }],
            max_tokens: 500, // 応答の長さを調整
        }, {
            headers: {
                'Authorization': `Bearer ${openaiApiKey}`,
                'Content-Type': 'application/json',
            }
        });

        const reply = response.data.choices[0].message.content.trim();
        return reply;
    } catch (error) {
        console.error("Error generating GPT-4 response:", error);
        throw error;
    }
}

// チャンクファイルからテキストを取得する関数
async function getTextFromFile(fileName) {
    const directoryPath = path.join(__dirname, 'storage', 'chunk');  // チャンクファイルを格納したディレクトリ
    const filePath = path.join(directoryPath, fileName);

    try {
        if (fs.existsSync(filePath)) {
            return fs.readFileSync(filePath, 'utf-8');  // ファイル内容を返す
        } else {
            return null;  // ファイルが見つからない場合
        }
    } catch (error) {
        console.error("Error reading file:", error);
        return null;
    }
}

// GPT-4 API からの応答を取得する関数
async function getGPT4Response(message) {
    try {
        const response = await axios.post(openaiApiUrl, {
            model: 'gpt-4o-mini',
            messages: [{ role: 'user', content: message }],
            max_tokens: 150, // 応答の長さ
        }, {
            headers: {
                'Authorization': `Bearer ${openaiApiKey}`,
                'Content-Type': 'application/json',
            }
        });

        const reply = response.data.choices[0].message.content.trim();
        return reply;
    } catch (error) {
        console.error("Error fetching GPT-4 response:", error);
        throw error;
    }
}

// サーバーのエンドポイントを設定
server.post('/api/messages', async (req, res) => {
    await adapter.processActivity(req, res, async (turnContext) => {
        await bot.run(turnContext);
    });
});

// ボットのエラーハンドリング
adapter.onTurnError = async (context, error) => {
    console.error(`Error occurred: ${error}`);
    await context.sendActivity('エラーが発生しました。');
};
