import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { MSG_PDF_LOAD_FAILED, MSG_PDF_RENDER_FAILED } from '../api/documents';
import { PDF_MARK_CLASS } from '../lib/pdfHighlight';
import { PAGE_GAP, ZOOM_MAX, fitScale } from '../lib/pdfView';
import { STAGE_HEIGHT, STAGE_WIDTH, fakePdf } from '../test/fakePdf';
import { atPdfPage, pageBox, pdfStage, scrollPdfTo } from '../test/pdfPage';
import { MSG_PDF_CITE_MISS, PdfViewer } from './PdfViewer';

// jsdom には canvas・Worker・レイアウトが無いので、PDF.js との境界（api/pdfjs.ts）と、ページの位置・大きさは差し替えてある（test/fakePdf.ts。
// ページは縦に並び、枠の高さと間隔から位置を返す）。ここで確かめるのは、ツールバーの操作・ページへの移動・スクロールに伴うページ番号と描く範囲・
// 根拠の語句の強調・失敗の表示。実際の描画とスクロールの感触は、実ブラウザーで確かめる。

const URL_PATH = '/api/documents/7/file?name=guide.pdf';

function view(props: Partial<Parameters<typeof PdfViewer>[0]> = {}) {
  return (
    <PdfViewer title="お手入れガイド" url={URL_PATH} saveName="お手入れガイド.pdf" page={null} cite={0} span={null} passage={null} {...props} />
  );
}

const marks = () => Array.from(document.querySelectorAll(`mark.${PDF_MARK_CLASS}`));
const slot = (n: number) => document.querySelector<HTMLElement>(`.pdf-page[data-page="${n}"]`)!;
const hasCanvas = (n: number) => slot(n).querySelector('canvas') !== null;
const renderedPages = () => [...new Set(fakePdf.renders.map((r) => r.page))].sort((a, b) => a - b);

/** 倍率 1 のとき（幅に合わせる）の、ページの枠の高さ。 */
const FIT = fitScale(STAGE_WIDTH, 612)!;
const PAGE_H = Math.floor(792 * FIT);
/** n ページ目の先頭の位置（表示領域の先頭から）。そこへ飛ぶときのスクロール位置は、上の余白の分だけ手前。 */
const topOf = (n: number) => PAGE_GAP + (n - 1) * (PAGE_H + PAGE_GAP);
const jumpTop = (n: number) => topOf(n) - PAGE_GAP;

const manyPages = (count: number) => Array.from({ length: count }, (_, i) => [`${i + 1} ページ目の本文です。`]);

