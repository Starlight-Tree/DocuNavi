import { useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent } from 'react';
import { MAX_MESSAGE_LENGTH } from '../api/chat';

interface ComposerProps {
  pending: boolean;
  /** 送信を受け付けたら true を返す。false のときは入力を消さない。 */
  onSend: (text: string) => boolean;
}

export function Composer({ pending, onSend }: ComposerProps) {
  const [draft, setDraft] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const canSend = !pending && draft.trim().length > 0;

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
      <textarea
        ref={inputRef}
        id="message-input"
        className="composer-input"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={handleKeyDown}
        maxLength={MAX_MESSAGE_LENGTH}
        rows={2}
        placeholder="メッセージを入力（Enter で送信 / Shift+Enter で改行）"
        aria-describedby="message-counter"
      />
      <div className="composer-footer">
        <span id="message-counter" className="counter">
          {draft.length} / {MAX_MESSAGE_LENGTH}
        </span>
        <button type="submit" className="btn btn-primary" disabled={!canSend}>
          送信
        </button>
      </div>
    </form>
  );
}
