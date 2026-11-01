import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DocumentSummary } from '../api/documents';
import { useHeroImage } from '../hooks/useDocuments';
import { AUTO_ADVANCE_MS, HeroCarousel } from './HeroCarousel';

vi.mock('../hooks/useDocuments', () => ({ useHeroImage: vi.fn() }));
const heroImage = vi.mocked(useHeroImage);

function doc(id: number, extra: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id,
    source: `doc${id}.pdf`,
    title: null,
    collection: 'guides',
    status: 'ready',
    failureReason: null,
    pages: 1,
    updatedAt: `2026-10-0${id}T03:00:00+00:00`,
    chunkCount: 1,
    embeddedCount: 1,
    heroImage: false,
    ...extra,
  };
}

const DOCS = [doc(5, { title: 'B 店の案内', collection: 'cafe' }), doc(4), doc(3), doc(2), doc(1)];

/** `matchMedia` の差し替え。合わせたいクエリの断片（'reduce' `max-width`）を渡す。 */
function stubMedia(matching: string[]) {
  window.matchMedia = vi.fn((query: string) => ({
    matches: matching.some((part) => query.includes(part)),
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

function renderCarousel(documents: DocumentSummary[] = DOCS, onOpen = vi.fn()) {
  render(<HeroCarousel documents={documents} collectionLabel={(name) => `種別:${name}`} onOpen={onOpen} />);
  return { onOpen, region: screen.getByRole('region', { name: '最近更新された資料' }) };
}

const position = (region: HTMLElement) => within(region).getByText(/^\d+ \/ \d+$/).textContent;
const advance = (ms: number) => act(() => void vi.advanceTimersByTime(ms));

beforeEach(() => {
  vi.useFakeTimers();
  stubMedia([]);
  heroImage.mockReset();
  heroImage.mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  Reflect.deleteProperty(window, 'matchMedia');
});

describe('HeroCarousel', () => {
  it('見えている 1 枚に、資料名・種別・更新日・「資料を開く」を出す。位置は「1 / 5」', () => {
    const { region } = renderCarousel();
    const slide = within(region).getByRole('group', { name: '1 / 5' });

    expect(slide).toHaveTextContent('B 店の案内');
    expect(slide).toHaveTextContent('種別:cafe');
    expect(slide).toHaveTextContent('更新 2026/10/05');
    expect(within(slide).getByRole('button', { name: '資料を開く：B 店の案内' })).toHaveTextContent('資料を開く →');
    expect(position(region)).toBe('1 / 5');
    // 見えていないスライドは、読み上げにも Tab にも出ない
    expect(within(region).queryByRole('group', { name: '2 / 5' })).not.toBeInTheDocument();
  });

  it('「資料を開く」で、その資料を渡す', () => {
    const { region, onOpen } = renderCarousel();

    fireEvent.click(within(region).getByRole('button', { name: '資料を開く：B 店の案内' }));

    expect(onOpen).toHaveBeenCalledWith(DOCS[0]);
  });

  it('画像が無くても（全スライドが代替表示）、文字側と操作が動く', () => {
    const { region } = renderCarousel();

    expect(region.querySelector('img')).toBeNull();
    fireEvent.click(within(region).getByRole('button', { name: '次の資料へ' }));
    expect(position(region)).toBe('2 / 5');
    expect(within(region).getByRole('button', { name: '資料を開く：doc4.pdf' })).toBeEnabled();
  });

  it('約 7 秒静止してから、次のスライドへ移る。最後の次は最初に戻る', () => {
    const { region } = renderCarousel();

    advance(AUTO_ADVANCE_MS - 1);
    expect(position(region)).toBe('1 / 5');
    advance(1);
    expect(position(region)).toBe('2 / 5');
    // 次の数え直しは、描き直した後に始まる。1 枚ずつ進める
    for (let i = 0; i < 4; i += 1) advance(AUTO_ADVANCE_MS);
    expect(position(region)).toBe('1 / 5');
  });

  it('一時停止ボタンで止まり、再生ボタンで再開する', () => {
    const { region } = renderCarousel();

    fireEvent.click(within(region).getByRole('button', { name: '自動切り替えを停止' }));
    advance(AUTO_ADVANCE_MS * 3);
    expect(position(region)).toBe('1 / 5');

    fireEvent.click(within(region).getByRole('button', { name: '自動切り替えを再開' }));
    advance(AUTO_ADVANCE_MS);
    expect(position(region)).toBe('2 / 5');
  });

  it('ホバー中は止まり、外れたら数え直す', () => {
    const { region } = renderCarousel();

    fireEvent.mouseEnter(region);
    advance(AUTO_ADVANCE_MS * 2);
    expect(position(region)).toBe('1 / 5');

    fireEvent.mouseLeave(region);
    advance(AUTO_ADVANCE_MS - 1);
    expect(position(region)).toBe('1 / 5');
    advance(1);
    expect(position(region)).toBe('2 / 5');
  });

  it('キーボードフォーカス中は止まり、外れたら数え直す', () => {
    const { region } = renderCarousel();
    const open = within(region).getByRole('button', { name: '資料を開く：B 店の案内' });

    fireEvent.focus(open);
    advance(AUTO_ADVANCE_MS * 2);
    expect(position(region)).toBe('1 / 5');

    fireEvent.blur(open, { relatedTarget: document.body });
    advance(AUTO_ADVANCE_MS);
    expect(position(region)).toBe('2 / 5');
  });

  it('カルーセルの中でフォーカスが移るだけなら、止まったまま', () => {
    const { region } = renderCarousel();
    const open = within(region).getByRole('button', { name: '資料を開く：B 店の案内' });
    const next = within(region).getByRole('button', { name: '次の資料へ' });

    fireEvent.focus(open);
    fireEvent.blur(open, { relatedTarget: next });
    fireEvent.focus(next);
    advance(AUTO_ADVANCE_MS * 2);

    expect(position(region)).toBe('1 / 5');
  });

  it('矢印で手動に動かしたら、その後は自動では再開しない。最初の前は最後に戻る', () => {
    const { region } = renderCarousel();

    fireEvent.click(within(region).getByRole('button', { name: '前の資料へ' }));
    expect(position(region)).toBe('5 / 5');
    fireEvent.mouseEnter(region);
    fireEvent.mouseLeave(region);
    advance(AUTO_ADVANCE_MS * 3);

    expect(position(region)).toBe('5 / 5');
    expect(within(region).getByRole('button', { name: '自動切り替えを再開' })).toBeInTheDocument();
  });

  it('横のスワイプで動かし、縦の動きや短い動きでは動かない。手動操作なので自動は止まる', () => {
    const { region } = renderCarousel();
    const viewport = region.querySelector('.hero-viewport')!;
    const swipe = (from: [number, number], to: [number, number]) => {
      fireEvent.touchStart(viewport, { touches: [{ clientX: from[0], clientY: from[1] }] });
      fireEvent.touchEnd(viewport, { changedTouches: [{ clientX: to[0], clientY: to[1] }] });
    };

    swipe([100, 100], [90, 100]);
    swipe([100, 100], [100, 20]);
    expect(position(region)).toBe('1 / 5');

    swipe([300, 100], [100, 110]);
    expect(position(region)).toBe('2 / 5');
    swipe([100, 100], [300, 100]);
    expect(position(region)).toBe('1 / 5');
    advance(AUTO_ADVANCE_MS * 2);
    expect(position(region)).toBe('1 / 5');
  });

  it('モバイル（狭い画面）では自動で動かさず、一時停止ボタンも出さない。矢印は使える', () => {
    stubMedia(['max-width']);
    const { region } = renderCarousel();

    advance(AUTO_ADVANCE_MS * 3);
    expect(position(region)).toBe('1 / 5');
    expect(within(region).queryByRole('button', { name: /自動切り替え/ })).not.toBeInTheDocument();
    fireEvent.click(within(region).getByRole('button', { name: '次の資料へ' }));
    expect(position(region)).toBe('2 / 5');
  });

  it('動きを減らす設定では自動で動かさず、一時停止ボタンも出さない。矢印は使える', () => {
    stubMedia(['reduce']);
    const { region } = renderCarousel();

    advance(AUTO_ADVANCE_MS * 3);
    expect(position(region)).toBe('1 / 5');
    expect(within(region).queryByRole('button', { name: /自動切り替え/ })).not.toBeInTheDocument();
    fireEvent.click(within(region).getByRole('button', { name: '次の資料へ' }));
    expect(position(region)).toBe('2 / 5');
  });

  it('1 件だけなら、動かす操作と位置表示は出さない', () => {
    const { region } = renderCarousel([DOCS[0]!]);

    expect(within(region).queryByRole('button', { name: '次の資料へ' })).not.toBeInTheDocument();
    expect(within(region).queryByText(/^\d+ \/ \d+$/)).not.toBeInTheDocument();
    advance(AUTO_ADVANCE_MS * 2);
    expect(within(region).getByRole('button', { name: '資料を開く：B 店の案内' })).toBeInTheDocument();
  });

  it('資料が無ければ何も出さない', () => {
    render(<HeroCarousel documents={[]} collectionLabel={(name) => name} onOpen={vi.fn()} />);

    expect(screen.queryByRole('region', { name: '最近更新された資料' })).not.toBeInTheDocument();
  });

  it('止まっている間だけ、切り替わりを読み上げに伝える', () => {
    const { region } = renderCarousel();
    const track = region.querySelector('.hero-track')!;

    expect(track).toHaveAttribute('aria-live', 'off');
    fireEvent.click(within(region).getByRole('button', { name: '自動切り替えを停止' }));
    expect(track).toHaveAttribute('aria-live', 'polite');
  });
});

describe('HeroCarousel の画像', () => {
  const withImage = [doc(5, { heroImage: true }), doc(4), doc(3, { heroImage: true })];

  it('画像がある資料の画像を、枠に出す（飾りなので読み上げない）。画像がない資料は取りに行かない', () => {
    heroImage.mockImplementation((id) => `blob:image-${id}`);
    const { region } = renderCarousel(withImage);

    const images = region.querySelectorAll('img');
    expect([...images].map((image) => image.getAttribute('src'))).toEqual(['blob:image-5', 'blob:image-3']);
    expect(images[0]).toHaveAttribute('alt', '');
    expect(heroImage.mock.calls.map(([id]) => id).sort()).toEqual([3, 5]);
  });

  it('まだ取れていない・取れなかった（null）間は、灰色の代替表示のまま。文字と操作は動く', () => {
    const { region } = renderCarousel(withImage);

    expect(region.querySelector('img')).toBeNull();
    expect(region.querySelector('.hero-media svg')).not.toBeNull();
    expect(within(region).getByRole('button', { name: '資料を開く：doc5.pdf' })).toBeEnabled();
  });

  it('画像が読めなかったとき（壊れた画像）は、灰色の代替表示に戻す', () => {
    heroImage.mockImplementation((id) => `blob:image-${id}`);
    const { region } = renderCarousel(withImage);

    fireEvent.error(region.querySelector('img')!);

    expect(region.querySelectorAll('img')).toHaveLength(1);
  });
});
