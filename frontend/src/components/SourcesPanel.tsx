import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { useOpenReference } from '../hooks/useOpenReference';
import { formatPages, referenceItems, REFERENCE_LIST_LIMIT, sourceLabel } from '../lib/sources';
import type { SourceDoc } from '../lib/sources';

interface SourcesPanelProps {
  /** 出典の全件（`withPages` でページ・資料の ID を付けたもの。並びは、回答の文末の引用番号と同じ）。 */
  sources: SourceDoc[];
  /** 資料のパネルを開く操作と、その状態（回答の本文の引用番号と共有する）。 */
  reference: ReturnType<typeof useOpenReference>;
}

/** 抜粋のヒントに出す最大の文字数（読み上げにも使うので、短くする）。全文は右のパネルで読む。 */
const EXCERPT_HINT_LENGTH = 140;

function excerptHint(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > EXCERPT_HINT_LENGTH ? `${flat.slice(0, EXCERPT_HINT_LENGTH)}…` : flat;
}

/**
 * 回答の根拠にした資料の「参照資料 N 件」ボタン。回答の下の操作の行（コピーの隣）に置く。押すと一覧（資料名 + ページ）のポップオーバーが開き、
 * 一覧の項目を押すと、右のパネルで該当箇所を開く（ポップオーバーは閉じる）。外を押す・Esc で閉じる（Esc は、抜粋のヒントがあればそれが先）。
 * 回答の文末の引用番号（本文の上付きの番号。`AssistantAnswer`）は、ここのカードの番号と同じ（出典の並びの n 番目）。
 * 文との対応は、LLM が付けた番号を信用せず、存在する番号だけを使う（`lib/citations.ts`）。カードは回答全体の根拠の一覧で、番号の有無にかかわらず出す。
 * 抜粋のヒントは、マウスを載せたときと、キーボードでフォーカスしたときに出る（Esc で閉じる）。
 * 出典の見出しが無い context（未検索・該当なしの定型文）では何も出さない。
 */
export function SourcesPanel({ sources, reference }: SourcesPanelProps) {
  const { opening, failureMessage, open } = reference;
  const [isOpen, setIsOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  // Esc で閉じたヒント（出典の添字）。フォーカスやマウスが外れたら、また出せるようにする
  const [dismissed, setDismissed] = useState<number | null>(null);
  const base = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const items = referenceItems(sources);

  // ポップオーバーの外を押したら閉じる
  useEffect(() => {
    if (!isOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setIsOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [isOpen]);

  if (items.length === 0) return null;
  const shown = expanded ? items : items.slice(0, REFERENCE_LIST_LIMIT);
  const hidden = items.length - shown.length;

  const handleItemKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key === 'Escape' && dismissed !== index) {
      // ポップオーバーや入力欄の Esc には渡さない（ヒントを閉じるだけ）
      event.stopPropagation();
      setDismissed(index);
    }
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' && isOpen) {
      // 資料のパネルや入力欄の Esc には渡さない（ポップオーバーを閉じて、ボタンへ戻る）
      event.stopPropagation();
      setIsOpen(false);
      triggerRef.current?.focus();
    }
  };

  const popoverId = `${base}-pop`;

  return (
    <>
      <div className="refs" ref={wrapRef} onKeyDown={handleKeyDown}>
        <button
          ref={triggerRef}
          type="button"
          className="refs-toggle"
          aria-expanded={isOpen}
          aria-controls={isOpen ? popoverId : undefined}
          onClick={() => setIsOpen((current) => !current)}
        >
          <svg className="ref-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
            <path d="M14 3v5h5" />
          </svg>
          参照資料 {items.length} 件
          <svg className="refs-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d={isOpen ? 'M18 15l-6-6-6 6' : 'M6 9l6 6 6-6'} />
          </svg>
        </button>
        {isOpen && (
          <section id={popoverId} className="refs-pop" aria-label="参照資料">
            <ul className="refs-list">
              {shown.map(({ source, index }) => {
                const pages = formatPages(source.page, source.pageEnd, source.pageLabel, source.pageEndLabel);
                const tipId = `${base}-tip-${index}`;
                const hint = excerptHint(source.text);
                return (
                  <li key={`${index}-${source.name}`} className="ref-item" data-dismissed={dismissed === index ? 'true' : undefined}>
                    <button
                      type="button"
                      className="ref-link"
                      aria-label={`${sourceLabel(source)}${pages ? ` ${pages}` : ''}を右のパネルで開く`}
                      aria-describedby={hint ? tipId : undefined}
                      disabled={opening !== null}
                      onClick={() => {
                        // ポップオーバーを閉じても、資料のパネルを閉じたときの戻り先を失わないよう、ボタンを渡す
                        void open(index, null, triggerRef.current);
                        setIsOpen(false);
                      }}
                      onKeyDown={(event) => handleItemKeyDown(event, index)}
                      onBlur={() => setDismissed((current) => (current === index ? null : current))}
                      onMouseLeave={() => setDismissed((current) => (current === index ? null : current))}
                    >
                      <span className="ref-num" aria-hidden="true">
                        {index + 1}
                      </span>
                      <span className="ref-name">{sourceLabel(source)}</span>
                      {pages && <span className="ref-page">{pages}</span>}
                      <span className="ref-open" aria-hidden="true">
                        {opening === index ? '…' : '↗'}
                      </span>
                    </button>
                    {hint && (
                      <span id={tipId} role="tooltip" className="ref-tip">
                        {hint}
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
            {hidden > 0 && (
              <button type="button" className="btn-link refs-more" aria-expanded={false} onClick={() => setExpanded(true)}>
                ほか {hidden} 件を表示
              </button>
            )}
            {expanded && items.length > REFERENCE_LIST_LIMIT && (
              <button type="button" className="btn-link refs-more" aria-expanded={true} onClick={() => setExpanded(false)}>
                折りたたむ
              </button>
            )}
          </section>
        )}
      </div>
      {failureMessage && (
        <p className="source-failure" role="alert">
          {failureMessage}
        </p>
      )}
    </>
  );
}
