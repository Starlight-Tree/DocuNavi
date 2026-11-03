import { useState } from 'react';
import { AppSidebar } from '../../components/AppSidebar';
import type { ChatMessage } from '../../hooks/useChat';
import { ALL_SCOPE, ChatMock, readyDocuments, scopeText } from './ChatMock';
import type { ChatScope } from './ChatMock';
import { DOCUMENTS } from './data';
import type { MockDocument } from './data';
import { EMPTY_FILTER, LibraryMock } from './Library';
import type { LibraryFilter } from './Library';
import './classification.css';

type Tab = 'chat' | 'documents';

interface Scenario {
  key: string;
  title: string;
  hint: string;
}

/** 確認用の状態。実際の画面では、URL・会話の保存内容・分類の変更によって起きる。 */
const SCENARIOS: readonly Scenario[] = [
  { key: 'normal', title: '通常', hint: '資料画面から始める。' },
  { key: 'legacy-url', title: '旧 URL（collection）', hint: '?collection=cafe。旧グループのフィルターが有効。「旧フィルターを解除」で外す。' },
  { key: 'both-url', title: '旧 URL + 対象', hint: '?collection=guides と対象（あおば電機 AW-820）の両方を AND で適用し、両方を表示する。' },
  { key: 'legacy-chat', title: '旧グループの会話を開いた', hint: 'チャットは「旧グループ: 規程・取扱説明書」で固定。新しくは選べない。' },
  { key: 'unavailable-chat', title: '対象が使えない会話', hint: '保存された対象が空になった会話。続きは送れるが、検索は 0 件になる。' },
];

const SAMPLE: ChatMessage[] = [
  { id: 'u1', role: 'user', text: '入寮の申し込みはいつまでですか？' },
  {
    id: 'a1',
    role: 'assistant',
    text: '入寮の申し込み期限は、毎年3月末日です。',
    context: '[tsubasa_dorm_rules.pdf]\n入寮の申し込み期限は、毎年3月末日です。',
    sources: [{ name: 'tsubasa_dorm_rules.pdf', page: 2, pageEnd: null, weak: false, collection: 'guides' }],
  },
];

function initial(scenario: string): { tab: Tab; filter: LibraryFilter; legacy: string | null; scope: ChatScope; messages: ChatMessage[] } {
  const base = { tab: 'documents' as Tab, filter: EMPTY_FILTER, legacy: null as string | null, scope: ALL_SCOPE as ChatScope, messages: [] as ChatMessage[] };
  if (scenario === 'legacy-url') return { ...base, legacy: 'cafe' };
  if (scenario === 'both-url') return { ...base, legacy: 'guides', filter: { ...EMPTY_FILTER, target: 'aoba-aw820' } };
  if (scenario === 'legacy-chat') return { ...base, tab: 'chat', scope: { kind: 'legacy', name: 'guides' }, messages: SAMPLE };
  if (scenario === 'unavailable-chat') return { ...base, tab: 'chat', scope: { kind: 'unavailable' }, messages: SAMPLE };
  return base;
}

/** 回答の本文と出典（モック）。範囲に検索できる資料が無ければ、0 件の回答になる。 */
function mockAnswer(scope: ChatScope, text: string, seq: number): ChatMessage {
  const found = readyDocuments(scope);
  const hit: MockDocument | undefined = found.find((d) => d.excerpt && [...text].some((c) => c.trim() && d.excerpt.includes(c) && d.title.includes(c))) ?? found[0];
  if (!hit) return { id: `a${seq}`, role: 'assistant', text: '該当する資料が見つかりませんでした。', hitCount: 0 };
  return {
    id: `a${seq}`,
    role: 'assistant',
    text: `${hit.excerpt}（モックの回答です）`,
    context: `[${hit.source}]\n${hit.excerpt}`,
    sources: [{ name: hit.source, page: 1, pageEnd: null, weak: false, collection: hit.collection }],
    hitCount: 1,
  };
}

/**
 * 「対象」「資料種別」の UI モック。資料ライブラリ（検索・対象・資料種別 + 詳細フィルター）と、チャットの質問対象（上の帯のメニュー）を、
 * 架空データで確認する。通信・保存・認証は使わない。本体の画面には触れない（承認後に、同じ構成で本体へ移す）。
 */
