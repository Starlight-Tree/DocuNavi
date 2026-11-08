import { useId, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { useOpenReference } from '../hooks/useOpenReference';
import { formatPages, referenceItems, REFERENCE_LIST_LIMIT } from '../lib/sources';
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
 * 回答の根拠にした資料を、回答の下に小さくまとめる「参照資料」。1 件 = 資料名 + ページ。押すと、右のパネルで該当箇所を開く。
 * 回答の文末の引用番号（本文の上付きの番号。`AssistantAnswer`）は、ここのカードの番号と同じ（出典の並びの n 番目）。
 * 文との対応は、LLM が付けた番号を信用せず、存在する番号だけを使う（`lib/citations.ts`）。カードは回答全体の根拠の一覧で、番号の有無にかかわらず出す。
 * 抜粋のヒントは、マウスを載せたときと、キーボードでフォーカスしたときに出る（Esc で閉じる）。
 * 出典の見出しが無い context（未検索・該当なしの定型文）では何も出さない。
 */
export function SourcesPanel({ sources, reference }: SourcesPanelProps) {
  const { opening, failureMessage, open } = reference;
  const [expanded, setExpanded] = useState(false);
  // Esc で閉じたヒント（出典の添字）。フォーカスやマウスが外れたら、また出せるようにする
  const [dismissed, setDismissed] = useState<number | null>(null);
  const base = useId();
  const items = referenceItems(sources);
  if (items.length === 0) return null;
  const shown = expanded ? items : items.slice(0, REFERENCE_LIST_LIMIT);
  const hidden = items.length - shown.length;

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key === 'Escape' && dismissed !== index) {
      // パネルや入力欄の Esc には渡さない（ヒントを閉じるだけ）
      event.stopPropagation();
      setDismissed(index);
    }
  };

  return (
    <section className="refs" aria-label="参照資料">
      <h3 className="refs-title">
        参照資料
        <span className="refs-count">{items.length} 件</span>
      </h3>
      <ul className="refs-list">
        {shown.map(({ source, index }) => {
          const pages = formatPages(source.page, source.pageEnd);
          const tipId = `${base}-tip-${index}`;
          const hint = excerptHint(source.text);
          return (
            <li key={`${index}-${source.name}`} className="ref-item" data-dismissed={dismissed === index ? 'true' : undefined}>
              <button
                type="button"
                className="ref-link"
                aria-label={`${source.name}${pages ? ` ${pages}` : ''}を右のパネルで開く`}
                aria-describedby={hint ? tipId : undefined}
                disabled={opening !== null}
                onClick={() => void open(index)}
                onKeyDown={(event) => handleKeyDown(event, index)}
                onBlur={() => setDismissed((current) => (current === index ? null : current))}
                onMouseLeave={() => setDismissed((current) => (current === index ? null : current))}
              >
                <span className="ref-num" aria-hidden="true">
                  {index + 1}
                </span>
                <svg className="ref-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
                  <path d="M14 3v5h5" />
                </svg>
                <span className="ref-name">{source.name}</span>
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
      {failureMessage && (
        <p className="source-failure" role="alert">
          {failureMessage}
        </p>
      )}
    </section>
  );
}