describe('PdfViewer: 表示とページの移動', () => {
  it('読み込み中を示し、開けたら全ページの枠を縦に並べる。描くのは、先頭のページと、その次だけ', async () => {
    render(view());

    expect(screen.getByRole('status')).toHaveTextContent('読み込み中…');
    await atPdfPage(1);
    expect(screen.getByRole('group', { name: 'お手入れガイド の PDF' })).toBeInTheDocument();
    expect(fakePdf.opened).toEqual([URL_PATH]);
    expect(document.querySelectorAll('.pdf-page')).toHaveLength(3);
    await waitFor(() => expect(hasCanvas(1) && hasCanvas(2)).toBe(true));
    expect(slot(1).querySelector('.pdf-text-layer')).not.toBeNull();
    expect(hasCanvas(3)).toBe(false); // 先読みは前後 1 ページ。3 ページ目は、近づくまで描かない
    expect(screen.queryByText('読み込み中…')).not.toBeInTheDocument();
  });

  it('前へ・次へで、そのページの先頭へ飛ぶ。先頭では「前」、末尾では「次」を押せない', async () => {
    const user = userEvent.setup();
    render(view());
    await atPdfPage(1);
    expect(screen.getByRole('button', { name: '前のページ' })).toBeDisabled();

    await user.click(screen.getByRole('button', { name: '次のページ' }));
    await atPdfPage(2);
    expect(pdfStage().scrollTop).toBe(jumpTop(2));
    await waitFor(() => expect(hasCanvas(3)).toBe(true));
    await user.click(screen.getByRole('button', { name: '次のページ' }));
    await atPdfPage(3);
    expect(pdfStage().scrollTop).toBe(jumpTop(3));

    expect(screen.getByRole('button', { name: '次のページ' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '前のページ' }));
    await atPdfPage(2);
    expect(pdfStage().scrollTop).toBe(jumpTop(2));
  });

  it('ページ番号の欄に入れて Enter（または欄を離れる）で、そのページへ飛ぶ。範囲の外は端に収め、数字でなければ戻す。Esc は入力を取り消すだけ', async () => {
    const user = userEvent.setup();
    render(view());
    const box = await atPdfPage(1);

    await user.clear(box);
    await user.type(box, '3{Enter}');
    await atPdfPage(3);
    expect(pdfStage().scrollTop).toBe(jumpTop(3));

    await user.clear(box);
    await user.type(box, '99{Enter}');
    await atPdfPage(3);

    await user.clear(box);
    await user.type(box, '0{Enter}');
    await atPdfPage(1);
    expect(pdfStage().scrollTop).toBe(jumpTop(1));

    await user.clear(box);
    await user.type(box, 'あ{Enter}');
    await atPdfPage(1);

    await user.clear(box);
    await user.type(box, '2');
    await user.keyboard('{Escape}');
    expect(box).toHaveValue('1');

    await user.clear(box);
    await user.type(box, '2');
    await user.tab(); // 欄を離れても確定する
    await atPdfPage(2);
  });

  it('指定のページから開く。総数を超える指定は、末尾に収める', async () => {
    const { unmount } = render(view({ page: 2 }));
    await atPdfPage(2);
    await waitFor(() => expect(pdfStage().scrollTop).toBe(jumpTop(2))); // 番号は最初から 2。位置は、ページの大きさが分かってから決まる
    await waitFor(() => expect(renderedPages()).toEqual([1, 2, 3])); // 2 ページ目の前後
    unmount();

    render(view({ page: 99 }));
    await atPdfPage(3);
    await waitFor(() => expect(pdfStage().scrollTop).toBe(jumpTop(3)));
  });

  it('出典を押し直す（合図が増える）と、見ているページを離れていても、出典のページへ戻る', async () => {
    const user = userEvent.setup();
    const { rerender } = render(view({ page: 2, cite: 1 }));
    await atPdfPage(2);
    await user.click(screen.getByRole('button', { name: '次のページ' }));
    await atPdfPage(3);

    rerender(view({ page: 2, cite: 2 }));

    await atPdfPage(2);
    expect(pdfStage().scrollTop).toBe(jumpTop(2));
  });

  it('出典のページが変わったら、開き直さずにそのページへ移る。ページの無い出典は、先頭へ', async () => {
    const { rerender } = render(view({ page: 3, cite: 1 }));
    await atPdfPage(3);

    rerender(view({ page: 1, cite: 2 }));
    await atPdfPage(1);
    rerender(view({ page: 3, cite: 3 }));
    await atPdfPage(3);
    rerender(view({ page: null, cite: 4 }));
    await atPdfPage(1);

    expect(fakePdf.opened).toHaveLength(1);
  });

  it('手動のスクロールで、見ているページの番号が変わる。出典のページへは、勝手に戻らない', async () => {
    render(view({ page: 2, cite: 1 }));
    await atPdfPage(2);
    await waitFor(() => expect(pdfStage().scrollTop).toBe(jumpTop(2)));

    scrollPdfTo(topOf(3) + 300 - STAGE_HEIGHT / 3 + 50); // 表示の上から 1/3 の線が、3 ページ目の中
    await atPdfPage(3);
    expect(pdfStage().scrollTop).toBe(topOf(3) + 300 - STAGE_HEIGHT / 3 + 50); // 出典の 2 ページ目へは戻らない

    scrollPdfTo(topOf(1));
    await atPdfPage(1);
    await waitFor(() => expect(hasCanvas(1)).toBe(true));
    expect(pdfStage().scrollTop).toBe(topOf(1));
  });

  it('いちばん下までスクロールしたら、最終ページの番号になる', async () => {
    render(view());
    await atPdfPage(1);

    scrollPdfTo(pdfStage().scrollHeight - STAGE_HEIGHT);

    await atPdfPage(3);
    expect(screen.getByRole('button', { name: '次のページ' })).toBeDisabled();
  });

  it('描くのは、見えているページと前後 1 ページだけ。遠いページの絵は捨て、戻ると描き直す（全ページの canvas を持たない）', async () => {
    fakePdf.pages = manyPages(10);
    render(view());
    await atPdfPage(1, 10);
    await waitFor(() => expect(hasCanvas(2)).toBe(true));
    expect(renderedPages()).toEqual([1, 2]);

    scrollPdfTo(jumpTop(8));
    await atPdfPage(8, 10);

    await waitFor(() => expect(hasCanvas(9)).toBe(true));
    expect(hasCanvas(7) && hasCanvas(8)).toBe(true);
    expect(hasCanvas(1)).toBe(false);
    expect(hasCanvas(2)).toBe(false);
    expect(document.querySelectorAll('.pdf-page canvas').length).toBeLessThanOrEqual(5);
    expect(slot(1)).toBeInTheDocument(); // 枠は残る（高さを保つ）

    scrollPdfTo(0);
    await atPdfPage(1, 10);
    await waitFor(() => expect(hasCanvas(1)).toBe(true));
    expect(hasCanvas(8)).toBe(false);
  });

  it('描き終えたページの大きさが予想（1 ページ目と同じ）と違えば、その枠はその大きさになる', async () => {
    render(view());
    await atPdfPage(1);
    await waitFor(() => expect(hasCanvas(2)).toBe(true));
    // 偽の PDF の描画結果は、枠と同じ大きさ（floor(792 × 倍率)）。枠の幅と高さが、描いた結果と一致している
    expect(slot(2).style.height).toBe(`${PAGE_H}px`);
    expect(slot(2).style.width).toBe(`${Math.floor(612 * FIT)}px`);
  });
});