export default function ClassificationMock() {
  const [scenario, setScenario] = useState('normal');
  const [start] = useState(() => initial('normal'));
  const [tab, setTab] = useState<Tab>(start.tab);
  const [filter, setFilter] = useState<LibraryFilter>(start.filter);
  const [legacy, setLegacy] = useState<string | null>(start.legacy);
  const [scope, setScope] = useState<ChatScope>(start.scope);
  const [messages, setMessages] = useState<ChatMessage[]>(start.messages);
  const [pending, setPending] = useState(false);
  const [confirm, setConfirm] = useState<ChatScope | null>(null);
  const [seq, setSeq] = useState(10);
  const [openId, setOpenId] = useState<number | null>(null);

  const reset = (key: string) => {
    const next = initial(key);
    setScenario(key);
    setTab(next.tab);
    setFilter(next.filter);
    setLegacy(next.legacy);
    setScope(next.scope);
    setMessages(next.messages);
    setConfirm(null);
    setPending(false);
    setOpenId(null);
  };

  /** 質問対象を変える。会話が無ければそのまま、あれば確認して新しい会話にする。 */
  const requestScope = (next: ChatScope) => {
    if (JSON.stringify(next) === JSON.stringify(scope) && messages.length === 0) return;
    if (messages.length === 0) setScope(next);
    else setConfirm(next);
  };

  const send = (text: string): boolean => {
    const question = text.trim();
    if (!question || pending) return false;
    setMessages((prev) => [...prev, { id: `u${seq}`, role: 'user', text: question }]);
    setPending(true);
    setSeq(seq + 1);
    window.setTimeout(() => {
      setMessages((prev) => [...prev, mockAnswer(scope, question, seq + 1)]);
      setPending(false);
    }, 700);
    return true;
  };

  const ask = (target: string | null) => {
    requestScope(target === null ? ALL_SCOPE : { kind: 'target', name: target });
    setTab('chat');
  };

  return (
    <div className="cm-root">
      <aside className="cm-scenarios" aria-label="確認用の切り替え">
        <p className="cm-scenarios-title">UI モック（架空データ・表示の確認用）</p>
        <div className="cm-scenarios-list" role="radiogroup" aria-label="確認する状態">
          {SCENARIOS.map((s) => (
            <label key={s.key} className="cm-scenario" data-checked={scenario === s.key ? 'true' : undefined}>
              <input type="radio" name="scenario" checked={scenario === s.key} onChange={() => reset(s.key)} />
              <span>{s.title}</span>
            </label>
          ))}
        </div>
        <p className="cm-scenario-hint" role="status">
          {SCENARIOS.find((s) => s.key === scenario)?.hint}（切り替えると、状態を初期化します）
        </p>
      </aside>
      <div className="ws-shell cm-shell">
        <AppSidebar
          view={tab}
          mode="mock"
          documentCount={DOCUMENTS.length}
          onNewConversation={() => {
            setMessages([]);
            setScope(ALL_SCOPE);
            setConfirm(null);
            setTab('chat');
          }}
          onGoChat={() => setTab('chat')}
          onGoDocuments={() => setTab('documents')}
        />
        <div className="ws-main">
          {tab === 'documents' ? (
            <LibraryMock
              filter={filter}
              onFilterChange={(next) => setFilter((prev) => ({ ...prev, ...next }))}
              legacy={legacy}
              onClearLegacy={() => setLegacy(null)}
              onAsk={ask}
              chatTargetLabel={scopeText(scope)}
              onOpenDocument={(d) => setOpenId(d.id)}
              openId={openId}
            />
          ) : (
            <ChatMock
              scope={scope}
              messages={messages}
              pending={pending}
              confirm={confirm}
              onSelect={requestScope}
              onConfirm={() => {
                if (!confirm) return;
                setScope(confirm);
                setMessages([]);
                setConfirm(null);
              }}
              onCancel={() => setConfirm(null)}
              onSend={send}
              onBack={() => setTab('documents')}
            />
          )}
        </div>
      </div>
    </div>
  );
}
