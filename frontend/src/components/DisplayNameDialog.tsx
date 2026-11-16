import { useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { DISPLAY_NAME_MAX, displayNameLength, validateDisplayName } from '../lib/displayName';
import { Modal } from './Modal';

const MSG_SAVE_FAILED = '表示名を保存できませんでした。しばらく待ってから、もう一度お試しください。';

interface DisplayNameDialogProps {
  /** いまの表示名。無ければ空。 */
  initial: string;
  /** 保存する。失敗したら例外を投げる（本文は画面に出さない）。 */
  onSave: (name: string) => Promise<void>;
  onClose: () => void;
}

/** 表示名の変更。この名前は、このアプリの中で自分にだけ表示される。 */
export function DisplayNameDialog({ initial, onSave, onClose }: DisplayNameDialogProps) {
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (saving) return;
    const checked = validateDisplayName(value);
    if (!checked.ok) {
      setError(checked.message);
      inputRef.current?.focus();
      return;
    }
    if (checked.name === initial.trim()) {
      onClose(); // 変わっていなければ、保存せずに閉じる
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSave(checked.name);
      onClose();
    } catch {
      setError(MSG_SAVE_FAILED);
      setSaving(false);
    }
  };

  return (
    <Modal title="表示名を変更" initialFocus={inputRef} closeOnBackdrop={false} onClose={onClose}>
      <form onSubmit={(event) => void submit(event)} noValidate>
        <div className="ws-modal-body">
          <p className="ws-note">この名前は、このアプリの中で自分にだけ表示されます。</p>
          <div className="ws-name-field">
            <label htmlFor="display-name-input" className="ws-name-label">
              表示名
            </label>
            <input
              ref={inputRef}
              id="display-name-input"
              type="text"
              className="ws-name-input"
              value={value}
              autoComplete="nickname"
              aria-invalid={error !== null && !saving ? true : undefined}
              aria-describedby="display-name-hint"
              disabled={saving}
              onChange={(event) => {
                setValue(event.target.value);
                setError(null);
              }}
            />
            <div className="ws-name-meta">
              <span id="display-name-hint">1〜{DISPLAY_NAME_MAX} 文字（前後の空白は除きます）</span>
              <span aria-hidden="true">
                {displayNameLength(value)} / {DISPLAY_NAME_MAX}
              </span>
            </div>
          </div>
          {error && (
            <p role="alert" className="ws-name-error">
              {error}
            </p>
          )}
        </div>
        <footer className="ws-modal-foot">
          <button type="button" className="btn btn-secondary" disabled={saving} onClick={onClose}>
            キャンセル
          </button>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? '保存中…' : '保存'}
          </button>
        </footer>
      </form>
    </Modal>
  );
}
