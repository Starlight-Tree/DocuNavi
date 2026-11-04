import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useAuth } from './auth/AuthContext';
import { AppSidebar, SidebarIcon } from './components/AppSidebar';
import { Composer } from './components/Composer';
import { DocumentsView, MSG_NOTHING_SELECTED } from './components/DocumentsView';
import { ErrorBanner } from './components/ErrorBanner';
import { LoginScreen } from './components/LoginScreen';
import { MessageList } from './components/MessageList';
import { PanelHost } from './components/PanelHost';
import { ScopeConfirm } from './components/ScopeConfirm';
import { ScopePicker } from './components/ScopePicker';
import type { TargetMenuItem } from './components/ScopePicker';
import { ThreadSidebar } from './components/ThreadSidebar';
import { useBackendMode } from './hooks/useBackendMode';
import { ALL_SCOPE, sameScope, useChat } from './hooks/useChat';
import type { ChatScope } from './hooks/useChat';
import {
  ClassificationCatalogContext,
  classificationItems,
  useClassificationCatalog,
  useClassificationLoader,
  usableClassifications,
} from './hooks/useClassifications';
import { useDocumentList } from './hooks/useDocuments';
import {
  CollectionCatalogContext,
  useCollectionCatalog,
  useCollectionCatalogLoader,
  usableCollections,
} from './hooks/useCollections';
import { useThreads } from './hooks/useThreads';
import { classificationLabel, isPreparing } from './lib/classifications';
import { collectionLabel } from './lib/collections';
import { effectiveSelection, normalizeSelection } from './lib/documentSelection';
import { useUrlState } from './lib/urlState';

const ALL_LABEL = 'すべての資料';
const MSG_SCOPE_MISMATCH =
  '回答対象が、サーバーに反映されなかった可能性があります。この回答は、すべての資料から探したものかもしれません。';
const MSG_SCOPE_UNAVAILABLE =
  'この会話の対象は、いま利用できません（資料が別の対象へ移った、または閲覧できなくなった可能性があります）。続きを送っても、保存された対象のまま探します。新しい会話で、対象を選び直せます。';
const MSG_TARGET_EMPTY = 'この対象には、いま検索できる資料がありません（取り込みが終わると使えます）。';
const UNKNOWN_LABEL = 'この会話の対象';
const SELECTED_LABEL = '選択した資料';

/** 回答対象を変える確認の待ち。切り替え先と、確認を閉じたあとにフォーカスを戻す要素。 */
interface PendingScope {
  scope: ChatScope;
  trigger: HTMLElement | null;
}

/** 資料グループの一覧（GET /collections）を、画面全体で 1 回だけ取って配る。 */
function CollectionsProvider({ children }: { children: ReactNode }) {
  const catalog = useCollectionCatalogLoader();
  return <CollectionCatalogContext.Provider value={catalog}>{children}</CollectionCatalogContext.Provider>;
}

/** 対象と資料種別の一覧（GET /targets・GET /doc-types）を、画面全体で 1 回ずつだけ取って配る。 */
function ClassificationProvider({ children }: { children: ReactNode }) {
  const catalog = useClassificationLoader();
  return <ClassificationCatalogContext.Provider value={catalog}>{children}</ClassificationCatalogContext.Provider>;
}