describe('PdfViewer: 拡大・縮小・保存', () => {
  it('「幅に合わせる」が初期状態。拡大・縮小で倍率が変わり、「幅に合わせる」で戻る。上限・下限では押せない', async () => {
    const user = userEvent.setup();
    render(view());
    await atPdfPage(1);
    await waitFor(() => expect(fakePdf.renders.length).toBeGreaterThan(0));
    const fit = fakePdf.renders[0]!.scale;
    const fitButton = screen.getByRole('button', { name: '幅に合わせる' });
    expect(fitButton).toHaveAttribute('aria-pressed', 'true');

    await user.click(screen.getByRole('button', { name: 'PDF を拡大' }));
    await waitFor(() => expect(fakePdf.renders.at(-1)?.scale).toBeCloseTo(fit * 1.25));
    expect(fitButton).toHaveAttribute('aria-pressed', 'false');

    await user.click(screen.getByRole('button', { name: 'PDF を縮小' }));
    await user.click(screen.getByRole('button', { name: 'PDF を縮小' }));
    await waitFor(() => expect(fakePdf.renders.at(-1)?.scale).toBeCloseTo(fit * 0.8));

    await user.click(fitButton);
    await waitFor(() => expect(fakePdf.renders.at(-1)?.scale).toBeCloseTo(fit));
    expect(fitButton).toHaveAttribute('aria-pressed', 'true');

    for (let i = 0; i < 6; i += 1) {
      const zoomIn = screen.getByRole('button', { name: 'PDF を拡大' });
      if (!(zoomIn as HTMLButtonElement).disabled) await user.click(zoomIn);
    }
    expect(screen.getByRole('button', { name: 'PDF を拡大' })).toBeDisabled();
    await waitFor(() => expect(fakePdf.renders.at(-1)?.scale).toBeCloseTo(fit * ZOOM_MAX));
  });

  it('拡大すると、描いているページがすべて描き直され、枠の高さも倍率に合う。ページを移しても、倍率は保つ', async () => {
    const user = userEvent.setup();
    render(view());
    await atPdfPage(1);
    await waitFor(() => expect(hasCanvas(2)).toBe(true));
    const before = fakePdf.renders.length;

    await user.click(screen.getByRole('button', { name: 'PDF を拡大' }));

    await waitFor(() => expect(fakePdf.renders.length).toBeGreaterThanOrEqual(before + 2));
    await waitFor(() => expect(slot(1).style.height).toBe(`${Math.floor(792 * FIT * 1.25)}px`));
    await user.click(screen.getByRole('button', { name: '次のページ' }));
    await atPdfPage(2);
    await waitFor(() => expect(fakePdf.renders.some((r) => r.page === 3)).toBe(true));
    expect(fakePdf.renders.filter((r) => r.page === 3).at(-1)?.scale).toBeCloseTo(FIT * 1.25);
  });

  it('「保存」は、ダウンロードのアイコン。元の PDF を、資料名のファイル名で保存するリンク', async () => {
    render(view());
    await atPdfPage(1);

    const save = screen.getByRole('link', { name: 'PDF を保存' });
    expect(save).toHaveAttribute('href', URL_PATH);
    expect(save).toHaveAttribute('download', 'お手入れガイド.pdf');
    expect(save).toHaveAttribute('title', '保存（ダウンロード）');
    expect(save.querySelector('svg')).not.toBeNull();
    expect(save).toHaveTextContent(''); // 文字は出さない（名前は aria-label）
  });

  it('操作のボタンには名前がある（アイコンだけのボタンも）。ページ番号の欄は、キーボードで入力できる', async () => {
    render(view());
    await atPdfPage(1);

    for (const name of ['前のページ', '次のページ', 'PDF を縮小', '幅に合わせる', 'PDF を拡大']) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument();
    }
    expect(screen.getByRole('toolbar', { name: 'PDF の操作' })).toBeInTheDocument();
    expect(await pageBox()).toBeEnabled();
    expect(pdfStage()).toHaveAttribute('tabindex', '0'); // 矢印・PageUp・PageDown でスクロールできる
  });
});

