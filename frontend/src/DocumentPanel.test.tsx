import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { jsonResponse, replyResponse } from './test/helpers';

// 出典を押して右のパネルを開く流れ（A 案）: 抽出テキストで根拠を強調し、「元のPDF」へ切り替えたら出典のページを開く。
// パネルの開閉・拡大・状態の保持・履歴・フォーカスを確かめる。

const UPDATED = '2026-10-18T00:00:00+00:00';

function summary(id: number, source: string, extra: object = {}) {
  return {
    id,
    source,
    status: 'ready',
    failure_reason: null,
    pages: 4,
    updated_at: UPDATED,
    chunk_count: 2,
    embedded_count: 2,
    ...extra,
  };
}

function chunk(index: number, page: number, content: string) {
  return {
    chunk_index: index,
    page,
    page_end: null,
    kind: 'text',
    heading: `見出し${index}`,
    content,
    embedded: true,
    embedding_model: 'm',
  };
}

const LIST = {
  configured: true,
  documents: [summary(7, 'tsubasa_dorm_rules.pdf'), summary(8, 'cafe_store_guide.pdf')],
};

const DETAIL: Record<string, unknown> = {
  '/documents/7/chunks': {
    document: LIST.documents[0],
    chunks: [chunk(0, 1, '第2条 寮は3棟で構成する。この文は一つ目のチャンクです。'), chunk(1, 3, '第5条 入寮の申込みは、2 月 28 日までに行う。')],
    original_pdf: true,
  },
  '/documents/8/chunks': {
    document: LIST.documents[1],
    chunks: [chunk(0, 1, 'カフェの営業時間は、午前 9 時から午後 5 時までです。')],
    original_pdf: true,
  },
};

const CONTEXT =
  '[tsubasa_dorm_rules.pdf]\n第2条 寮は3棟で構成する。この文は一つ目のチャンクです。\n\n[tsubasa_dorm_rules.pdf]\n第5条 入寮の申込みは、2 月 28 日までに行う。';
const SOURCES = [
  { name: 'tsubasa_dorm_rules.pdf', page: 1, page_end: null, weak: false },
  { name: 'tsubasa_dorm_rules.pdf', page: 3, page_end: null, weak: false },
];

let files: string[];
let urlCount: number;

/** `install` に渡すと、サーバーが出典のページを送らない（古い応答）。 */
const NO_SOURCES = Symbol('no sources');

function install({ sources = SOURCES, list = LIST, context = CONTEXT }: { sources?: unknown; list?: unknown; context?: string } = {}) {
  files = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'gemini' });
      if (url.endsWith('/collections')) return jsonResponse({ configured: false, collections: [] });
      if (url.endsWith('/targets')) return jsonResponse({ configured: false, targets: [] });
      if (url.endsWith('/doc-types')) return jsonResponse({ configured: false, doc_types: [] });
      if (url.includes('/chat/stream')) return replyResponse({ reply: '2 月 28 日までです。', context, sources: sources === NO_SOURCES ? undefined : sources });
      if (url === '/documents') return jsonResponse(list);
      if (url.endsWith('/file')) {
        files.push(url);
        return new Response(new Blob(['%PDF-1.4'], { type: 'application/pdf' }), {
          status: 200,
          headers: { 'Content-Type': 'application/pdf' },
        });
      }
      const detail = DETAIL[url];
      return detail ? jsonResponse(detail) : new Response('not found', { status: 404 });
    }),
  );
}

beforeEach(() => {
  urlCount = 0;
  Object.assign(URL, { createObjectURL: () => `blob:test/${++urlCount}`, revokeObjectURL: () => {} });
});
afterEach(() => {
  Reflect.deleteProperty(URL, 'createObjectURL');
  Reflect.deleteProperty(URL, 'revokeObjectURL');
});

type User = ReturnType<typeof userEvent.setup>;

async function ask(user: User) {
  await user.type(await screen.findByRole('textbox', { name: 'メッセージ' }), '申込みはいつ？');
  await user.click(screen.getByRole('button', { name: '送信' }));
  await screen.findByText('2 月 28 日までです。');
}

const chips = () => screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
const findPanel = () => screen.findByRole('complementary', { name: /^資料:/ });
const pdfFrame = () => screen.getByTitle('tsubasa_dorm_rules.pdf の元PDF') as HTMLIFrameElement;