function ChatScreen() {
  const chat = useChat();
  const catalog = useCollectionCatalog();
  const groups = usableCollections(catalog.state);
  const classifications = useClassificationCatalog();
  const targets = usableClassifications(classifications.targets);
  const targetItems = classificationItems(classifications.targets);
  const healthMode = useBackendMode();
  const { state, signOut } = useAuth();
  const signedIn = state.status === 'signedIn';
  // 開いている画面は URL が決める（戻る・再読み込み・共有で戻れる）
  const { tab: view, doc, navigate } = useUrlState();
  // 資料の一覧は、資料一覧のタブと右のパネル（どちらも資料を出す）が共有する。どちらも出ていない間は取らない
  const panelOpen = doc !== null;
  const [documentList, reloadDocuments] = useDocumentList(view === 'documents' || panelOpen);
  // 右のパネルの拡大（CSS だけ。パネルもチャットも作り直さない）。閉じたら戻す
  const [expanded, setExpanded] = useState(false);
  // 左サイドバーの開閉。閉じても隠すだけで、会話・入力途中の文・回答対象・スクロール位置は変えない
  const [sideOpen, setSideOpen] = useState(true);
  const collapseRef = useRef<HTMLButtonElement>(null);
  const reopenRef = useRef<HTMLButtonElement>(null);
  const sideToggled = useRef(false);
  // 開閉したら、フォーカスを、いま押せる側のボタンへ移す（隠した側にフォーカスを残さない）
  useEffect(() => {
    if (!sideToggled.current) return;
    (sideOpen ? collapseRef.current : reopenRef.current)?.focus();
  }, [sideOpen]);
  const toggleSide = (open: boolean) => {
    sideToggled.current = true;
    setSideOpen(open);
  };
  const [pendingScope, setPendingScope] = useState<PendingScope | null>(null);
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

  // 回答対象（会話の範囲）を変える。会話が無ければそのまま切り替え、会話があれば（会話ごとに固定なので）確認して新しい会話にする。
  // 入力欄のチップのメニューと、資料一覧の「全資料に戻す」は、どちらもここを通る。資料一覧の絞り込みとは別のもの。
  // 資料一覧から変えても、資料一覧に留まる（質問は自動で送らない）。範囲を選び直すと、資料の選択は外れる（setScope / startNew）
  const requestScope = (scope: ChatScope, trigger: HTMLElement | null) => {
    if (!chat.scopeUnknown && sameScope(scope, chat.scope)) return;
    if (chat.setScope(scope)) {
      if (view === 'chat') setFocusRequest((n) => n + 1);
      return;
    }
    setPendingScope({ scope, trigger });
  };

  // 範囲を選ぶ操作。いまの範囲と同じでも、資料の選択が残っていれば外す（範囲が変わらないと、requestScope では何も起きないため）
  const chooseScope = (scope: ChatScope, trigger: HTMLElement | null) => {
    if (!chat.scopeUnknown && sameScope(scope, chat.scope) && chat.documentIds !== null) {
      chat.setDocumentIds(null);
      return;
    }
    requestScope(scope, trigger);
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
    chat.startNew(pendingScope.scope);
    setPendingScope(null);
    // 資料一覧から変えたときは、資料一覧に留まる。チャットにいるときだけ、入力欄へフォーカスを移す
    if (view === 'chat') setFocusRequest((n) => n + 1);
  };

  const scopeLabel = (scope: ChatScope) =>
    scope.kind === 'all'
      ? ALL_LABEL
      : scope.kind === 'target'
        ? classificationLabel(scope.name, targetItems)
        : `旧グループ: ${collectionLabel(scope.name, groups ?? undefined)}`;
  // 画面で資料を選んでいる間は、会話の範囲より選んだ資料が優先される（ターンごとに送る）
  const selecting = chat.documentIds !== null;
  const nothingSelected = chat.documentIds?.length === 0;
  const currentLabel = selecting ? SELECTED_LABEL : chat.scopeUnknown ? UNKNOWN_LABEL : scopeLabel(chat.scope);
  const current = chat.scope;
  const currentTarget = current.kind === 'target' ? targets?.find((t) => t.name === current.name) : undefined;
  const currentGroup = current.kind === 'collection' ? groups?.find((g) => g.name === current.name) : undefined;
  const allCount = groups?.reduce((sum, g) => sum + g.documentCount, 0);
  // チップに出す対象の資料数。範囲が分からない間・一覧を読めなかったときは出さない
  const contextCount = chat.documentIds
    ? chat.documentIds.length
    : chat.scopeUnknown
    ? undefined
    : chat.scope.kind === 'all'
      ? allCount
      : chat.scope.kind === 'target'
        ? currentTarget?.documentCount
        : currentGroup?.documentCount;
  // 回答対象を選べるときだけ、メニューを出す。旧グループの会話は、いまの範囲を示す項目（選べない）を先頭に足す
  const menuItems: TargetMenuItem[] | null = targets
    ? [
        ...(chat.scope.kind === 'collection' && !chat.scopeUnknown
          ? [
              {
                key: 'legacy',
                scope: chat.scope,
                title: scopeLabel(chat.scope),
                count: currentGroup?.documentCount,
                note: 'この会話で固定（新しくは選べません）',
                checked: true,
                disabled: true,
              },
            ]
          : []),
        { key: 'all', scope: ALL_SCOPE, title: ALL_LABEL, count: allCount, checked: !selecting && !chat.scopeUnknown && chat.scope.kind === 'all' },
        ...targets.map((t) => ({
          key: t.name,
          scope: { kind: 'target', name: t.name } as ChatScope,
          title: t.label,
          count: t.documentCount,
          note: isPreparing(t) ? '準備中（取り込み中の資料だけ）' : undefined,
          checked: !selecting && !chat.scopeUnknown && chat.scope.kind === 'target' && chat.scope.name === t.name,
        })),
      ]
    : null;
  const targetEmpty = !selecting && !chat.scopeUnknown && chat.scope.kind === 'target' && currentTarget !== undefined && isPreparing(currentTarget);

  // 回答に使う資料の選択（資料一覧のチェックボックス）。資料の一覧を読めている間だけ出す
  const listedDocuments = documentList.status === 'ready' ? documentList.data.documents : null;
  const selection =
    listedDocuments === null
      ? undefined
      : {
          selected: effectiveSelection(listedDocuments, chat.scope, chat.scopeUnknown, chat.documentIds),
          onChange: (next: ReadonlySet<number>) =>
            chat.setDocumentIds(normalizeSelection(next, listedDocuments, chat.scope, chat.scopeUnknown)),
          onReset: (trigger: HTMLElement) => chooseScope(ALL_SCOPE, trigger),
          isAll: !selecting && !chat.scopeUnknown && chat.scope.kind === 'all',
        };

  const goChat = () => {
    navigate({ tab: 'chat' });
    setFocusRequest((n) => n + 1);
  };

  return (
    <div
      className={panelOpen ? 'ws-shell ws-shell-panel' : 'ws-shell'}
      data-expanded={panelOpen && expanded ? 'true' : undefined}
      data-side={sideOpen ? undefined : 'closed'}
    >
      <AppSidebar
        view={view}
        mode={chat.mode ?? healthMode}
        userLabel={state.status === 'signedIn' ? (state.user.email ?? 'ログイン中') : undefined}
        onSignOut={signedIn ? () => void signOut() : undefined}
        onNewConversation={() => {
          // いまの会話と検索範囲を閉じて、チャットで新しく始める
          chat.reset();
          goChat();
        }}
        onCollapse={() => toggleSide(false)}
        collapseRef={collapseRef}
        // 確認は出さない。いまの会話と回答対象のまま、チャットへ移る（回答対象を変えるのは、資料一覧のボタンと入力欄のチップ）
        onGoChat={goChat}
        onGoDocuments={() => navigate({ tab: 'documents' })}
        knowledgeInMenu
        history={
          // ログインを使わない構成でも、欄は出す（出せない理由を示す。何も無いのと区別がつかないため）
          <ThreadSidebar
            state={threads.state}
            currentId={chat.threadId}
            busy={chat.pending}
            onSelect={(threadId) => {
              // 資料一覧を見ていても、選んだ会話はチャットで開く
              if (view !== 'chat') navigate({ tab: 'chat' });
              chat.resume(threadId);
            }}
            onDelete={removeThread}
            onRetry={threads.reload}
          />
        }
      />
      {/* 拡大中は、パネルが上に重なる。下のチャット・資料一覧は作り直さず、操作と読み上げだけ外す */}
      <div className="ws-main" inert={panelOpen && expanded}>
        {/* サイドバーを閉じている間も、再表示のボタンは残す（チャットでも資料一覧でも、同じ場所） */}
        {!sideOpen && (
          <div className="ws-main-bar">
            <button ref={reopenRef} type="button" className="ws-icon-btn" aria-label="サイドバーを開く" onClick={() => toggleSide(true)}>
              <SidebarIcon />
            </button>
          </div>
        )}
        {pendingScope && (
          <ScopeConfirm
            targetLabel={scopeLabel(pendingScope.scope)}
            generating={chat.pending}
            onConfirm={confirmScope}
            onCancel={cancelScope}
          />
        )}
        {/* 資料一覧を開いている間も、チャットは隠すだけで残す（生成中の回答・入力途中の文・スクロール位置を失わない） */}
        <div className="chat-layout" hidden={view !== 'chat'}>
          <div className="chat-pane">
            {chat.scopeUnavailable && (
              <p className="scope-warning" role="status">
                {MSG_SCOPE_UNAVAILABLE}
              </p>
            )}
            {targetEmpty && !chat.hasConversation && (
              <p className="scope-warning" role="status">
                {MSG_TARGET_EMPTY}
              </p>
            )}
            <MessageList
              messages={chat.messages}
              waiting={chat.waiting}
              phase={chat.phase}
              busy={chat.pending}
              resumed={chat.threadId !== null && chat.messages.length === 0}
              history={chat.history}
              onExample={chat.pending ? undefined : chat.send}
              onAskAll={targets || groups ? chat.askAgainInAll : undefined}
            />
            {chat.error && (
              <ErrorBanner
                error={chat.error}
                disabled={chat.pending}
                onRetry={chat.retry}
                onNewConversation={targets || groups ? chat.reset : undefined}
              />
            )}
            {chat.scopeMismatch && (
              <p className="scope-warning" role="status">
                {MSG_SCOPE_MISMATCH}
              </p>
            )}
            {nothingSelected && (
              <p className="scope-warning" role="status">
                {MSG_NOTHING_SELECTED}
              </p>
            )}
            <Composer
              pending={chat.pending}
              sendBlocked={nothingSelected}
              onSend={chat.send}
              onStop={chat.stop}
              scopeControl={
                <ScopePicker
                  targetLabel={currentLabel}
                  documentCount={contextCount}
                  items={menuItems}
                  failed={classifications.targets.status === 'error'}
                  onRetry={classifications.reloadTargets}
                  fixed={chat.hasConversation}
                  selecting={selecting}
                  disabled={chat.pending}
                  onSelect={chooseScope}
                />
              }
            />
          </div>
        </div>
        {view === 'documents' && (
          <DocumentsView
            state={documentList}
            onReload={reloadDocuments}
            selection={selection}
            onClose={goChat}
          />
        )}
      </div>
      <PanelHost
        list={documentList}
        onReload={reloadDocuments}
        expanded={expanded}
        onToggleExpand={() => setExpanded((v) => !v)}
        onClosed={() => setExpanded(false)}
      />
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
      <ClassificationProvider>
        <ChatScreen />
      </ClassificationProvider>
    </CollectionsProvider>
  );
}
