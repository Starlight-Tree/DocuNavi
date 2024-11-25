import * as msal from '@azure/msal-browser'; // MSAL.jsライブラリをインポート

const msalConfig = {
    auth: {
        clientId: process.env.REACT_APP_AZURE_APP_CLIENT_ID,  // .envからAzureアプリケーションIDを読み込む
        authority: `https://login.microsoftonline.com/${process.env.REACT_APP_AZURE_APP_TENANT_ID}`,  // .envからテナントIDを読み込む
        redirectUri: process.env.REACT_APP_AZURE_APP_REDIRECT_URI,  // .envからリダイレクトURIを読み込む
    },
    system: {
        // PKCEを強制的に有効にする設定
        allowNativeBroker: false, // モバイル環境でのPKCEが必要ない場合はfalseにします
        usePKCE: true // PKCEを強制的に有効にします
    }
};

const msalInstance = new msal.PublicClientApplication(msalConfig);

// リダイレクト後の処理（リダイレクトから戻った後に呼び出される）
msalInstance.handleRedirectPromise().then((response) => {
    if (response) {
        console.log('サインイン成功:', response);
    } else {
        console.log('リダイレクト後に何も返ってこなかった');
    }
}).catch(error => {
    console.error('リダイレクト後の処理でエラー:', error);
});

// サインイン処理（リダイレクト型）
export function signIn() {
    const loginRequest = {
        scopes: ['user.read'] // 必要なスコープ
    };

    msalInstance.loginRedirect(loginRequest); // リダイレクト型でサインイン
}

// サインアウト処理
export function signOut() {
    msalInstance.logout(); // ログアウト処理
}

// アクセストークンの取得（リダイレクト後）
export function getAccessToken() {
    const tokenRequest = {
        scopes: ['user.read'] // 必要なスコープ
    };

    return msalInstance.acquireTokenSilent(tokenRequest)
        .then(response => {
            return response.accessToken;
        })
        .catch(error => {
            console.error('トークン取得失敗:', error);
            if (error instanceof msal.InteractionRequiredAuthError) {
                return signIn(); // リダイレクトで再認証
            }
            throw error;
        });
}

export { msalInstance };