async function openCitation(user: User, index: number) {
  await user.click((await chips())[index]!);
  return within(await findPanel());
}

describe('出典を押して右のパネルを開く', () => {
  it('チャットのまま、抽出テキストで根拠のチャンクを強調する。会話と未送信の文は残る', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    await user.type(screen.getByRole('textbox', { name: 'メッセージ' }), '書きかけ');

    const panel = await openCitation(user, 1);

    expect(window.location.search).toBe('?tab=chat&doc=7&view=text&page=3&chunk=1');
    expect(panel.getByRole('button', { name: '抽出テキスト' })).toHaveAttribute('aria-pressed', 'true');
    expect(panel.getByText('PDFから抽出したテキストです。レイアウトや図は含まれません。')).toBeVisible();
    // 根拠の箇所が見つかったときは、「特定できなかった」とは出さない
    expect(panel.queryByText(/箇所を特定できなかった/)).not.toBeInTheDocument();
    const marked = document.querySelectorAll('[data-focus="true"]');
    expect(marked).toHaveLength(1);
    expect(marked[0]).toHaveTextContent('第5条 入寮の申込みは、2 月 28 日までに行う。');
    expect(screen.getByRole('log', { name: '会話' })).toBeVisible();
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toHaveValue('書きかけ');
  });

  it('PDF は、「元のPDF」を初めて開くまで取らない。切り替えたら、出典のページを開く', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const panel = await openCitation(user, 1);
    await panel.findByText('第5条 入寮の申込みは、2 月 28 日までに行う。');
    expect(files).toEqual([]);
    expect(screen.queryByTitle(/の元PDF/)).not.toBeInTheDocument();

    await user.click(panel.getByRole('button', { name: '元のPDF' }));

    expect(await screen.findByTitle('tsubasa_dorm_rules.pdf の元PDF')).toHaveAttribute('src', 'blob:test/1#page=3');
    expect(files).toEqual(['/documents/7/file']);
  });

  it('出典がページを持たないときは、ページを推測せず、「元のPDF」は先頭から開く', async () => {
    install({ sources: NO_SOURCES });
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const panel = await openCitation(user, 1);
    expect(window.location.search).toBe('?tab=chat&doc=7&view=text&chunk=1');
    await user.click(panel.getByRole('button', { name: '元のPDF' }));

    expect(await screen.findByTitle('tsubasa_dorm_rules.pdf の元PDF')).toHaveAttribute('src', 'blob:test/1');
  });

  it('同じ資料の中で表示を切り替えても、PDF は取り直さず作り直さない（見ている位置を保つ）', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    const panel = await openCitation(user, 1);
    await user.click(panel.getByRole('button', { name: '元のPDF' }));
    const frame = await screen.findByTitle('tsubasa_dorm_rules.pdf の元PDF');

    await user.click(panel.getByRole('button', { name: '抽出テキスト' }));
    expect(frame).not.toBeVisible();
    await user.click(panel.getByRole('button', { name: '元のPDF' }));

    expect(pdfFrame()).toBe(frame);
    expect(frame).toBeVisible();
    expect(files).toHaveLength(1);
  });

  it('別の出典を押したら、保持中の位置より新しい出典の位置を優先する（同じ出典を押し直しても、その位置へ戻す）', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    const panel = await openCitation(user, 1);
    await user.click(panel.getByRole('button', { name: '元のPDF' }));
    const first = await screen.findByTitle('tsubasa_dorm_rules.pdf の元PDF');
    expect(first).toHaveAttribute('src', 'blob:test/1#page=3');

    await user.click((await chips())[0]!);

    await waitFor(() => expect(window.location.search).toBe('?tab=chat&doc=7&view=text&page=1&chunk=0'));
    await user.click(panel.getByRole('button', { name: '元のPDF' }));
    const second = pdfFrame();
    expect(second).toHaveAttribute('src', 'blob:test/1#page=1');
    expect(second).not.toBe(first);

    // 同じ出典を押し直すと、PDF の位置は出典のページへ戻る（作り直す）
    await user.click(panel.getByRole('button', { name: '抽出テキスト' }));
    await user.click((await chips())[0]!);
    await waitFor(() => expect(pdfFrame()).not.toBe(second));
    expect(pdfFrame()).toHaveAttribute('src', 'blob:test/1#page=1');
    expect(files).toHaveLength(1); // PDF そのものは取り直さない
  });

  it('資料一覧からの表示とタブの移動: パネルは作り直されず、別の資料へ移ると前の資料のページ・強調を引き継がない', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    await openCitation(user, 1);
    const panelBefore = await findPanel();

    await user.click(screen.getByRole('link', { name: '資料' }));

    expect(window.location.search).toBe('?tab=documents&doc=7&view=text&page=3&chunk=1');
    expect(await findPanel()).toBe(panelBefore); // タブを移っても、同じパネルのまま
    expect(document.querySelectorAll('[data-focus="true"]')).toHaveLength(1);

    await user.click(within(screen.getByRole('table')).getByRole('button', { name: 'cafe_store_guide.pdf を開く' }));

    expect(window.location.search).toBe('?tab=documents&doc=8');
    const next = within(await findPanel());
    expect(await next.findByTitle('cafe_store_guide.pdf の元PDF')).toBeInTheDocument();
    expect(document.querySelectorAll('[data-focus="true"]')).toHaveLength(0);
    expect(screen.queryByTitle('tsubasa_dorm_rules.pdf の元PDF')).not.toBeInTheDocument();
  });
});

