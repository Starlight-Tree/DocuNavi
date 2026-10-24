'use client';

import { StrictMode } from 'react';
import App from './App';
import { AuthProvider } from './auth/AuthContext';
import { createAuthClientFromEnv } from './auth/supabase';

// ログインの設定（NEXT_PUBLIC_SUPABASE_*）が無ければ null: ログイン画面を出さず、認証なしで動く。
// このファイルはブラウザーでだけ読み込まれる（app/client-app.tsx が ssr: false で読む）
const authClient = createAuthClientFromEnv();

export default function Root() {
  return (
    <StrictMode>
      <AuthProvider client={authClient}>
        <App />
      </AuthProvider>
    </StrictMode>
  );
}
