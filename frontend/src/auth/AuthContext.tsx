import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { saveReturnTo, saveThreadId, takeReturnTo } from '../lib/storage';
import { currentStateSearch, replaceUrlState } from '../lib/urlState';
import type { AuthClient, AuthUser } from './types';

export const MSG_SESSION_EXPIRED = 'ログインの有効期限が切れました。もう一度ログインしてください。';
const MSG_SIGN_IN_FAILED = 'ログインを始められませんでした。しばらく待ってから、もう一度お試しください。';

export type AuthState =
  | { status: 'disabled' } // ログインを使わない構成（認証なしで動く）
  | { status: 'loading' }
  | { status: 'signedOut'; notice?: string }
  | { status: 'signedIn'; user: AuthUser }
  | { status: 'noAccess'; user: AuthUser }; // ログインはできたが、利用資格（許可）がない。ログアウトだけできる

export interface AuthContextValue {
  state: AuthState;
  client: AuthClient | null;
  signIn: () => Promise<void>;
  /** 自分でログアウトする。会話の ID も捨てる（共有の端末に残さない）。 */
  signOut: () => Promise<void>;
  /** 表示名を変更する。成功したら画面の名前もすぐ変える。失敗したら例外を投げる。 */
  updateName: (name: string) => Promise<void>;
  /** トークンを更新できなかった・更新しても 401 だった。ログイン画面へ戻す（会話の ID は残す）。 */
  expire: () => void;
  /** API が「利用許可がありません」（識別コード付きの 403）を返した。許可がない画面へ切り替える（ログアウトは残す）。 */
  denyAccess: () => void;
}

const DISABLED: AuthContextValue = {
  state: { status: 'disabled' },
  client: null,
  signIn: async () => undefined,
  signOut: async () => undefined,
  updateName: async () => undefined,
  expire: () => undefined,
  denyAccess: () => undefined,
};

const AuthContext = createContext<AuthContextValue>(DISABLED);

/**
 * ログイン前に預けた画面の状態を、ログイン後の URL に戻す。
 * いまの URL に画面の指定（tab）がすでにあるときは、そちらを優先して、預けた分は捨てる。
 */
function restoreReturnTo(): void {
  const saved = takeReturnTo();
  if (!saved || new URLSearchParams(window.location.search).has('tab')) return;
  replaceUrlState(saved);
}

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}

interface AuthProviderProps {
  /** null ならログインを使わない（Provider が無いのと同じ）。 */
  client: AuthClient | null;
  children: ReactNode;
}

export function AuthProvider({ client, children }: AuthProviderProps) {
  const [state, setState] = useState<AuthState>(client ? { status: 'loading' } : { status: 'disabled' });

  useEffect(() => {
    if (!client) {
      setState({ status: 'disabled' });
      return;
    }
    let active = true;
    const apply = (user: AuthUser | null) => {
      if (!active) return;
      if (user) restoreReturnTo();
      // 期限切れの通知（expire）で出した案内は、ログインし直すまで残す。許可がない画面は、同じセッションの再取得では戻さない
      setState((current) =>
        user
          ? current.status === 'noAccess'
            ? { status: 'noAccess', user }
            : { status: 'signedIn', user }
          : current.status === 'signedOut'
            ? current
            : { status: 'signedOut' },
      );
    };
    const unsubscribe = client.onChange(apply);
    // 戻り先の ?code= の交換が終わるまで待ってから、最初の状態を決める
    client
      .getUser()
      .then(apply)
      .catch(() => apply(null));
    return () => {
      active = false;
      unsubscribe();
    };
  }, [client]);

  const signIn = useCallback(async () => {
    if (!client) return;
    // Google から戻る先は origin だけでクエリが落ちるので、いまの画面の状態を預けておく
    saveReturnTo(currentStateSearch());
    try {
      await client.signInWithGoogle();
    } catch {
      setState({ status: 'signedOut', notice: MSG_SIGN_IN_FAILED });
    }
  }, [client]);

  const signOut = useCallback(async () => {
    saveThreadId(null);
    setState({ status: 'signedOut' });
    try {
      await client?.signOut();
    } catch {
      // 端末のセッションを消せなくても、画面はログアウトの状態にしてある
    }
  }, [client]);

  const updateName = useCallback(
    async (name: string) => {
      if (!client) return;
      await client.updateName(name);
      // 再取得の通知（少し遅れる）を待たずに、画面の名前を変える
      setState((current) => (current.status === 'signedIn' ? { status: 'signedIn', user: { ...current.user, name } } : current));
    },
    [client],
  );

  const expire = useCallback(() => {
    setState((current) =>
      current.status === 'signedIn' ? { status: 'signedOut', notice: MSG_SESSION_EXPIRED } : current,
    );
    void client?.signOut().catch(() => undefined);
  }, [client]);

  const denyAccess = useCallback(() => {
    setState((current) => (current.status === 'signedIn' ? { status: 'noAccess', user: current.user } : current));
  }, []);

  const value = useMemo(
    () => ({ state, client, signIn, signOut, updateName, expire, denyAccess }),
    [state, client, signIn, signOut, updateName, expire, denyAccess],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