describe('パネルの拡大・縮小', () => {
  it('CSS だけで広げる。同じ要素のまま、会話・未送信の文・PDF を作り直さない', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    await user.type(screen.getByRole('textbox', { name: 'メッセージ' }), '書きかけ');
    const panel = await openCitation(user, 1);
    await user.click(panel.getByRole('button', { name: '元のPDF' }));
    const frame = await screen.findByTitle('tsubasa_dorm_rules.pdf の元PDF');
    const aside = await findPanel();
    const message = screen.getByText('2 月 28 日までです。');

    await user.click(panel.getByRole('button', { name: '拡大' }));

    expect(await findPanel()).toBe(aside);
    expect(aside).toHaveClass('ws-panel-expanded');
    expect(panel.getByRole('button', { name: '縮小' })).toHaveAttribute('aria-pressed', 'true');
    expect(document.querySelector('.ws-main')).toHaveAttribute('inert');
    expect(pdfFrame()).toBe(frame);
    expect(screen.getByText('2 月 28 日までです。')).toBe(message);

    await user.click(panel.getByRole('button', { name: '縮小' }));

    expect(aside).not.toHaveClass('ws-panel-expanded');
    expect(document.querySelector('.ws-main')).not.toHaveAttribute('inert');
    expect(pdfFrame()).toBe(frame);
    expect(files).toHaveLength(1);
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toHaveValue('書きかけ');
  });

  it('閉じると、拡大は戻る', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    const panel = await openCitation(user, 1);
    await user.click(panel.getByRole('button', { name: '拡大' }));

    await user.click(panel.getByRole('button', { name: 'パネルを閉じる' }));
    expect(screen.queryByRole('complementary', { name: /^資料:/ })).not.toBeInTheDocument();
    expect(document.querySelector('.ws-main')).not.toHaveAttribute('inert');

    await openCitation(user, 1);
    expect((await findPanel()).className).not.toContain('ws-panel-expanded');
  });
});

