/**
 * ログインの窓口。画面とチャットの通信は、この型にだけ依存する（Better Auth の SDK は betterAuth.ts に閉じ込める）。
 * テストでは、これを満たす偽物を AuthProvider に渡す。
 */

export interface AuthUser {
  /** 画面に出す名前（メールアドレス）。取れなければ null。 */
  email: string | null;
  /** Google の表示名（初回ログイン時の値）。無ければ null か省略。 */
  name?: string | null | undefined;
  /** プロフィール画像の URL（Google のアイコン）。無ければ null か省略。 */
  image?: string | null | undefined;
}

/**
 * トークンを取りに行ったが、通信の失敗・サーバーの一時的な不調で取れなかった（ログアウトしたとは限らない）。
 * 「ログインしていない」（確かに未ログイン・セッション無効）を表す null とは区別する。投げられたら、ログイン画面へ戻さず、再試行を促す。
 */
export class TokenUnavailableError extends Error {
  constructor() {
    super('token unavailable');
    this.name = 'TokenUnavailableError';
  }
}

export interface AuthClient {
  /** いまのログイン中の利用者。ログインしていなければ null。 */
  getUser: () => Promise<AuthUser | null>;
  /** 有効なアクセストークン。期限が近ければ、ここで更新してから返す。ログインしていなければ null。一時的な失敗は TokenUnavailableError を投げる。 */
  getAccessToken: () => Promise<string | null>;
  /** 新しいアクセストークンを取り直して返す。セッションが無効なら null。一時的な失敗は TokenUnavailableError を投げる。 */
  refreshAccessToken: () => Promise<string | null>;
  /** ログイン状態の変化（ログイン・ログアウト・別タブでの変更・更新の失敗）を受け取る。戻り値で購読をやめる。 */
  onChange: (listener: (user: AuthUser | null) => void) => () => void;
  /** Google のログイン画面へ移る（戻ってきたときに、セッションの Cookie ができる）。 */
  signInWithGoogle: () => Promise<void>;
  /** 表示名を変更する。失敗したら例外を投げる。 */
  updateName: (name: string) => Promise<void>;
  /** この端末のセッションを破棄する。 */
  signOut: () => Promise<void>;
}
