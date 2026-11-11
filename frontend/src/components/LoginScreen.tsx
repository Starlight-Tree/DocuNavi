interface LoginScreenProps {
  /** 期限切れなど、ログイン画面に戻された理由。 */
  notice?: string | undefined;
  onSignIn: () => void;
}

export function LoginScreen({ notice, onSignIn }: LoginScreenProps) {
  return (
    <main className="login-screen">
      <h1 className="app-title">DocuNavi</h1>
      <p>利用するには、Google アカウントでログインしてください。</p>
      {notice && (
        <p className="error-banner login-notice" role="alert">
          {notice}
        </p>
      )}
      <button type="button" className="btn btn-primary" onClick={onSignIn}>
        Google でログイン
      </button>
    </main>
  );
}