describe('パネルと履歴', () => {
  it('開く・閉じるは戻る・進むで復元できる。同じ状態への操作では履歴を増やさない', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    const before = window.history.length;

    await openCitation(user, 1);
    expect(window.history.length).toBe(before + 1);

    // 同じ出典をもう一度押しても、同じ URL なので履歴は増えない
    await user.click((await chips())[1]!);
    await waitFor(() => expect(screen.getByRole('complementary', { name: /^資料:/ })).toBeInTheDocument());
    expect(window.history.length).toBe(before + 1);

    window.history.back();
    await waitFor(() => expect(screen.queryByRole('complementary', { name: /^資料:/ })).not.toBeInTheDocument());
    expect(window.location.search).toBe('?tab=chat');

    window.history.forward();
    expect(await findPanel()).toBeInTheDocument();
    expect(window.location.search).toBe('?tab=chat&doc=7&view=text&page=3&chunk=1');
  });

  it('URL で開いたとき（戻る・進む・再読み込み）は、フォーカスを奪わない', async () => {
    install();
    window.history.replaceState(null, '', '/?tab=chat&doc=7&view=text&chunk=1');
    render(<App />);

    const panel = await findPanel();
    await within(panel).findByText('第5条 入寮の申込みは、2 月 28 日までに行う。');
    expect(panel).not.toHaveFocus();
    expect(document.querySelectorAll('[data-focus="true"]')).toHaveLength(1);
  });

  it('一覧に無い資料（消えた・権限が無い）の URL は、パネルを出さず、履歴を増やさずに閉じる', async () => {
    install({ list: { configured: true, documents: [LIST.documents[1]] } });
    window.history.replaceState(null, '', '/?tab=chat&doc=7&view=text');
    const before = window.history.length;
    render(<App />);

    await waitFor(() => expect(window.location.search).toBe('?tab=chat'));
    expect(screen.queryByRole('complementary', { name: /^資料:/ })).not.toBeInTheDocument();
    expect(window.history.length).toBe(before);
  });

  it('資料の一覧を取れなかったときは、パネルに固定メッセージと再試行を出す', async () => {
    let calls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'gemini' });
        if (url.endsWith('/collections')) return jsonResponse({ configured: false, collections: [] });
        if (url.endsWith('/targets')) return jsonResponse({ configured: false, targets: [] });
        if (url.endsWith('/doc-types')) return jsonResponse({ configured: false, doc_types: [] });
        if (url === '/documents') {
          calls += 1;
          return calls === 1 ? new Response('<html>Bad Gateway postgresql://u:pw@host/db</html>', { status: 502 }) : jsonResponse(LIST);
        }
        return jsonResponse(DETAIL[url] ?? {});
      }),
    );
    window.history.replaceState(null, '', '/?tab=chat&doc=7&view=text');
    const user = userEvent.setup();
    render(<App />);

    const panel = within(await screen.findByRole('complementary', { name: '資料' }));
    const alert = await panel.findByRole('alert');
    expect(alert).toHaveTextContent('HTTP 502');
    expect(alert).not.toHaveTextContent('postgresql');
    await user.click(panel.getByRole('button', { name: '再試行' }));

    expect(await screen.findByRole('complementary', { name: /^資料: tsubasa_dorm_rules\.pdf/ })).toBeInTheDocument();
  });
});

describe('パネルのフォーカス', () => {
  it('出典を押して開いたらパネルへフォーカスを移し、Esc で閉じたら押したカードへ戻す', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    const chip = (await chips())[1]!;

    await user.click(chip);

    const panel = await findPanel();
    await waitFor(() => expect(panel).toHaveFocus());

    await user.keyboard('{Escape}');

    await waitFor(() => expect(screen.queryByRole('complementary', { name: /^資料:/ })).not.toBeInTheDocument());
    expect(chip).toHaveFocus();
    expect(window.location.search).toBe('?tab=chat');
  });

  it('拡大中の Esc は、閉じずに縮小する', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    const panel = await openCitation(user, 1);
    await user.click(panel.getByRole('button', { name: '拡大' }));

    await user.keyboard('{Escape}');

    expect(await findPanel()).not.toHaveClass('ws-panel-expanded');
    expect(window.location.search).toContain('doc=7');
  });

  it('資料一覧の行から開いたときも、パネルへ移り、閉じると押したボタンへ戻る', async () => {
    install();
    window.history.replaceState(null, '', '/?tab=documents');
    const user = userEvent.setup();
    render(<App />);
    const open = await within(await screen.findByRole('table')).findByRole('button', { name: 'cafe_store_guide.pdf を開く' });

    await user.click(open);

    await waitFor(async () => expect(await findPanel()).toHaveFocus());
    await user.click(screen.getByRole('button', { name: 'パネルを閉じる' }));
    expect(open).toHaveFocus();
    expect(window.location.search).toBe('?tab=documents');
  });
});

