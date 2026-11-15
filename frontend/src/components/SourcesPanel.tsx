import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
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

/** 一覧の項目に出す抜粋の最大の文字数（読み上げの説明にも使う）。全文は右のパネルで読む。 */
const EXCERPT_LENGTH = 240;


/** 一覧と画面の上下の端との間（px）。 */
const PANEL_MARGIN = 8;

function excerptText(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > EXCERPT_LENGTH ? `${flat.slice(0, EXCERPT_LENGTH)}…` : flat;
}

/**
 * 回答の根拠にした資料の「N 件のソース」ボタン。回答の下の操作の行（コピーの隣）に置く。押すと一覧（資料名 + ページ + 根拠の抜粋）のポップオーバーが開き、
 * 一覧は、回答の欄の右端に揃えた縦長のパネルで開く（上下はボタンの高さの近く）。項目を押すと、右のパネルで該当箇所を開く（一覧は閉じる）。外を押す・Esc で閉じる。
 * 回答の文末の引用番号（本文の上付きの番号。`AssistantAnswer`）は、ここのカードの番号と同じ（出典の並びの n 番目）。
 * 文との対応は、LLM が付けた番号を信用せず、存在する番号だけを使う（`lib/citations.ts`）。カードは回答全体の根拠の一覧で、番号の有無にかかわらず出す。
 * 出典の見出しが無い context（未検索・該当なしの定型文）では何も出さない。
 */
export function SourcesPanel({ sources, reference }: SourcesPanelProps) {
  const { opening, failureMessage, open } = reference;
  const [isOpen, setIsOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const base = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLElement>(null);
  // 一覧の位置（画面の座標）。開いている間だけ測る
  const [place, setPlace] = useState<{ top: number; right: number } | null>(null);
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

  // 開いている間、一覧を「回答の欄の右端」に揃え、上下は「N 件のソース」のボタンの高さを中心に置く（画面の上下の端からは出さない）。
  // 会話をスクロールしたとき・窓の大きさが変わったとき・項目が増えたときは、置き直す
  useLayoutEffect(() => {
    if (!isOpen) {
      setPlace(null);
      return;
    }
    const trigger = triggerRef.current;
    const pop = popRef.current;
    if (!trigger || !pop) return;
    const column = trigger.closest('.message') ?? trigger;
    const list = trigger.closest('.message-list');
    const measure = () => {
      const button = trigger.getBoundingClientRect();
      const height = pop.offsetHeight;
      const top = button.top + button.height / 2 - height / 2;
      const viewport = document.documentElement.clientHeight;
      setPlace({
        top: Math.min(Math.max(top, PANEL_MARGIN), Math.max(viewport - height - PANEL_MARGIN, PANEL_MARGIN)),
        right: Math.max(document.documentElement.clientWidth - column.getBoundingClientRect().right, PANEL_MARGIN),
      });
    };
    measure();
    window.addEventListener('resize', measure);
    list?.addEventListener('scroll', measure);
    return () => {
      window.removeEventListener('resize', measure);
      list?.removeEventListener('scroll', measure);
    };
  }, [isOpen, expanded]);

  if (items.length === 0) return null;
  const shown = expanded ? items : items.slice(0, REFERENCE_LIST_LIMIT);
  const hidden = items.length - shown.length;

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
          {items.length} 件のソース
          <svg className="refs-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d={isOpen ? 'M18 15l-6-6-6 6' : 'M6 9l6 6 6-6'} />
          </svg>
        </button>
        {isOpen && (
          <section
            id={popoverId}
            ref={popRef}
            className="refs-pop"
            aria-label="参照資料"
            style={place ? { top: place.top, right: place.right } : { visibility: 'hidden' }}
          >
            <div className="refs-pop-head">
              <h3 className="refs-pop-title">ソース</h3>
              <button
                type="button"
                className="refs-pop-close"
                aria-label="ソースの一覧を閉じる"
                onClick={() => {
                  setIsOpen(false);
                  triggerRef.current?.focus();
                }}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M6 9l6 6 6-6" />
                </svg>
              </button>
            </div>
            <ul className="refs-list">
              {shown.map(({ source, index }) => {
                const pages = formatPages(source.page, source.pageEnd, source.pageLabel, source.pageEndLabel);
                const excerptId = `${base}-excerpt-${index}`;
                const excerpt = excerptText(source.text);
                return (
                  <li key={`${index}-${source.name}`} className="ref-item">
                    <button
                      type="button"
                      className="ref-link"
                      aria-label={`${sourceLabel(source)}${pages ? ` ${pages}` : ''}を右のパネルで開く`}
                      aria-describedby={excerpt ? excerptId : undefined}
                      disabled={opening !== null}
                      onClick={() => {
                        // ポップオーバーを閉じても、資料のパネルを閉じたときの戻り先を失わないよう、ボタンを渡す
                        void open(index, null, triggerRef.current);
                        setIsOpen(false);
                      }}
                    >
                      <span className="ref-num" aria-hidden="true">
                        {index + 1}
                      </span>
                      <span className="ref-body">
                        <span className="ref-head">
                          <span className="ref-name">{sourceLabel(source)}</span>
                          {pages && <span className="ref-page">{pages}</span>}
                          <span className="ref-open" aria-hidden="true">
                            {opening === index ? '…' : '↗'}
                          </span>
                        </span>
                        {excerpt && (
                          <span id={excerptId} className="ref-excerpt">
                            {excerpt}
                          </span>
                        )}
                      </span>
                    </button>
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
