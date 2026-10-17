import { useAuth } from './auth/AuthContext';
import { Composer } from './components/Composer';
import { ErrorBanner } from './components/ErrorBanner';
import { Header } from './components/Header';
import { LoginScreen } from './components/LoginScreen';
import { MessageList } from './components/MessageList';
import { useBackendMode } from './hooks/useBackendMode';
import { useChat } from './hooks/useChat';

function ChatScreen() {
  const chat = useChat();
  const healthMode = useBackendMode();
  const { state, signOut } = useAuth();
  const signedIn = state.status === 'signedIn';

  return (
    <div className="app">
      <Header
        mode={chat.mode ?? healthMode}
        onNewConversation={chat.reset}
        userLabel={state.status === 'signedIn' ? (state.user.email ?? 'ログイン中') : undefined}
        onSignOut={signedIn ? () => void signOut() : undefined}
      />
      <MessageList
        messages={chat.messages}
        waiting={chat.waiting}
        busy={chat.pending}
        resumed={chat.threadId !== null && chat.messages.length === 0}
      />
      {chat.error && (
        <ErrorBanner error={chat.error} disabled={chat.pending} onRetry={chat.retry} />
      )}
      <Composer pending={chat.pending} onSend={chat.send} onStop={chat.stop} />
    </div>
  );
}

export default function App() {
  const { state, signIn } = useAuth();

  if (state.status === 'loading') {
    return (
      <main className="login-screen">
        <p role="status">読み込み中…</p>
      </main>
    );
  }
  if (state.status === 'signedOut') {
    return <LoginScreen notice={state.notice} onSignIn={() => void signIn()} />;
  }
  // 'disabled'（ログインを使わない構成）と 'signedIn'
  return <ChatScreen />;
}
