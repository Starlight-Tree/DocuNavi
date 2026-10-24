import { createClient } from '@supabase/supabase-js';
import type { Session } from '@supabase/supabase-js';
import type { AuthClient, AuthUser } from './types';

function toUser(session: Session | null): AuthUser | null {
  return session ? { email: session.user.email ?? null } : null;
}

/**
 * Supabase Auth（Google ログイン）の AuthClient。
 *
 * - セッションはブラウザの localStorage に保存され、アクセストークンの期限が近づくと SDK が自動で更新する
 *   （getSession も、期限切れならリフレッシュトークンで更新してから返す）
 * - 戻り先の `?code=` の交換（PKCE）は、クライアントを作ったときに SDK が行う
 * publishableKey は公開してよい鍵（匿名用）。サービスロールの鍵は絶対に入れない。
 */
export function createSupabaseAuth(url: string, publishableKey: string): AuthClient {
  const client = createClient(url, publishableKey, {
    auth: { flowType: 'pkce', autoRefreshToken: true, persistSession: true, detectSessionInUrl: true },
  });

  return {
    getUser: async () => toUser((await client.auth.getSession()).data.session),
    getAccessToken: async () => (await client.auth.getSession()).data.session?.access_token ?? null,
    refreshAccessToken: async () => (await client.auth.refreshSession()).data.session?.access_token ?? null,
    onChange: (listener) => {
      // コールバックの中で SDK の別のメソッドを await すると固まるので、通知を渡すだけにする
      const { data } = client.auth.onAuthStateChange((_event, session) => listener(toUser(session)));
      return () => data.subscription.unsubscribe();
    },
    signInWithGoogle: async () => {
      const { error } = await client.auth.signInWithOAuth({
        provider: 'google',
        options: { redirectTo: window.location.origin },
      });
      if (error) throw error;
    },
    signOut: async () => {
      // 他の端末のセッションは残す（scope: 'local'）
      await client.auth.signOut({ scope: 'local' });
    },
  };
}

/**
 * NEXT_PUBLIC_SUPABASE_URL と NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY がそろっていれば AuthClient を作る。
 * どちらかが無ければ null: ログイン画面を出さず、従来どおり認証なしで動く（バックエンドが AUTH_MODE=required だと 401 になる）。
 */
export function createAuthClientFromEnv(): AuthClient | null {
  const url = (process.env.NEXT_PUBLIC_SUPABASE_URL ?? '').trim();
  const key = (process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ?? '').trim();
  return url && key ? createSupabaseAuth(url, key) : null;
}