describe('PdfViewer: 根拠の語句の強調', () => {
  const SPAN = '洗濯時に付着した糸くずやゴミを捕捉する「糸くずフィルター」';

  it('出典のページで、根拠の語句だけを強調する。項目をまたぐ語句は、各項目の該当部分', async () => {
    render(view({ page: 2, cite: 1, span: SPAN }));

    await waitFor(() => expect(marks().length).toBeGreaterThan(0));
    expect(marks().map((m) => m.textContent)).toEqual(['洗濯時に付着した糸くずやゴミを捕捉する', '「糸くずフィルター」']);
    expect(screen.queryByText(MSG_PDF_CITE_MISS)).not.toBeInTheDocument();
  });

  it('出典のページの絵が描かれている間は強調が残り、遠くへ離れて絵を捨てると消える。戻ると、また付く（スクロールは、出典へ勝手に飛ばない）', async () => {
    fakePdf.pages = [['1 ページ目'], ['AW-820 には、洗濯時に付着した糸くずやゴミを捕捉する', '「糸くずフィルター」と、乾燥時に発生する繊維くずを除去する'], ...manyPages(9).slice(2), ['最後のページ']];
    const user = userEvent.setup();
    render(view({ page: 2, cite: 1, span: SPAN }));
    await atPdfPage(2, 10);
    await waitFor(() => expect(marks()).toHaveLength(2));

    await user.clear(await pageBox());
    await user.type(await pageBox(), '8{Enter}');
    await atPdfPage(8, 10);
    await waitFor(() => expect(marks()).toHaveLength(0));
    expect(screen.queryByText(MSG_PDF_CITE_MISS)).not.toBeInTheDocument();

    await user.clear(await pageBox());
    await user.type(await pageBox(), '2{Enter}');
    await atPdfPage(2, 10);
    await waitFor(() => expect(marks()).toHaveLength(2));
  });

  it('拡大・縮小で描き直しても、強調は付く（複製せず、1 回分だけ）', async () => {
    const user = userEvent.setup();
    render(view({ page: 2, cite: 1, span: SPAN }));
    await waitFor(() => expect(marks()).toHaveLength(2));
    const before = fakePdf.renders.length;

    await user.click(screen.getByRole('button', { name: 'PDF を拡大' }));

    await waitFor(() => expect(fakePdf.renders.length).toBeGreaterThan(before));
    await waitFor(() => expect(marks()).toHaveLength(2));
  });

  it('出典を開いた直後だけ、強調の先頭が見える位置へスクロールする。その後の手動のスクロールを戻さない', async () => {
    render(view({ page: 2, cite: 1, span: SPAN }));
    await waitFor(() => expect(marks()).toHaveLength(2));
    // jsdom の位置は 0 なので、強調の位置は「表示の上から 1/4」の分だけ手前（0 未満にはならない）。飛んだあとの位置が、ページの先頭か 0
    expect(pdfStage().scrollTop).toBeLessThanOrEqual(jumpTop(2));

    scrollPdfTo(topOf(3) + 40);
    await atPdfPage(3);
    await waitFor(() => expect(marks()).toHaveLength(2)); // 2 ページ目は前後 1 ページの中で、絵が残る
    expect(pdfStage().scrollTop).toBe(topOf(3) + 40);
  });

  describe('チャンクの本文（passage）の強調', () => {
    // 2 ページ目の 3 つの項目のうち、前の 2 つにまたがる本文
    const PASSAGE = 'AW-820 には、洗濯時に付着した糸くずやゴミを捕捉する\n「糸くずフィルター」と、乾燥時に発生する繊維くずを除去する';

    it('語句が無いときは、本文の全体（項目ごと）を強調する。範囲の外の項目は強調しない', async () => {
      render(view({ page: 2, cite: 1, passage: PASSAGE }));

      await waitFor(() => expect(marks()).toHaveLength(2));
      expect(marks().map((m) => m.textContent)).toEqual(['AW-820 には、洗濯時に付着した糸くずやゴミを捕捉する', '「糸くずフィルター」と、乾燥時に発生する繊維くずを除去する']);
      expect(screen.queryByText(MSG_PDF_CITE_MISS)).not.toBeInTheDocument();
    });

    it('語句が見つかるときは、本文全体ではなく、語句だけを強調する', async () => {
      render(view({ page: 2, cite: 1, span: '「糸くずフィルター」', passage: PASSAGE }));

      await waitFor(() => expect(marks()).toHaveLength(1));
      expect(marks()[0]).toHaveTextContent('「糸くずフィルター」');
    });

    it('語句が見つからないときは、本文の全体に退く', async () => {
      render(view({ page: 2, cite: 1, span: '存在しない語句の引用です', passage: PASSAGE }));

      await waitFor(() => expect(marks()).toHaveLength(2));
      expect(screen.queryByText(MSG_PDF_CITE_MISS)).not.toBeInTheDocument();
    });

    it('本文が PDF の文字から 1 か所に決まらないときは、強調せず、その旨を示す', async () => {
      render(view({ page: 2, cite: 1, passage: '資料の本文には、PDF の文字と違う並びで出てくる文章です。' }));

      expect(await screen.findByText(MSG_PDF_CITE_MISS)).toBeInTheDocument();
      expect(marks()).toHaveLength(0);
    });

    it('出典がページを持たないときは、本文があっても強調しない（ページを推測しない）', async () => {
      render(view({ page: null, cite: 1, passage: PASSAGE }));

      await atPdfPage(1);
      await waitFor(() => expect(renderedPages()).toContain(2));
      expect(marks()).toHaveLength(0);
      expect(screen.queryByText(MSG_PDF_CITE_MISS)).not.toBeInTheDocument();
    });
  });

  it('ページの文字の中に語句が見つからないときは、強調せず、その旨を示す。ページは開いたまま', async () => {
    render(view({ page: 2, cite: 1, span: '存在しない語句の引用です' }));

    expect(await screen.findByText(MSG_PDF_CITE_MISS)).toBeInTheDocument();
    expect(marks()).toHaveLength(0);
    await atPdfPage(2);
  });

  it('語句が無い（出典以外で開いた）ときは、ページだけ開き、何も強調せず、何も示さない', async () => {
    render(view({ page: 2, cite: 1, span: null }));

    await atPdfPage(2);
    await waitFor(() => expect(renderedPages()).toContain(2));
    expect(marks()).toHaveLength(0);
    expect(screen.queryByText(MSG_PDF_CITE_MISS)).not.toBeInTheDocument();
  });

  it('出典がページを持たないときは、語句があっても強調しない（ページを推測しない）', async () => {
    render(view({ page: null, cite: 1, span: SPAN }));

    await atPdfPage(1);
    await waitFor(() => expect(renderedPages()).toContain(2));
    expect(marks()).toHaveLength(0);
    expect(screen.queryByText(MSG_PDF_CITE_MISS)).not.toBeInTheDocument();
  });

  it('総数を超えるページの出典は、ページを推測せず、強調しない', async () => {
    render(view({ page: 99, cite: 1, span: SPAN }));

    await atPdfPage(3);
    await waitFor(() => expect(hasCanvas(3)).toBe(true));
    expect(marks()).toHaveLength(0);
  });
});

