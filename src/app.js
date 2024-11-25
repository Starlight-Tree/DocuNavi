import { signIn, signOut, getAccessToken } from './auth'; // auth.jsからインポート

// サインインボタンのクリックイベント
document.getElementById('sign-in-btn').addEventListener('click', () => {
    signIn()
        .then(accessToken => {
            console.log('ログイン成功', accessToken);
            // WebChatやその他の処理を続ける
            initializeWebChat(accessToken);
        })
        .catch(error => {
            console.error('ログイン失敗', error);
        });
});

// サインアウトボタンのクリックイベント
document.getElementById('sign-out-btn').addEventListener('click', () => {
    signOut();
});

// WebChatを初期化する関数
function initializeWebChat(accessToken) {
    fetch('/secret')
        .then(response => response.json())
        .then(data => {
            const directLineSecret = data.directLineSecret;
            const directLine = window.WebChat.createDirectLine({
                secret: directLineSecret,
                userID: 'user1',
                username: 'ユーザー名',
                locale: 'ja-JP'
            });

            window.WebChat.renderWebChat({
                directLine: directLine,
            }, document.getElementById('webchat'));
        })
        .catch(error => {
            console.error("Failed to fetch the Direct Line secret:", error);
        });
}
