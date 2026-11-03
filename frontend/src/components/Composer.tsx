import { useLayoutEffect, useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent } from 'react';
import { MAX_MESSAGE_LENGTH } from '../api/chat';

interface ComposerProps {
  pending: boolean;
  /** 送信を受け付けたら true を返す。false のときは入力を消さない。 */
  onSend: (text: string) => boolean;
  /** 回答の生成を止める。 */
  onStop: () => void;
  /**
   * いまの検索範囲の表示（「すべての資料」「設定資料」など）。入力欄の中に出す。渡さなければ（グループの機能を出さないとき）何も出さない。
   */
  scopeLabel?: string | undefined;
  /** 範囲に添える一言（「この会話では固定」「資料はまだありません」など）。 */
  scopeNote?: string | undefined;
  /** 範囲の見出し（既定は「検索範囲」。対象で絞る画面は「質問対象」）。 */
  scopeCaption?: string | undefined;
}

export function Composer({ pending, onSend, onStop, scopeLabel, scopeNote, scopeCaption = '検索範囲' }: ComposerProps) {
  const [draft, setDraft] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const canSend = !pending && draft.trim().length > 0;

  // 長い文でも全体が見えるよう、入力に合わせて高さを伸ばす（上限は CSS の max-height。超えたら中でスクロールする）。
  // 高さを測れない環境（scrollHeight が 0）では、CSS の既定のままにする
  useLayoutEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = 'auto';
    if (input.scrollHeight > 0) input.style.height = `${input.scrollHeight}px`;
  }, [draft]);

  const submit = () => {
    if (!onSend(draft)) return;
    setDraft('');
    // 送信ボタンで送ると、ボタンが無効化されてフォーカスが失われるため入力欄へ戻す
    inputRef.current?.focus();
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    submit();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey) return;
    // IME の変換確定の Enter は送信しない（Safari は確定後に keyCode 229 の keydown が来る）
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    submit();
  };

  return (
    <form className="composer" onSubmit={handleSubmit}>
      <label htmlFor="message-input" className="visually-hidden">
        メッセージ
      </label>
      <div className="composer-field">
        {scopeLabel !== undefined && (
          <p id="composer-scope" className="composer-scope">
            <span className="composer-scope-name">
              {scopeCaption}: {scopeLabel}
            </span>
            {scopeNote && <span className="composer-scope-note">{scopeNote}</span>}
          </p>
        )}
        <textarea
          ref={inputRef}
          id="message-input"
          className="composer-input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          maxLength={MAX_MESSAGE_LENGTH}
          rows={1}
          placeholder="メッセージを入力（Enter で送信 / Shift+Enter で改行）"
          aria-describedby={scopeLabel !== undefined ? 'composer-scope message-counter' : 'message-counter'}
        />
      </div>
      <div className="composer-footer">
        <span id="message-counter" className="counter">
          {draft.length} / {MAX_MESSAGE_LENGTH}
        </span>
        <div className="composer-actions">
          {pending && (
            <button type="button" className="btn btn-secondary" onClick={onStop}>
              停止
            </button>
          )}
          <button type="submit" className="btn btn-primary" disabled={!canSend}>
            送信
          </button>
        </div>
      </div>
    </form>
  );
}