describe('PdfViewer: 失敗と後始末', () => {
  it('PDF を開けなければ、固定の文と再試行を出し、再試行で開き直す', async () => {
    fakePdf.failOpen = true;
    const user = userEvent.setup();
    render(view());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(MSG_PDF_LOAD_FAILED);

    fakePdf.failOpen = false;
    await user.click(screen.getByRole('button', { name: '再試行' }));

    await atPdfPage(1);
    expect(fakePdf.opened).toHaveLength(2);
  });

  it('ページの描画に失敗したら、固定の文と再試行を出し、再試行で描き直す', async () => {
    fakePdf.failRender = true;
    const user = userEvent.setup();
    render(view());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(MSG_PDF_RENDER_FAILED);
    expect(alert).not.toHaveTextContent('render failed');

    fakePdf.failRender = false;
    await user.click(screen.getByRole('button', { name: '再試行' }));

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    await waitFor(() => expect(hasCanvas(1)).toBe(true));
  });

  it('画面から外すと、開いていた PDF を閉じる（worker の資源を解放する）', async () => {
    const { unmount } = render(view());
    await atPdfPage(1);
    expect(fakePdf.destroyed).toBe(0);

    unmount();

    expect(fakePdf.destroyed).toBe(1);
  });

  it('スクロールの途中で出典を押し直しても、ページの途中の描画の失敗で、画面を壊さない（描画の中断は失敗にしない）', async () => {
    fakePdf.pages = manyPages(10);
    render(view());
    await atPdfPage(1, 10);

    scrollPdfTo(jumpTop(9)); // 描きかけのページを、すぐ範囲の外へ出す
    fireEvent.scroll(pdfStage());
    scrollPdfTo(0);

    await atPdfPage(1, 10);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