describe('資料ビューア: タブ・メタ情報・根拠の特定', () => {
  const tabs = (panel: ReturnType<typeof within>) =>
    within(panel.getByRole('group', { name: '表示の切り替え' })).getAllByRole('button').map((b) => b.textContent);

  it('タブは「抽出テキスト」「元のPDF」の順。関係図は、対応する資料（入寮規程）にだけ付く', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const panel = await openCitation(user, 1);
    expect(tabs(panel)).toEqual(['抽出テキスト', '元のPDF', '関係図']);

    // 関係図の無い資料（カフェ）には、タブも、案内の文も出さない
    await user.click(screen.getByRole('link', { name: '資料' }));
    await user.click(within(screen.getByRole('table')).getByRole('button', { name: 'cafe_store_guide.pdf を開く' }));
    const cafe = within(await screen.findByRole('complementary', { name: '資料: cafe_store_guide.pdf' }));
    // 一覧から開いたときは、元のPDF（あれば）から。出典のハイライトに依存せず、資料全体を見る
    await cafe.findByTitle('cafe_store_guide.pdf の元PDF');
    expect(tabs(cafe)).toEqual(['抽出テキスト', '元のPDF']);
    expect(cafe.queryByText(/関係図は/)).not.toBeInTheDocument();
  });

  it('資料名の下に、ページ数と更新日時を出す。更新日時の記録が無い資料は「記録なし」と出す（日時を作らない）', async () => {
    install({ list: { configured: true, documents: [summary(7, 'tsubasa_dorm_rules.pdf'), summary(8, 'cafe_store_guide.pdf', { updated_at: null })] } });
    window.history.replaceState(null, '', '/?tab=documents&doc=7');
    render(<App />);

    const panel = within(await findPanel());
    expect((await panel.findByText('4 ページ')).parentElement).toHaveTextContent(/更新 2026\/10\/18/);

    cleanup();
    window.history.replaceState(null, '', '/?tab=documents&doc=8');
    render(<App />);
    const cafe = within(await screen.findByRole('complementary', { name: '資料: cafe_store_guide.pdf' }));
    expect((await cafe.findByText('4 ページ')).parentElement).toHaveTextContent('更新 記録なし');
    expect(cafe.getByText('記録なし')).toHaveAttribute('title', 'この資料には、更新日時の記録がありません');
  });

  it('出典の根拠の箇所を特定できなかったときは、強調を付けず、その旨を示す。ほかの資料を開いたときは引き継がない', async () => {
    // 抜粋が、資料のどのチャンクとも合わない（ページは、サーバーが返した値がある）
    install({ context: '[tsubasa_dorm_rules.pdf]\n全く別の文章が、ここに入っています。', sources: [{ name: 'tsubasa_dorm_rules.pdf', page: 2, page_end: null, weak: false }] });
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const panel = await openCitation(user, 0);

    // ページは、サーバーが返した値（2）だけを持つ。箇所は推測せず、何も強調しない
    expect(window.location.search).toBe('?tab=chat&doc=7&view=text&page=2');
    expect(await panel.findByRole('status')).toHaveTextContent('出典の箇所を特定できなかったため、強調は付けていません');
    expect(document.querySelectorAll('[data-focus="true"]')).toHaveLength(0);
    expect(panel.queryByText('根拠の箇所')).not.toBeInTheDocument();
    // 資料全体は読める（2 つのチャンクが出る）
    expect(panel.getAllByRole('listitem')).toHaveLength(2);

    // 資料一覧から別の資料を開いたときは、この案内を引き継がない
    await user.click(screen.getByRole('link', { name: '資料' }));
    await user.click(within(screen.getByRole('table')).getByRole('button', { name: 'cafe_store_guide.pdf を開く' }));
    const cafe = within(await screen.findByRole('complementary', { name: '資料: cafe_store_guide.pdf' }));
    await cafe.findByTitle('cafe_store_guide.pdf の元PDF');
    expect(cafe.queryByRole('status')).not.toBeInTheDocument();
  });

  it('「元のPDF」には、元のPDFをそのまま表示していることと、出典のページを書く。ページが無い出典では、ページを書かない', async () => {
    install();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    const panel = await openCitation(user, 1);

    await user.click(panel.getByRole('button', { name: '元のPDF' }));
    expect(await panel.findByText('元のPDFをそのまま表示しています。 出典のページ（p.3）を開いています。')).toBeInTheDocument();
  });
});
