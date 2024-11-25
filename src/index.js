const express = require('express');
const { BotFrameworkAdapter, MemoryStorage, ConversationState } = require('botbuilder');
const app = express();
const port = 3978;

const adapter = new BotFrameworkAdapter({
    appId: 'your-app-id',
    appPassword: 'your-app-password'
});

app.use(express.json());

// メッセージを受け取って処理するエンドポイント
app.post('/api/messages', (req, res) => {
    adapter.processActivity(req, res, async (turnContext) => {
        await turnContext.sendActivity('こんにちは！');
    });
});

app.listen(port, () => {
    console.log(`Bot is listening at http://localhost:${port}`);
});
