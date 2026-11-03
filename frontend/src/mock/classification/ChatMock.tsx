import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { Composer } from '../../components/Composer';
import { MessageList } from '../../components/MessageList';
import type { ChatMessage } from '../../hooks/useChat';
import { DOCUMENTS, GROUPS, label, TARGETS, targetCatalog } from './data';
import type { MockDocument } from './data';

/** 質問の範囲。`legacy` は旧グループ（`collection`）で固定された過去の会話（新しくは選べない）。`unavailable` は保存された範囲が、いま利用できない会話。 */
export type ChatScope =
  | { kind: 'all' }
  | { kind: 'target'; name: string }
  | { kind: 'legacy'; name: string }
  | { kind: 'unavailable' };

export const ALL_SCOPE: ChatScope = { kind: 'all' };

export function scopeText(scope: ChatScope): string {
  if (scope.kind === 'all') return 'すべての資料';
  if (scope.kind === 'target') return label(TARGETS, scope.name);
  if (scope.kind === 'legacy') return `旧グループ: ${label(GROUPS, scope.name)}`;
  return 'この会話の対象';
}

/** 範囲に入る、検索できる（取り込み済み）資料。 */
export function readyDocuments(scope: ChatScope, documents: readonly MockDocument[] = DOCUMENTS): MockDocument[] {
  const ready = documents.filter((d) => d.status === 'ready');
  if (scope.kind === 'all') return ready;
  if (scope.kind === 'target') return ready.filter((d) => d.target === scope.name);
  if (scope.kind === 'legacy') return ready.filter((d) => d.collection === scope.name);
  return [];
}

function scopeCount(scope: ChatScope): number | undefined {
  if (scope.kind === 'unavailable') return undefined;
  if (scope.kind === 'all') return DOCUMENTS.length;
  if (scope.kind === 'target') return DOCUMENTS.filter((d) => d.target === scope.name).length;
  return DOCUMENTS.filter((d) => d.collection === scope.name).length;
}

interface MenuItem {
  key: string;
  scope: ChatScope;
  title: string;
  count: number;
  note?: string;
  checked: boolean;
  disabled?: boolean;
}

interface TargetBarProps {
  scope: ChatScope;
  /** 会話がある（対象は会話ごとに固定）。 */
  fixed: boolean;
  /** 回答を作っている間は変えられない。 */
  disabled: boolean;
  onSelect: (scope: ChatScope) => void;
  onBack: () => void;
}

/**
 * チャットの上の帯。いま何について質問しているか（質問対象）と、変更する操作を、ここ 1 か所に置く。
 * 変更はメニュー（対象が増えても、横長の列に頼らない）。会話があるときは、選ぶと確認が出て、新しい会話になる。
 */
