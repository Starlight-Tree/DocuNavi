import { useEffect, useRef, useState } from 'react';
import type { FocusEvent, TouchEvent } from 'react';
import type { DocumentSummary } from '../api/documents';
import { useHeroImage } from '../hooks/useDocuments';
import { useMediaQuery } from '../hooks/useMediaQuery';

/** 1 枚を静止して見せる時間。この後、横にスライドして次へ移る。 */
export const AUTO_ADVANCE_MS = 7000;

const REDUCED_MOTION = '(prefers-reduced-motion: reduce)';
const MOBILE = '(max-width: 40rem)';
/** 横にこれ以上動かして離したら、スワイプとみなす（px）。 */
const SWIPE_MIN_PX = 40;

const DAY_FORMAT = new Intl.DateTimeFormat('ja-JP', {
  timeZone: 'Asia/Tokyo',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

interface HeroCarouselProps {
  /** 更新が新しい順の資料（選ぶのは呼び出し側。`recentDocuments`）。空なら何も出さない。 */
  documents: DocumentSummary[];
  /** 種別（資料グループ）の表示名。 */
  collectionLabel: (collection: string) => string;
  onOpen: (document: DocumentSummary) => void;
}

function Chevron({ direction }: { direction: 'prev' | 'next' }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={direction === 'prev' ? 'M15 5l-7 7 7 7' : 'M9 5l7 7-7 7'} />
    </svg>
  );
}

function PauseIcon({ playing }: { playing: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      {playing ? (
        <>
          <rect x="6" y="5" width="4" height="14" rx="1" />
          <rect x="14" y="5" width="4" height="14" rx="1" />
        </>
      ) : (
        <path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5z" />
      )}
    </svg>
  );
}

/** 画像がない・取れない・読めない間の灰色の代替表示。 */
function MediaPlaceholder() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
    </svg>
  );
}

/**
 * 承認済みの画像がある資料の画像。取れない・読めないときは、灰色の代替表示のまま（失敗は画面に出さない）。
 * 画像がある資料（`heroImage`）にだけ使う（画像がない資料は、取りに行かない）。
 */
function HeroImage({ documentId }: { documentId: number }) {
  const url = useHeroImage(documentId);
  const [broken, setBroken] = useState(false);
  if (!url || broken) return <MediaPlaceholder />;
  return <img className="hero-image" src={url} alt="" onError={() => setBroken(true)} />;
}

/**
 * 資料一覧の一番上の「最近更新された資料」。1 枚ずつ大きく見せるカルーセル（左に資料情報、右に画像）。
 *
 * 自動で切り替えるのは、動きを減らす設定でも、狭い画面（モバイル）でもないとき。
 * 約 7 秒静止して、横にスライドする。次のどれかの間は止まる:
 * 一時停止ボタンを押した後、ホバー中、キーボードフォーカス中。矢印・スワイプの手動操作をした後は、自動では再開しない
 * （再開は、利用者が再生ボタンを押したときだけ）。
 */
export function HeroCarousel({ documents, collectionLabel, onOpen }: HeroCarouselProps) {
  const count = documents.length;
  const reducedMotion = useMediaQuery(REDUCED_MOTION);
  const mobile = useMediaQuery(MOBILE);
  const autoAllowed = !reducedMotion && !mobile;

  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [hovering, setHovering] = useState(false);
  const [focusWithin, setFocusWithin] = useState(false);
  const touchStart = useRef<{ x: number; y: number } | null>(null);

  // 資料が減って位置が範囲の外になったときは、最後の 1 枚に寄せる
  const current = Math.min(index, Math.max(count - 1, 0));
  const running = autoAllowed && playing && !hovering && !focusWithin && count > 1;

  useEffect(() => {
    if (!running) return;
    const timer = window.setTimeout(() => setIndex((current + 1) % count), AUTO_ADVANCE_MS);
    return () => window.clearTimeout(timer);
  }, [running, current, count]);

  if (count === 0) return null;

  // 手動で動かしたら、自動は止める（再開は再生ボタン）
  const move = (delta: 1 | -1) => {
    setPlaying(false);
    setIndex((current + delta + count) % count);
  };

  const onTouchStart = (event: TouchEvent) => {
    const touch = event.touches[0];
    touchStart.current = touch ? { x: touch.clientX, y: touch.clientY } : null;
  };
  const onTouchEnd = (event: TouchEvent) => {
    const start = touchStart.current;
    const touch = event.changedTouches[0];
    touchStart.current = null;
    if (!start || !touch || count < 2) return;
    const dx = touch.clientX - start.x;
    const dy = touch.clientY - start.y;
    if (Math.abs(dx) >= SWIPE_MIN_PX && Math.abs(dx) > Math.abs(dy)) move(dx < 0 ? 1 : -1);
  };
  const onBlur = (event: FocusEvent<HTMLElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget)) setFocusWithin(false);
  };

  return (
    <section
      className="hero-carousel"
      aria-roledescription="カルーセル"
      aria-label="最近更新された資料"
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
      onFocus={() => setFocusWithin(true)}
      onBlur={onBlur}
    >
      <h2 className="hero-carousel-title">最近更新された資料</h2>
      <div className="hero-frame">
        <div className="hero-viewport" onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
          {/* 自動で動いている間は読み上げない（頻繁に割り込むため）。止まっているときだけ、切り替わりを伝える */}
          <div className="hero-track" style={{ transform: `translateX(-${current * 100}%)` }} aria-live={running ? 'off' : 'polite'}>
            {documents.map((document, position) => {
              const name = document.title ?? document.source;
              const updated = document.updatedAt ? DAY_FORMAT.format(new Date(document.updatedAt)) : null;
              const shown = position === current;
              return (
                <div
                  key={document.id}
                  className="hero-slide"
                  role="group"
                  aria-roledescription="スライド"
                  aria-label={`${position + 1} / ${count}`}
                  aria-hidden={!shown}
                  inert={!shown}
                >
                  <div className="hero-info">
                    <div className="hero-meta">
                      {document.collection && <span className="hero-kind">{collectionLabel(document.collection)}</span>}
                      {updated && <span className="hero-date">更新 {updated}</span>}
                    </div>
                    <p className="hero-title">{name}</p>
                    <button
                      type="button"
                      className="btn btn-primary hero-open"
                      aria-label={`資料を開く：${name}`}
                      onClick={() => onOpen(document)}
                    >
                      資料を開く →
                    </button>
                  </div>
                  <div className="hero-media" aria-hidden="true">
                    {document.heroImage ? <HeroImage documentId={document.id} /> : <MediaPlaceholder />}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
        {count > 1 && (
          <div className="hero-controls">
            <button type="button" className="hero-control" aria-label="前の資料へ" onClick={() => move(-1)}>
              <Chevron direction="prev" />
            </button>
            <button type="button" className="hero-control" aria-label="次の資料へ" onClick={() => move(1)}>
              <Chevron direction="next" />
            </button>
            {autoAllowed && (
              <button
                type="button"
                className="hero-control"
                aria-label={playing ? '自動切り替えを停止' : '自動切り替えを再開'}
                onClick={() => setPlaying((value) => !value)}
              >
                <PauseIcon playing={playing} />
              </button>
            )}
            <span className="hero-dots" aria-hidden="true">
              {documents.map((document, dot) => (
                <span key={document.id} className={dot === current ? 'hero-dot is-current' : 'hero-dot'} />
              ))}
            </span>
            <span className="hero-position">
              {current + 1} / {count}
            </span>
          </div>
        )}
      </div>
    </section>
  );
}
