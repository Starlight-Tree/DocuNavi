import { Fragment, useEffect, useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent } from 'react';
import { useMock } from './MockProvider';
import { activeThread, pendingUploads, scopeLabel, sendBlock } from './store';
import type { MockMessage, SourceRef } from './types';

const MAX_LENGTH = 2000;

const BLOCK_NOTE = {
  waiting: '選んだ資料は取り込み中です。完了すると質問できます。',
  'no-docs': '質問できる資料がありません。資料を追加するか、質問対象を変えてください。',
} as const;

/** 回答の本文。`[1]` は、その番号の出典を右パネルで開くボタンにする。 */
function AnswerText({ message, onOpen }: { message: MockMessage; onOpen: (index: number) => void }) {
  const parts = message.text.split(/(\[\d+\])/);
  return (
    <p className="ws-answer-text">
      {parts.map((part, i) => {
        const match = /^\[(\d+)\]$/.exec(part);
        const index = match ? Number(match[1]) - 1 : -1;
        if (match && message.sources?.[index]) {
          return (
            <button key={i} type="button" className="ws-cite" aria-label={`出典${match[1]}を開く`} onClick={() => onOpen(index)}>
              {match[1]}
            </button>
          );
        }
        return <Fragment key={i}>{part}</Fragment>;
      })}
    </p>
  );
}

export function ChatView() {
  const { state, dispatch } = useMock();
  const thread = activeThread(state);
  const empty = thread.messages.length === 0;
  const block = sendBlock(state, thread);
  const waitingUploads = pendingUploads(state, thread);
  const [draft, setDraft] = useState('');
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // 新しい発言・回答が増えたら、最新が見える位置までスクロールする
  const messageCount = thread.messages.length;
  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [messageCount, thread.pending, thread.id]);

  // 会話を切り替えたら、入力欄へ戻す（下書きは会話ごとに持たない）
  useEffect(() => {
    setDraft('');
    inputRef.current?.focus();
  }, [thread.id]);

  // 資料画面からチャットへ戻ったときも、入力欄へ戻す
  const chatVisible = state.view === 'chat';
  useEffect(() => {
    if (chatVisible) inputRef.current?.focus();
  }, [chatVisible]);

  const canSend = block === null && draft.trim().length > 0;
  const submit = () => {
    if (!canSend) return;
    dispatch({ type: 'send', text: draft, now: Date.now() });
    setDraft('');
    inputRef.current?.focus();
  };
  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    submit();
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey) return;
    // IME の変換確定の Enter は送信しない
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    submit();
  };

  const openSource = (source: SourceRef, number: number) =>
    dispatch({
      type: 'openPanel',
      target: { docId: source.docId, docName: source.docName, citation: number, focus: { page: source.page, para: source.para, location: source.location } },
    });
  const isOpen = (source: SourceRef) =>
    state.panel?.docId === source.docId && state.panel.focus?.page === source.page && state.panel.focus.para === source.para;

  const composer = (
    <form className="ws-composer" onSubmit={handleSubmit}>
      <label htmlFor="ws-input" className="visually-hidden">
        質問
      </label>
      <textarea
        ref={inputRef}
        id="ws-input"
        className="ws-input"
        rows={empty ? 3 : 2}
        value={draft}
        maxLength={MAX_LENGTH}
        placeholder="資料について質問を入力（Enter で送信 / Shift+Enter で改行）"
        aria-describedby="ws-scope-line"
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={handleKeyDown}
      />
      <div className="ws-composer-bar">
        <div className="ws-composer-tools">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => dispatch({ type: 'openDialog', dialog: { type: 'upload', origin: 'chat' } })}
          >
            ＋ 資料を追加
          </button>
          <button
            type="button"
            className="btn btn-secondary ws-scope-btn"
            aria-haspopup="dialog"
            onClick={() => dispatch({ type: 'openDialog', dialog: { type: 'scope' } })}
          >
            <span className="ws-scope-label">質問対象</span>
            <span id="ws-scope-line" className="ws-scope-value">
              {scopeLabel(state, thread.scope)}
            </span>
            <span aria-hidden="true">▾</span>
          </button>
        </div>
        <button type="submit" className="btn btn-primary" disabled={!canSend}>
          送信
        </button>
      </div>
    </form>
  );

  const notices = (
    <div className="ws-notices" role="status" aria-live="polite">
      {waitingUploads.length > 0 && (
        <p className="ws-notice">
          <span className="ws-spinner" aria-hidden="true" />
          「{waitingUploads[0]?.name}」{waitingUploads.length > 1 ? `ほか${waitingUploads.length - 1}件` : ''}を取り込み中です。完了後に質問対象になります。
        </p>
      )}
      {block === 'waiting' || block === 'no-docs' ? <p className="ws-notice ws-notice-warn">{BLOCK_NOTE[block]}</p> : null}
    </div>
  );

  if (empty) {
    return (
      <div className="ws-chat ws-chat-empty">
        <div className="ws-empty-inner">
          <h1 className="ws-hero">何を調べますか？</h1>
          <p className="ws-hero-sub">登録した資料について質問できます。取り込み済みの資料だけが質問対象になります。</p>
          {composer}
          {notices}
          <p className="ws-hint">UI モックです。回答は定型文で、実際の検索・生成はしません。</p>
        </div>
      </div>
    );
  }

  return (
    <div className="ws-chat">
      <header className="ws-chat-head">
        <h1 className="ws-chat-title">{thread.title}</h1>
        <span className="ws-badge-mock">UI モック</span>
      </header>
      <div ref={listRef} className="ws-messages" role="log" aria-label="会話" aria-live="polite">
        <div className="ws-messages-inner">
          {thread.messages.map((message) => {
            if (message.role === 'system') {
              return (
                <p key={message.id} className="ws-system">
                  {message.text}
                </p>
              );
            }
            if (message.role === 'user') {
              return (
                <div key={message.id} className="ws-msg ws-msg-user">
                  <p className="ws-bubble">{message.text}</p>
                </div>
              );
            }
            const sources = message.sources ?? [];
            return (
              <div key={message.id} className="ws-msg ws-msg-assistant">
                <AnswerText message={message} onOpen={(i) => sources[i] && openSource(sources[i], i + 1)} />
                <span className="ws-badge-mock" title="定型の回答です。実際の検索・生成はしていません">
                  モック応答
                </span>
                {sources.length > 0 && (
                  <div className="ws-sources">
                    <h3 className="ws-sources-title">出典</h3>
                    <ul>
                      {sources.map((source, i) => (
                        <li key={`${source.docId}-${source.page}-${source.para}`}>
                          <button type="button" className="ws-source" aria-pressed={isOpen(source)} onClick={() => openSource(source, i + 1)}>
                            <span className="ws-cite-num" aria-hidden="true">
                              {i + 1}
                            </span>
                            <span className="ws-source-name">{source.docName}</span>
                            <span className="ws-source-loc">{source.location}</span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            );
          })}
          {thread.pending && (
            <p className="ws-msg ws-pending" role="status">
              <span className="ws-spinner" aria-hidden="true" />
              回答を作成しています（モック）…
            </p>
          )}
        </div>
      </div>
      <div className="ws-composer-wrap">
        {notices}
        {composer}
      </div>
    </div>
  );
}
