import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useAuth } from './auth/AuthContext';
import { ChatContextBar } from './components/ChatContextBar';
import { Composer } from './components/Composer';
import { DocumentsView } from './components/DocumentsView';
import { ErrorBanner } from './components/ErrorBanner';
import { Header } from './components/Header';
import { LoginScreen } from './components/LoginScreen';
import { MessageList } from './components/MessageList';
import { ReferencesView } from './components/ReferencesView';
import { ScopeChips } from './components/ScopeChips';
import { ScopeConfirm } from './components/ScopeConfirm';
import { ThreadSidebar } from './components/ThreadSidebar';
import { useBackendMode } from './hooks/useBackendMode';
import { useChat } from './hooks/useChat';
import {
  CollectionCatalogContext,
  useCollectionCatalog,
  useCollectionCatalogLoader,
  usableCollections,
} from './hooks/useCollections';
import { useThreads } from './hooks/useThreads';
import { collectionLabel } from './lib/collections';
import type { SourceDoc } from './lib/sources';
import { useUrlState } from './lib/urlState';

const ALL_LABEL = 'すべての資料';
const MSG_SCOPE_MISMATCH =
  '検索範囲が、サーバーに反映されなかった可能性があります。この回答は、すべての資料から探したものかもしれません。';

/** 範囲を変える確認の待ち。切り替え先（null は「すべて」）と、確認を閉じたあとにフォーカスを戻す要素。 */
interface PendingScope {
  collection: string | null;
  trigger: HTMLElement | null;
}

/** 資料グループの一覧（GET /collections）を、画面全体で 1 回だけ取って配る。 */
function CollectionsProvider({ children }: { children: ReactNode }) {
  const catalog = useCollectionCatalogLoader();
  return <CollectionCatalogContext.Provider value={catalog}>{children}</CollectionCatalogContext.Provider>;
}

