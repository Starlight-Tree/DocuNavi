import { useId, useRef, useState } from 'react';
import type { ChangeEvent, DragEvent } from 'react';
import { Modal } from './Modal';
import { ACCEPT_ATTRIBUTE, ACCEPTED_EXTENSIONS, MAX_UPLOAD_FILES, formatSize, isAcceptedName, kindOf } from './data';
import { useMock } from './MockProvider';

interface Picked {
  key: string;
  name: string;
  size: number;
}

/**
 * 資料の追加。チャットと資料画面の両方から、同じダイアログを開く。
 * ファイルの中身は読まず、外へも送らない（名前と大きさだけを使う）。追加した資料は、会話に限らず「資料」に保存される扱い。
 */
export function UploadDialog({ origin, onClose }: { origin: 'chat' | 'documents'; onClose: () => void }) {
  const { dispatch } = useMock();
  const [files, setFiles] = useState<Picked[]>([]);
  const [problems, setProblems] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const noteId = useId();

  const take = (list: FileList | File[]) => {
    const incoming = [...list];
    const messages: string[] = [];
    const next = [...files];
    const rejected: string[] = [];
    for (const file of incoming) {
      if (!isAcceptedName(file.name)) {
        rejected.push(file.name);
        continue;
      }
      if (next.some((f) => f.name === file.name && f.size === file.size)) continue;
      if (next.length >= MAX_UPLOAD_FILES) {
        messages.push(`一度に追加できるのは${MAX_UPLOAD_FILES}件までです。`);
        break;
      }
      next.push({ key: `${file.name}-${file.size}-${next.length}`, name: file.name, size: file.size });
    }
    if (rejected.length > 0) {
      messages.push(`対応していない形式のため、追加しませんでした: ${rejected.join('、')}（対応: ${ACCEPTED_EXTENSIONS.join(' / ')}）`);
    }
    setFiles(next);
    setProblems(messages);
  };

  const onPick = (event: ChangeEvent<HTMLInputElement>) => {
    if (event.target.files) take(event.target.files);
    event.target.value = '';
  };
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    take(event.dataTransfer.files);
  };

  const submit = () => {
    if (files.length === 0) return;
    dispatch({ type: 'addFiles', files: files.map(({ name, size }) => ({ name, size })), origin, now: Date.now() });
  };

  return (
    <Modal title="資料を追加" description="ファイルを選び、追加を押すと、資料に保存して取り込みを始めます（モック）。" width="wide" onClose={onClose}>
      <div className="mk-modal-body">
        <div
          className="mk-drop"
          data-active={dragging ? 'true' : undefined}
          onDragEnter={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <p className="mk-drop-title">ここにファイルをドラッグ＆ドロップ</p>
          <p className="mk-hint">または</p>
          <button type="button" className="btn btn-secondary" onClick={() => inputRef.current?.click()}>
            ファイルを選択
          </button>
          <input ref={inputRef} type="file" multiple accept={ACCEPT_ATTRIBUTE} className="visually-hidden" tabIndex={-1} aria-hidden="true" data-testid="mk-file-input" onChange={onPick} />
          <p className="mk-hint">対応: {ACCEPTED_EXTENSIONS.join(' / ')}（一度に{MAX_UPLOAD_FILES}件まで）</p>
        </div>

        {problems.length > 0 && (
          <ul className="mk-problems" role="alert">
            {problems.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        )}

        {files.length > 0 && (
          <section aria-labelledby="mk-picked-title">
            <h3 id="mk-picked-title" className="mk-section-title">
              選んだファイル（{files.length}件）
            </h3>
            <ul className="mk-picked">
              {files.map((file) => (
                <li key={file.key}>
                  <span className="mk-picked-name">{file.name}</span>
                  <span className="mk-picked-meta">
                    {kindOf(file.name)} ・ {formatSize(file.size)}
                  </span>
                  <button
                    type="button"
                    className="mk-icon-btn"
                    aria-label={`「${file.name}」を外す`}
                    onClick={() => setFiles(files.filter((f) => f.key !== file.key))}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}

        <p id={noteId} className="mk-note">
          追加した資料は「資料」に保存され、ほかの会話でも使えます。会話だけの一時的な添付はありません。
          {origin === 'chat' && 'この会話の質問対象にも入ります（取り込みが終わるまでは質問に使えません）。'}
          <br />
          モック: ファイルの内容は読み取らず、外部へ送信しません。ファイル名に「エラー」を含むと、取り込みの失敗を試せます。
        </p>
      </div>
      <footer className="mk-modal-foot">
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          キャンセル
        </button>
        <button type="button" className="btn btn-primary" disabled={files.length === 0} aria-describedby={noteId} onClick={submit}>
          {files.length > 0 ? `${files.length}件を追加` : '追加'}
        </button>
      </footer>
    </Modal>
  );
}
