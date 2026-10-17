import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { AuthProvider } from './auth/AuthContext';
import { createAuthClientFromEnv } from './auth/supabase';
import './styles.css';

// ログインの設定（VITE_SUPABASE_*）が無ければ null: ログイン画面を出さず、認証なしで動く
const authClient = createAuthClientFromEnv();

const container = document.getElementById('root');
if (!container) throw new Error('#root が見つかりません');

createRoot(container).render(
  <StrictMode>
    <AuthProvider client={authClient}>
      <App />
    </AuthProvider>
  </StrictMode>,
);
