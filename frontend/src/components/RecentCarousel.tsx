import { useCallback, useEffect, useRef, useState } from 'react';
import type { DocumentSummary } from '../api/documents';
import { visibleRange } from '../lib/recentDocuments';

/** 画面に並べる枚数の目安（初期表示。実際の枚数は幅で決まり、スクロールのたびに測り直す）。 */
const INITIAL_VISIBLE = 3;

const DAY_FORMAT = new Intl.DateTimeFormat('ja-JP', {
  timeZone: 'Asia/Tokyo',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

interface RecentCarouselProps {
  /** 更新が新しい順の資料（選ぶのは呼び出し側。`recentDocuments`）。空なら何も出さない。 */
  documents: DocumentSummary[];
  /** 種別（資料グループ）の表示名。 */
  collectionLabel: (collection: string) => string;
  onOpen: (document: DocumentSummary) => void;
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * 資料一覧の一番上の「最近更新された資料」。手動のカルーセル（自動再生なし）。
 * 横スクロール（スナップ付き）なので、スワイプ・トラックパッド・矢印ボタンで動く。カードを押すと資料を開く。
 */
export function RecentCarousel({ documents, collectionLabel, onOpen }: RecentCarouselProps) {
  const trackRef = useRef<HTMLUListElement>(null);
  const count = documents.length;
  const [range, setRange] = useState({ first: 0, last: Math.min(count, INITIAL_VISIBLE) - 1 });

  // スクロール位置から、見えているカードの範囲を測り直す（レイアウトが無い環境では測れず、いまの値のまま）
  const measure = useCallback(() => {
    const track = trackRef.current;
    if (!track) return;
    const cards = Array.from(track.children, (child) => ({
      left: (child as HTMLElement).offsetLeft, // 枠（position: relative）の左端からの距離
      width: (child as HTMLElement).offsetWidth,
    }));
    const next = visibleRange(cards, track.scrollLeft, track.clientWidth);
    if (next) setRange((prev) => (prev.first === next.first && prev.last === next.last ? prev : next));
  }, []);

  useEffect(() => {
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [measure, count]);

  if (count === 0) return null;

  const last = Math.min(range.last, count - 1);
  const atStart = range.first <= 0;
  const atEnd = last >= count - 1;

  // 1 枚ずつ送る。スナップ付きなので、目的のカードを左端に合わせる（動きを減らす設定では、すぐ移る）
  const move = (delta: 1 | -1) => {
    const target = Math.min(count - 1, Math.max(0, range.first + delta));
    const card = trackRef.current?.children[target];
    if (!card) return;
    const width = last - range.first;
    setRange({ first: target, last: Math.min(count - 1, target + width) });
    card.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'nearest', inline: 'start' });
  };

  return (
    <section className="recent-carousel" aria-label="最近更新された資料">
      <div className="recent-carousel-head">
        <h2 className="recent-carousel-title">最近更新された資料</h2>
        <span className="recent-carousel-range" aria-live="polite">
          {range.first + 1}–{last + 1} / {count}
        </span>
        <button
          type="button"
          className="recent-carousel-arrow"
          aria-label="前の資料へ"
          disabled={atStart}
          onClick={() => move(-1)}
        >
          ‹
        </button>
        <button
          type="button"
          className="recent-carousel-arrow"
          aria-label="次の資料へ"
          disabled={atEnd}
          onClick={() => move(1)}
        >
          ›
        </button>
      </div>
      <ul className="recent-carousel-track" ref={trackRef} onScroll={measure}>
        {documents.map((document) => {
          const name = document.title ?? document.source;
          const updated = document.updatedAt ? DAY_FORMAT.format(new Date(document.updatedAt)) : null;
          return (
            <li key={document.id} className="recent-carousel-item">
              <button
                type="button"
                className="recent-carousel-card"
                aria-label={`${name} を開く（最近更新）`}
                onClick={() => onOpen(document)}
              >
                <span className="recent-carousel-name">{name}</span>
                <span className="recent-carousel-meta">
                  {document.collection && <span className="recent-carousel-kind">{collectionLabel(document.collection)}</span>}
                  {updated && <span className="recent-carousel-date">更新 {updated}</span>}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