export function TargetBar({ scope, fixed, disabled, onSelect, onBack }: TargetBarProps) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);
  const menuId = useId();

  const items: MenuItem[] = [
    ...(scope.kind === 'legacy'
      ? [{ key: 'legacy', scope, title: scopeText(scope), count: scopeCount(scope) ?? 0, note: 'この会話で固定（新しくは選べません）', checked: true, disabled: true }]
      : []),
    { key: 'all', scope: ALL_SCOPE, title: 'すべての資料', count: DOCUMENTS.length, checked: scope.kind === 'all' },
    ...targetCatalog().map((t) => ({
      key: t.name,
      scope: { kind: 'target', name: t.name } as ChatScope,
      title: t.label,
      count: t.documentCount,
      ...(t.readyCount === 0 ? { note: '準備中（取り込み中の資料だけ）' } : {}),
      checked: scope.kind === 'target' && scope.name === t.name,
    })),
  ];

  const close = (restore: boolean) => {
    setOpen(false);
    if (restore) buttonRef.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLElement>('[aria-checked="true"]:not([disabled]), [role="menuitemradio"]:not([disabled])')?.focus();
    const onPointer = (event: MouseEvent) => {
      const node = event.target as Node;
      if (!menuRef.current?.contains(node) && !buttonRef.current?.contains(node)) setOpen(false);
    };
    document.addEventListener('mousedown', onPointer);
    return () => document.removeEventListener('mousedown', onPointer);
  }, [open]);

  const handleKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    const nodes = [...(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]:not([disabled])') ?? [])];
    const index = nodes.findIndex((n) => n === document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      nodes[(index + (event.key === 'ArrowDown' ? 1 : -1) + nodes.length) % nodes.length]?.focus();
    } else if (event.key === 'Tab') {
      close(false);
    }
  };

  const count = scopeCount(scope);
  return (
    <div className="chat-context-bar cm-bar">
      <button type="button" className="btn-link chat-context-back" onClick={onBack}>
        <span aria-hidden="true">‹ </span>資料一覧へ
      </button>
      <span className="cm-bar-label" id={`${menuId}-label`}>
        質問対象
      </span>
      <div className="cm-menu-wrap">
        <button
          ref={buttonRef}
          type="button"
          className="cm-target-btn"
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={open ? menuId : undefined}
          aria-labelledby={`${menuId}-label ${menuId}-value`}
          disabled={disabled}
          onClick={() => setOpen((v) => !v)}
        >
          <span id={`${menuId}-value`}>
            {scopeText(scope)}
            {count !== undefined && `（${count}件）`}
          </span>
          <span aria-hidden="true">▾</span>
        </button>
        {open && (
          <ul ref={menuRef} id={menuId} role="menu" aria-label="質問対象を選ぶ" className="cm-menu" onKeyDown={handleKeyDown}>
            {items.map((item) => (
              <li key={item.key} role="none">
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={item.checked}
                  className="cm-menu-item"
                  disabled={item.disabled}
                  onClick={() => {
                    close(true);
                    if (!item.checked) onSelect(item.scope);
                  }}
                >
                  <span className="cm-menu-check" aria-hidden="true">
                    {item.checked ? '✓' : ''}
                  </span>
                  <span className="cm-menu-text">
                    <span>{item.title}</span>
                    {item.note && <span className="cm-menu-note">{item.note}</span>}
                  </span>
                  <span className="cm-menu-count">{item.count}件</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <span className="cm-bar-hint" title="質問対象は会話ごとに固定です。会話があるときに変えると、確認のあとで新しい会話になります。">
        {disabled ? '回答の生成中は変えられません' : fixed ? 'この会話で固定' : '最初の質問で固定'}
      </span>
    </div>
  );
}

interface ConfirmProps {
  nextLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}

/** 会話があるときに質問対象を変える確認。「いまの会話を続ける」が既定（フォーカスはそこ）。 */
export function ScopeConfirmMock({ nextLabel, onConfirm, onCancel }: ConfirmProps) {
  const keepRef = useRef<HTMLButtonElement>(null);
  useEffect(() => keepRef.current?.focus(), []);
  return (
    <div
      className="scope-confirm"
      role="alertdialog"
      aria-labelledby="cm-confirm-title"
      aria-describedby="cm-confirm-body"
      onKeyDown={(event) => {
        if (event.key === 'Escape') onCancel();
      }}
    >
      <div className="scope-confirm-text">
        <p id="cm-confirm-title" className="scope-confirm-title">
          質問対象を「{nextLabel}」に変えますか？
        </p>
        <p id="cm-confirm-body" className="scope-confirm-body">
          質問対象は会話ごとに固定なので、新しい会話に切り替えます。いまの会話は閉じます（ログイン中は、一覧から開き直せます）。
        </p>
      </div>
      <div className="scope-confirm-actions">
        <button ref={keepRef} type="button" className="btn btn-secondary" onClick={onCancel}>
          いまの会話を続ける
        </button>
        <button type="button" className="btn btn-primary" onClick={onConfirm}>
          新しい会話にする
        </button>
      </div>
    </div>
  );
}

interface ChatProps {
  scope: ChatScope;
  messages: ChatMessage[];
  pending: boolean;
  confirm: ChatScope | null;
  onSelect: (scope: ChatScope) => void;
  onConfirm: () => void;
  onCancel: () => void;
  onSend: (text: string) => boolean;
  onBack: () => void;
}

export function ChatMock({ scope, messages, pending, confirm, onSelect, onConfirm, onCancel, onSend, onBack }: ChatProps) {
  const last = messages[messages.length - 1];
  const emptyScope = readyDocuments(scope).length === 0 && scope.kind !== 'unavailable';
  return (
    <div className="chat-layout">
      <div className="chat-pane">
        <TargetBar scope={scope} fixed={messages.length > 0} disabled={pending} onSelect={onSelect} onBack={onBack} />
        {confirm && <ScopeConfirmMock nextLabel={scopeText(confirm)} onConfirm={onConfirm} onCancel={onCancel} />}
        {scope.kind === 'unavailable' && (
          <p className="scope-warning cm-notice" role="status">
            この会話の対象は、いま利用できません（資料が別の対象へ移った、または閲覧できなくなった可能性があります）。続きを送っても、保存された対象のまま探します。新しい会話で、対象を選び直せます。
          </p>
        )}
        {emptyScope && messages.length === 0 && (
          <p className="scope-warning cm-notice" role="status">
            この対象には、いま検索できる資料がありません（取り込みが終わると使えます）。
          </p>
        )}
        <MessageList messages={messages} waiting={pending} busy={pending} onExample={pending ? undefined : onSend} />
        {last?.role === 'assistant' && last.hitCount === 0 && !pending && (
          <div className="scope-empty cm-empty" role="status">
            <p>「{scopeText(scope)}」の中には、いま検索できる資料がありませんでした。</p>
          </div>
        )}
        <Composer
          pending={pending}
          onSend={onSend}
          onStop={() => undefined}
          scopeLabel={scopeText(scope)}
          scopeCaption="質問対象"
          scopeNote={messages.length > 0 ? 'この会話で固定' : undefined}
        />
      </div>
    </div>
  );
}
