import { useEffect } from 'react';
import { useAuth } from './auth/AuthContext';
import { Composer } from './components/Composer';
import { DocumentsView } from './components/DocumentsView';
import { ErrorBanner } from './components/ErrorBanner';
import { Header } from './components/Header';
import { LoginScreen } from './components/LoginScreen';
import { MessageList } from './components/MessageList';
import { ThreadSidebar } from './components/ThreadSidebar';
import { useBackendMode } from './hooks/useBackendMode';
import { useChat } from './hooks/useChat';
import { useThreads } from './hooks/useThreads';
import { useUrlState } from './lib/urlState';

function ChatScreen() {
  const chat = useChat();
  const healthMode = useBackendMode();
  const { state, signOut } = useAuth();
  const signedIn = state.status === 'signedIn';
  // 開いている画面は URL が決める（戻る・再読み込み・共有で戻れる）
  const { tab: view, navigate } = useUrlState();
  // 会話の一覧は、ログイン中（バックエンドが本人の会話を区別できるとき）だけ出す
  const threads = useThreads(signedIn);
  const { reload: reloadThreads } = threads;
  // 回答が最後まで届いた（または止まった）あと、新しい会話・更新された順を一覧に反映する。
  // 生成中は null。終わると発言の数に変わる（送るたびに数が変わるので、取り直しが走る）
  const settledCount = chat.pending ? null : chat.messages.length;
  useEffect(() => {
    if (settledCount) reloadThreads();
  }, [settledCount, reloadThreads]);

  const removeThread = async (threadId: string) => {
    const failure = await threads.remove(threadId);
    // 開いていた会話を消したら、画面も最初の状態に戻す
    if (failure === null && threadId === chat.threadId) chat.reset();
    return failure;
  };

  return (
    <div className={signedIn ? 'app app-with-sidebar' : 'app'}>
      <Header
        mode={chat.mode ?? healthMode}
        view={view}
        onViewChange={(tab) => navigate({ tab })}
        onNewConversation={chat.reset}
        userLabel={state.status === 'signedIn' ? (state.user.email ?? 'ログイン中') : undefined}
        onSignOut={signedIn ? () => void signOut() : undefined}
      />
      {/* 資料一覧を開いている間も、チャットは隠すだけで残す（生成中の回答・入力途中の文・スクロール位置を失わない） */}
      <div className="chat-layout" hidden={view !== 'chat'}>
        {signedIn && (
          <ThreadSidebar
            state={threads.state}
            currentId={chat.threadId}
            busy={chat.pending}
            onSelect={chat.resume}
            onDelete={removeThread}
            onRetry={threads.reload}
          />
        )}
        <div className="chat-pane">
          <MessageList
            messages={chat.messages}
            waiting={chat.waiting}
            phase={chat.phase}
            busy={chat.pending}
            resumed={chat.threadId !== null && chat.messages.length === 0}
            history={chat.history}
            onExample={chat.pending ? undefined : chat.send}
          />
          {chat.error && (
            <ErrorBanner error={chat.error} disabled={chat.pending} onRetry={chat.retry} />
          )}
          <Composer pending={chat.pending} onSend={chat.send} onStop={chat.stop} />
        </div>
      </div>
      {view === 'documents' && <DocumentsView />}
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
