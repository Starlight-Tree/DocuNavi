import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { StatusBadge } from './StatusBadge';
import { formatDateTime, formatSize, passagesFor } from './data';
import { useMock } from './MockProvider';
import { docById } from './store';
import type { PanelTarget } from './types';

export const PANEL_ID = 'mk-panel';

/**
 * 右サイドの共通パネル。資料の詳細（一覧から開く）と、回答の出典（根拠の箇所を強調）を、同じ部品で出す。
 * `target.focus` があれば出典の表示。原文はサンプルテキスト（実ファイルは読まない）。
 */
export function DocumentPanel({ target }: { target: PanelTarget }) {
  const { state, dispatch } = useMock();
  const doc = docById(state, target.docId);
  const focus = target.focus;
  const pages = doc ? passagesFor(doc) : [];
  const [page, setPage] = useState(focus?.page ?? 1);
  const bodyRef = useRef<HTMLDivElement>(null);
  const current = pages.find((p) => p.page === page) ?? pages[0];

  // 根拠の箇所が見える位置までスクロールする
  useEffect(() => {
    bodyRef.current?.querySelector<HTMLElement>('[data-focus="true"]')?.scrollIntoView?.({ block: 'center' });
  }, [page, target]);

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    dispatch({ type: 'closePanel' });
  };

  const name = doc?.name ?? target.docName ?? '資料';
  const eyebrow = focus ? '回答の出典' : '資料の詳細';

  return (
    <aside id={PANEL_ID} className="mk-panel" aria-label={`${eyebrow}: ${name}`} tabIndex={-1} onKeyDown={handleKeyDown}>
      <header className="mk-panel-head">
        <span className="mk-eyebrow">{eyebrow}</span>
        <button type="button" className="mk-icon-btn" aria-label="パネルを閉じる" onClick={() => dispatch({ type: 'closePanel' })}>
          ×
        </button>
      </header>
      <h2 className="mk-panel-title">{name}</h2>

      {!doc ? (
        <p className="mk-note" role="status">
          この資料は削除されました。回答の出典の名前だけが残っています。
        </p>
      ) : (
        <>
          <dl className="mk-meta">
            <div>
              <dt>ファイル種別</dt>
              <dd>{doc.kind}</dd>
            </div>
            <div>
              <dt>状態</dt>
              <dd>
                <StatusBadge status={doc.status} />
              </dd>
            </div>
            <div>
              <dt>追加日時</dt>
              <dd>{formatDateTime(doc.addedAt)}</dd>
            </div>
            <div>
              <dt>サイズ</dt>
              <dd>{formatSize(doc.size)}</dd>
            </div>
          </dl>
          <div className="mk-panel-actions">
            <button
              type="button"
              className="btn btn-primary"
              disabled={doc.status !== 'ready'}
              onClick={() => dispatch({ type: 'askWithDoc', docId: doc.id, now: Date.now() })}
            >
              この資料で質問
            </button>
            {doc.status === 'failed' && (
              <button type="button" className="btn btn-secondary" onClick={() => dispatch({ type: 'retry', docId: doc.id, now: Date.now() })}>
                再試行
              </button>
            )}
          </div>
          {doc.status !== 'ready' && (
            <p className="mk-note" role="status">
              {doc.status === 'processing'
                ? '取り込み中です。完了すると、質問対象に選べて、原文を見られます。'
                : (doc.failureReason ?? '取り込みに失敗しました。')}
            </p>
          )}

          {doc.status === 'ready' && current && (
            <section className="mk-preview" aria-labelledby="mk-preview-title">
              <div className="mk-preview-head">
                <h3 id="mk-preview-title" className="mk-section-title">
                  原文プレビュー
                </h3>
                <div className="mk-pager">
                  <button type="button" className="mk-icon-btn" aria-label="前のページ" disabled={current.page <= 1} onClick={() => setPage(current.page - 1)}>
                    ‹
                  </button>
                  <span aria-live="polite">
                    p.{current.page} / {pages.length}
                  </span>
                  <button
                    type="button"
                    className="mk-icon-btn"
                    aria-label="次のページ"
                    disabled={current.page >= pages.length}
                    onClick={() => setPage(current.page + 1)}
                  >
                    ›
                  </button>
                </div>
              </div>
              {focus && (
                <p className="mk-cite-line">
                  <span className="mk-cite-num" aria-hidden="true">
                    {target.citation ?? '•'}
                  </span>
                  出典: {focus.location}
                </p>
              )}
              <div ref={bodyRef} className="mk-preview-body" tabIndex={0} aria-label="原文（サンプルテキスト）">
                <h4 className="mk-preview-heading">{current.heading}</h4>
                {current.paragraphs.map((text, i) => {
                  const hit = focus !== undefined && focus.page === current.page && focus.para === i;
                  return hit ? (
                    <p key={i} className="mk-para" data-focus="true">
                      <mark>
                        <span className="mk-mark-label">根拠の箇所: </span>
                        {text}
                      </mark>
                    </p>
                  ) : (
                    <p key={i} className="mk-para">
                      {text}
                    </p>
                  );
                })}
              </div>
              <p className="mk-hint">原文はサンプルテキストです（実際のファイルの内容ではありません）。</p>
            </section>
          )}
        </>
      )}
    </aside>
  );
}
