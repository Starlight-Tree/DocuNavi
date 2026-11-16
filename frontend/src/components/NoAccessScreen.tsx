export const MSG_NO_ACCESS = 'このアカウントには利用許可がありません。';

interface NoAccessScreenProps {
  /** ログイン中のアカウントのメール。どのアカウントで入ったかを示す。 */
  email: string | null | undefined;
  onSignOut: () => void;
}

/** ログインはできたが、利用資格がないアカウントの画面。申請や承認の仕組みは無いので、別のアカウントでログインし直す導線だけを出す。 */
export function NoAccessScreen({ email, onSignOut }: NoAccessScreenProps) {
  return (
    <main className="login-screen">
      <h1 className="app-title">DocuNavi</h1>
      <p className="error-banner login-notice" role="alert">
        {MSG_NO_ACCESS}
      </p>
      {email && <p className="login-account">ログイン中のアカウント: {email}</p>}
      <p>別のアカウントを使うには、ログアウトして、もう一度ログインしてください。</p>
      <button type="button" className="btn btn-primary" onClick={onSignOut}>
        ログアウト
      </button>
    </main>
  );
}