function ChatScreen() {
  const chat = useChat();
  const catalog = useCollectionCatalog();
  const groups = usableCollections(catalog.state);
  const healthMode = useBackendMode();
  const { state, signOut } = useAuth();
  const signedIn = state.status === 'signedIn';
  // 開いている画面は URL が決める（戻る・再読み込み・共有で戻れる）
  const { tab: view, navigate } = useUrlState();
  const [pendingScope, setPendingScope] = useState<PendingScope | null>(null);
  // 参照資料の専用ページに出す、ひとつの回答の出典。null ならチャット。URL には載せない（会話の中身は再読み込みで残らないため）
  const [references, setReferences] = useState<SourceDoc[] | null>(null);
  // 範囲を切り替えたあと、入力欄へフォーカスを移す依頼（チャットの画面が見えてから移す）
  const [focusRequest, setFocusRequest] = useState(0);
  const focusedRequest = useRef(0);
  useEffect(() => {
    if (view !== 'chat' || focusRequest === focusedRequest.current) return;
    focusedRequest.current = focusRequest;
    document.getElementById('message-input')?.focus();
  }, [focusRequest, view]);
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

  // 検索範囲を変える。会話が無ければそのまま切り替え、会話があれば（会話ごとに固定なので）確認して新しい会話にする。
  // 資料一覧の「このグループに質問」と、入力欄の上のチップは、どちらもここを通る。資料一覧の絞り込みとは別のもの
  const requestScope = (collection: string | null, trigger: HTMLElement | null, fromDocuments: boolean) => {
    const goChat = () => {
      if (fromDocuments) navigate({ tab: 'chat' });
      setFocusRequest((n) => n + 1);
    };
    if (!chat.scopeUnknown && collection === chat.scope) {
      if (fromDocuments) goChat();
      return;
    }
    if (chat.setScope(collection)) {
      goChat();
      return;
    }
    setPendingScope({ collection, trigger });
  };

  const cancelScope = useCallback(() => {
    const trigger = pendingScope?.trigger ?? null;
    setPendingScope(null);
    // 閉じたら、開く前の位置へ戻す。その要素が無くなっていれば、入力欄へ
    if (trigger?.isConnected) trigger.focus();
    else setFocusRequest((n) => n + 1);
  }, [pendingScope]);

  const confirmScope = () => {
    if (!pendingScope) return;
    chat.startNew(pendingScope.collection);
    setPendingScope(null);
    if (view !== 'chat') navigate({ tab: 'chat' });
    setFocusRequest((n) => n + 1);
  };

  const scopeLabel = (name: string | null) => (name === null ? ALL_LABEL : collectionLabel(name, groups ?? undefined));
  const currentGroup = groups?.find((g) => g.name === chat.scope);
  // 文脈バーに出す対象の資料数。範囲が分からない間・一覧を読めなかったときは出さない
  const contextCount = !groups || chat.scopeUnknown
    ? undefined
    : chat.scope === null
      ? groups.reduce((sum, g) => sum + g.documentCount, 0)
      : currentGroup?.documentCount;

  return (
    <div className={signedIn ? 'app app-with-sidebar' : 'app'}>
      <Header
        mode={chat.mode ?? healthMode}
        view={view}
        onViewChange={(tab) => {
          setReferences(null);
          navigate({ tab });
        }}
        onNewConversation={() => {
          setReferences(null);
          chat.reset();
        }}
        userLabel={state.status === 'signedIn' ? (state.user.email ?? 'ログイン中') : undefined}
        onSignOut={signedIn ? () => void signOut() : undefined}
      />
      {pendingScope && (
        <ScopeConfirm
          targetLabel={scopeLabel(pendingScope.collection)}
          generating={chat.pending}
          onConfirm={confirmScope}
          onCancel={cancelScope}
        />
      )}
      {/* 資料一覧を開いている間も、チャットは隠すだけで残す（生成中の回答・入力途中の文・スクロール位置を失わない） */}
      <div className="chat-layout" hidden={view !== 'chat' || references !== null}>
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
          <ChatContextBar
            targetLabel={chat.scopeUnknown ? 'この会話の設定' : scopeLabel(chat.scope)}
            documentCount={contextCount}
            onBack={() => {
              setReferences(null);
              navigate({ tab: 'documents' });
            }}
          />
          <MessageList
            messages={chat.messages}
            waiting={chat.waiting}
            phase={chat.phase}
            busy={chat.pending}
            resumed={chat.threadId !== null && chat.messages.length === 0}
            history={chat.history}
            onExample={chat.pending ? undefined : chat.send}
            onAskAll={groups ? chat.askAgainInAll : undefined}
            onShowReferences={setReferences}
          />
          {chat.error && (
            <ErrorBanner
              error={chat.error}
              disabled={chat.pending}
              onRetry={chat.retry}
              onNewConversation={groups ? chat.reset : undefined}
            />
          )}
          {chat.scopeMismatch && (
            <p className="scope-warning" role="status">
              {MSG_SCOPE_MISMATCH}
            </p>
          )}
          {(groups || catalog.state.status === 'error') && (
            <ScopeChips
              collections={groups ?? []}
              status={groups ? 'ready' : 'unavailable'}
              value={chat.scope}
              unknown={chat.scopeUnknown}
              disabled={chat.pending}
              onSelect={(collection, trigger) => requestScope(collection, trigger, false)}
              onRetry={catalog.reload}
            />
          )}
          <Composer
            pending={chat.pending}
            onSend={chat.send}
            onStop={chat.stop}
            scopeLabel={groups ? (chat.scopeUnknown ? 'この会話の設定' : scopeLabel(chat.scope)) : undefined}
            scopeNote={
              groups
                ? currentGroup && currentGroup.readyCount === 0
                  ? '資料はまだありません'
                  : chat.hasConversation
                    ? 'この会話で固定'
                    : undefined
                : undefined
            }
          />
        </div>
      </div>
      {view === 'chat' && references !== null && (
        <ReferencesView
          sources={references}
          onBack={() => {
            setReferences(null);
            setFocusRequest((n) => n + 1);
          }}
        />
      )}
      {view === 'documents' && (
        <DocumentsView
          onAskGroup={groups ? (collection, trigger) => requestScope(collection, trigger, true) : undefined}
        />
      )}
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
  return (
    <CollectionsProvider>
      <ChatScreen />
    </CollectionsProvider>
  );
}
