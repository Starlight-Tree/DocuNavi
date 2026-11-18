import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { MSG_PDF_LOAD_FAILED, MSG_PDF_RENDER_FAILED } from '../api/documents';
import { PDF_MARK_CLASS } from '../lib/pdfHighlight';
import { ZOOM_MAX } from '../lib/pdfView';
import { fakePdf } from '../test/fakePdf';
import { MSG_PDF_CITE_MISS, PdfViewer } from './PdfViewer';

// jsdom には canvas・Worker・レイアウトが無いので、PDF.js との境界（api/pdfjs.ts）は差し替えてある（test/fakePdf.ts）。
// ここで確かめるのは、ツールバーの操作・ページの移動・根拠の語句の強調・失敗の表示。実際の描画は、実ブラウザーで確かめる。

const URL_PATH = '/api/documents/7/file?name=guide.pdf';

function view(props: Partial<Parameters<typeof PdfViewer>[0]> = {}) {
  return (
    <PdfViewer title="お手入れガイド" url={URL_PATH} saveName="お手入れガイド.pdf" page={null} cite={0} span={null} passage={null} {...props} />
  );
}

const pageText = (n: number, total = 3) => screen.findByText(`${n} / ${total}`);
const marks = () => Array.from(document.querySelectorAll(`mark.${PDF_MARK_CLASS}`));

describe('PdfViewer: 表示とページの移動', () => {
  it('読み込み中を示し、開けたら先頭のページを描く。ツールバーは「N / 総数」', async () => {
    render(view());

    expect(screen.getByRole('status')).toHaveTextContent('読み込み中…');
    expect(await pageText(1)).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'お手入れガイド の PDF' })).toBeInTheDocument();
    expect(fakePdf.opened).toEqual([URL_PATH]);
    await waitFor(() => expect(document.querySelector('.pdf-page canvas')).not.toBeNull());
    expect(document.querySelector('.pdf-page .pdf-text-layer')).not.toBeNull();
    expect(screen.queryByText('読み込み中…')).not.toBeInTheDocument();
  });

  it('前へ・次へでページを移す。先頭では「前」、末尾では「次」を押せない', async () => {
    const user = userEvent.setup();
    render(view());
    await pageText(1);
    expect(screen.getByRole('button', { name: '前のページ' })).toBeDisabled();

    await user.click(screen.getByRole('button', { name: '次のページ' }));
    await pageText(2);
    await waitFor(() => expect(fakePdf.renders.at(-1)?.page).toBe(2));
    await user.click(screen.getByRole('button', { name: '次のページ' }));
    await pageText(3);

    expect(screen.getByRole('button', { name: '次のページ' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '前のページ' }));
    await pageText(2);
  });

  it('指定のページから開く。総数を超える指定は、末尾に収める', async () => {
    const { unmount } = render(view({ page: 2 }));
    await pageText(2);
    await waitFor(() => expect(fakePdf.renders.map((r) => r.page)).toEqual([2]));
    unmount();

    render(view({ page: 99 }));
    await pageText(3);
  });

  it('出典を押し直す（合図が増える）と、見ているページを離れていても、出典のページへ戻る', async () => {
    const user = userEvent.setup();
    const { rerender } = render(view({ page: 2, cite: 1 }));
    await pageText(2);
    await user.click(screen.getByRole('button', { name: '次のページ' }));
    await pageText(3);

    rerender(view({ page: 2, cite: 2 }));

    await pageText(2);
  });

  it('出典のページが変わったら、開き直さずにそのページへ移る。ページの無い出典は、先頭へ', async () => {
    const { rerender } = render(view({ page: 3, cite: 1 }));
    await pageText(3);

    rerender(view({ page: 1, cite: 2 }));
    await pageText(1);
    rerender(view({ page: null, cite: 3 }));
    await pageText(1);

    expect(fakePdf.opened).toHaveLength(1);
  });
});

describe('PdfViewer: 拡大・縮小・保存', () => {
  it('「幅に合わせる」が初期状態。拡大・縮小で倍率が変わり、「幅に合わせる」で戻る。上限・下限では押せない', async () => {
    const user = userEvent.setup();
    render(view());
    await pageText(1);
    await waitFor(() => expect(fakePdf.renders).toHaveLength(1));
    const fit = fakePdf.renders[0]!.scale;
    expect(screen.getByRole('button', { name: '幅に合わせる' })).toHaveAttribute('aria-pressed', 'true');

    await user.click(screen.getByRole('button', { name: 'PDF を拡大' }));
    await waitFor(() => expect(fakePdf.renders.at(-1)?.scale).toBeCloseTo(fit * 1.25));
    expect(screen.getByRole('button', { name: '幅に合わせる' })).toHaveAttribute('aria-pressed', 'false');

    await user.click(screen.getByRole('button', { name: 'PDF を縮小' }));
    await user.click(screen.getByRole('button', { name: 'PDF を縮小' }));
    await waitFor(() => expect(fakePdf.renders.at(-1)?.scale).toBeCloseTo(fit * 0.8));

    await user.click(screen.getByRole('button', { name: '幅に合わせる' }));
    await waitFor(() => expect(fakePdf.renders.at(-1)?.scale).toBeCloseTo(fit));
    expect(screen.getByRole('button', { name: '幅に合わせる' })).toHaveAttribute('aria-pressed', 'true');

    for (let i = 0; i < 6; i += 1) {
      const zoomIn = screen.getByRole('button', { name: 'PDF を拡大' });
      if (!(zoomIn as HTMLButtonElement).disabled) await user.click(zoomIn);
    }
    expect(screen.getByRole('button', { name: 'PDF を拡大' })).toBeDisabled();
    await waitFor(() => expect(fakePdf.renders.at(-1)?.scale).toBeCloseTo(fit * ZOOM_MAX));
  });

  it('ページを移しても、倍率は保つ', async () => {
    const user = userEvent.setup();
    render(view());
    await pageText(1);
    await user.click(screen.getByRole('button', { name: 'PDF を拡大' }));
    await waitFor(() => expect(fakePdf.renders).toHaveLength(2));

    await user.click(screen.getByRole('button', { name: '次のページ' }));

    await waitFor(() => expect(fakePdf.renders.at(-1)?.page).toBe(2));
    expect(fakePdf.renders.at(-1)?.scale).toBeCloseTo(fakePdf.renders[1]!.scale);
  });

  it('「保存」は、元の PDF を資料名のファイル名で保存するリンク', async () => {
    render(view());
    await pageText(1);

    const save = screen.getByRole('link', { name: 'PDF を保存' });
    expect(save).toHaveAttribute('href', URL_PATH);
    expect(save).toHaveAttribute('download', 'お手入れガイド.pdf');
    expect(save).toHaveTextContent('保存');
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

  it('別のページへ移ると強調は消え、出典のページへ戻ると、また付く', async () => {
    const user = userEvent.setup();
    render(view({ page: 2, cite: 1, span: SPAN }));
    await waitFor(() => expect(marks()).toHaveLength(2));

    await user.click(screen.getByRole('button', { name: '次のページ' }));
    await pageText(3);
    await waitFor(() => expect(marks()).toHaveLength(0));
    expect(screen.queryByText(MSG_PDF_CITE_MISS)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '前のページ' }));
    await waitFor(() => expect(marks()).toHaveLength(2));
  });

  it('拡大・縮小で描き直しても、強調は付く（複製せず、1 回分だけ）', async () => {
    const user = userEvent.setup();
    render(view({ page: 2, cite: 1, span: SPAN }));
    await waitFor(() => expect(marks()).toHaveLength(2));

    await user.click(screen.getByRole('button', { name: 'PDF を拡大' }));

    await waitFor(() => expect(fakePdf.renders).toHaveLength(2));
    await waitFor(() => expect(marks()).toHaveLength(2));
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

      await pageText(1);
      await waitFor(() => expect(fakePdf.renders).toHaveLength(1));
      expect(marks()).toHaveLength(0);
      expect(screen.queryByText(MSG_PDF_CITE_MISS)).not.toBeInTheDocument();
    });
  });

  it('ページの文字の中に語句が見つからないときは、強調せず、その旨を示す。ページは開いたまま', async () => {
    render(view({ page: 2, cite: 1, span: '存在しない語句の引用です' }));

    expect(await screen.findByText(MSG_PDF_CITE_MISS)).toBeInTheDocument();
    expect(marks()).toHaveLength(0);
    expect(screen.getByText('2 / 3')).toBeInTheDocument();
  });

  it('語句が無い（出典以外で開いた）ときは、ページだけ開き、何も強調せず、何も示さない', async () => {
    render(view({ page: 2, cite: 1, span: null }));

    await pageText(2);
    await waitFor(() => expect(fakePdf.renders).toHaveLength(1));
    expect(marks()).toHaveLength(0);
    expect(screen.queryByText(MSG_PDF_CITE_MISS)).not.toBeInTheDocument();
  });

  it('出典がページを持たないときは、語句があっても強調しない（ページを推測しない）', async () => {
    render(view({ page: null, cite: 1, span: SPAN }));

    await pageText(1);
    await waitFor(() => expect(fakePdf.renders).toHaveLength(1));
    expect(marks()).toHaveLength(0);
    expect(screen.queryByText(MSG_PDF_CITE_MISS)).not.toBeInTheDocument();
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

    await pageText(1);
    expect(fakePdf.opened).toHaveLength(2);
  });

  it('ページの描画に失敗したら、固定の文と再試行を出し、再試行で描き直す', async () => {
    const user = userEvent.setup();
    render(view());
    await pageText(1);
    await waitFor(() => expect(document.querySelector('.pdf-page canvas')).not.toBeNull());

    fakePdf.failRender = true;
    await user.click(screen.getByRole('button', { name: '次のページ' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(MSG_PDF_RENDER_FAILED);
    expect(alert).not.toHaveTextContent('render failed');

    fakePdf.failRender = false;
    await user.click(screen.getByRole('button', { name: '再試行' }));

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    await waitFor(() => expect(fakePdf.renders.at(-1)?.page).toBe(2));
  });

  it('画面から外すと、開いていた PDF を閉じる（worker の資源を解放する）', async () => {
    const { unmount } = render(view());
    await pageText(1);
    expect(fakePdf.destroyed).toBe(0);

    unmount();

    expect(fakePdf.destroyed).toBe(1);
  });
});
