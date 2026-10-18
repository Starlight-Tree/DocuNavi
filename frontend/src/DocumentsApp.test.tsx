import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { AuthProvider } from './auth/AuthContext';
import { MSG_NOT_CONFIGURED, MSG_NO_DOCUMENTS, MSG_NO_GRAPH } from './components/DocumentsView';
import { fakeAuth } from './test/auth';
import type { FakeAuth } from './test/auth';
import { errorResponse, jsonResponse } from './test/helpers';

const UPDATED = '2026-10-18T00:00:00+00:00';

function summary(id: number, source: string, chunks: number, embedded: number, extra: object = {}) {
  return {
    id,
    source,
    status: 'ready',
    failure_reason: null,
    pages: 3,
    updated_at: UPDATED,
    chunk_count: chunks,
    embedded_count: embedded,
    ...extra,
  };
}

function chunk(index: number, content: string, extra: object = {}) {
  return {
    chunk_index: index,
    page: index + 1,
    page_end: null,
    kind: 'text',
    heading: '第1章 総則',
    content,
    embedded: true,
    embedding_model: 'gemini-embedding-2',
    ...extra,
  };
}

const LIST = {
  configured: true,
  documents: [summary(1, 'tsubasa_dorm_rules.pdf', 3, 3), summary(2, 'cafe_store_guide.pdf', 30, 28)],
};

type Route = (url: string, init: RequestInit, call: number) => Response | Promise<Response>;

/** /health と /documents 系を差し替える。/chat/stream には何も返さない（呼ばれたら失敗させる）。 */
function installDocsFetch(route: Route) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'mock' });
    calls.push({ url, init: init ?? {} });
    return route(url, init ?? {}, calls.length);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls };
}

function setup(auth: FakeAuth | null = null) {
  const user = userEvent.setup();
  render(
    <AuthProvider client={auth}>
      <App />
    </AuthProvider>,
  );
  return { user };
}

async function openDocuments(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: '資料一覧' }));
}

describe('資料一覧: 画面', () => {
  it('開くまでは資料を取らない。開くと、文書ごとの状態が出る（説明文は出さない）', async () => {
    const { calls } = installDocsFetch(() => jsonResponse(LIST));
    const { user } = setup();
    await screen.findByRole('textbox', { name: 'メッセージ' });
    expect(calls).toHaveLength(0);

    await openDocuments(user);

    const row = (await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ })) as HTMLElement;
    expect(within(row).getByText('取り込み済み')).toBeInTheDocument();
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
    expect(screen.queryByText('埋め込み済み')).not.toBeInTheDocument();
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe('/documents');
    expect(calls[0]!.init.method ?? 'GET').toBe('GET');
  });

  it('資料一覧を開くと、チャットの入力欄とメッセージは隠れ、「チャット」に戻ると入力途中の文も残っている', async () => {
    installDocsFetch(() => jsonResponse(LIST));
    const { user } = setup();
    await user.type(await screen.findByRole('textbox', { name: 'メッセージ' }), '書きかけの質問');

    await openDocuments(user);
    expect(screen.queryByRole('textbox', { name: 'メッセージ' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '新しい会話' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'チャット' }));
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toHaveValue('書きかけの質問');
  });

  it('いま開いている画面を、タブに示す', async () => {
    installDocsFetch(() => jsonResponse(LIST));
    const { user } = setup();
    expect(await screen.findByRole('button', { name: 'チャット' })).toHaveAttribute('aria-current', 'page');

    await openDocuments(user);

    expect(screen.getByRole('button', { name: '資料一覧' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: 'チャット' })).not.toHaveAttribute('aria-current');
  });

  it('DB が設定されていないときは、文書 0 件とは別の案内を出す', async () => {
    installDocsFetch(() => jsonResponse({ configured: false, documents: [] }));
    const { user } = setup();

    await openDocuments(user);

    expect(await screen.findByText(MSG_NOT_CONFIGURED)).toBeInTheDocument();
    expect(screen.queryByText(MSG_NO_DOCUMENTS)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '資料一覧' })).toBeInTheDocument(); // タブは残る
  });

  it('文書が 0 件のときは、「登録された資料はありません」を出す', async () => {
    installDocsFetch(() => jsonResponse({ configured: true, documents: [] }));
    const { user } = setup();

    await openDocuments(user);

    expect(await screen.findByText(MSG_NO_DOCUMENTS)).toBeInTheDocument();
    expect(screen.queryByText(MSG_NOT_CONFIGURED)).not.toBeInTheDocument();
  });

  it('取得に失敗したら固定メッセージと再試行を出し、再試行で取り直せる', async () => {
    const message = '資料を取得できませんでした。しばらく待ってから再試行してください。';
    const { calls } = installDocsFetch((_url, _init, call) =>
      call === 1 ? errorResponse(503, message) : jsonResponse(LIST),
    );
    const { user } = setup();

    await openDocuments(user);

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ })).toBeInTheDocument();
    expect(calls).toHaveLength(2);
  });

  it('サーバーが英語や想定外の本文を返しても、そのままは見せない', async () => {
    installDocsFetch(() => new Response('<html>Bad Gateway postgresql://user:pw@host/db</html>', { status: 502 }));
    const { user } = setup();

    await openDocuments(user);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('HTTP 502');
    expect(alert).not.toHaveTextContent('postgresql');
  });

  it('形が違う応答は、想定外の応答として扱う', async () => {
    installDocsFetch(() => jsonResponse({ configured: true, documents: [{ id: 'x' }] }));
    const { user } = setup();

    await openDocuments(user);

    expect(await screen.findByRole('alert')).toHaveTextContent('想定外の応答');
  });
});

describe('資料一覧: チャンク', () => {
  const DETAIL = {
    document: summary(1, 'tsubasa_dorm_rules.pdf', 3, 2),
    chunks: [
      chunk(0, '第1条 この規程は、つばさ大学学生寮の運営を定める。\n第2条 寮は3棟で構成する。'),
      chunk(1, '2 前項の定めにかかわらず…', { heading: 'つばさ大学 学生寮 入寮規程', page_end: 3 }),
      chunk(2, '<b>太字にならない</b> <script>alert(1)</script>', {
        embedded: false,
        embedding_model: null,
        heading: null,
      }),
    ],
  };

  function routes(url: string) {
    return jsonResponse(url === '/documents' ? LIST : DETAIL);
  }

  it('「開く」で、チャンクを順に、見出し・ページ・本文の全文つきで出す', async () => {
    const { calls } = installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);

    await user.click(await screen.findByRole('button', { name: 'tsubasa_dorm_rules.pdf を開く' }));

    const items = await screen.findAllByRole('listitem');
    expect(items).toHaveLength(3);
    // チャンク番号・埋め込みの状態は、開発者向けなので、既定では隠す
    expect(items[0]).not.toHaveTextContent('#0');
    expect(items[0]).not.toHaveTextContent('埋め込み');
    expect(screen.queryByText(/チャンク（埋め込み済み/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '開発者向けの情報' }));
    expect(screen.getByRole('button', { name: '開発者向けの情報' })).toHaveAttribute('aria-pressed', 'true');
    expect(items[0]).toHaveTextContent('#0');
    expect(items[0]).toHaveTextContent('p.1');
    expect(items[0]).toHaveTextContent('第1章 総則');
    expect(items[0]).toHaveTextContent('埋め込み済み（gemini-embedding-2）');
    expect(items[0]!.querySelector('pre')!.textContent).toBe(
      '第1条 この規程は、つばさ大学学生寮の運営を定める。\n第2条 寮は3棟で構成する。',
    );
    expect(items[1]).toHaveTextContent('p.2–3');
    expect(items[1]).toHaveTextContent('つばさ大学 学生寮 入寮規程');
    expect(items[2]).toHaveTextContent('（見出しなし）');
    expect(items[2]).toHaveTextContent('埋め込みなし');
    expect(screen.getByText('3 チャンク（埋め込み済み 2 / 3）')).toBeInTheDocument();
    expect(calls.map((c) => c.url)).toEqual(['/documents', '/documents/1/chunks']);
  });

  it('行のどこを押しても開く', async () => {
    installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);

    const row = await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });
    await user.click(within(row).getByText('取り込み済み'));

    expect(await screen.findAllByRole('listitem')).toHaveLength(3);
  });

  it('本文は HTML として解釈せず、そのままの文字で出す', async () => {
    installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);

    await user.click(await screen.findByRole('button', { name: 'tsubasa_dorm_rules.pdf を開く' }));

    const items = await screen.findAllByRole('listitem');
    const pre = items[2]!.querySelector('pre')!;
    expect(pre.textContent).toBe('<b>太字にならない</b> <script>alert(1)</script>');
    expect(pre.querySelector('b, script')).toBeNull();
  });

  it('「資料一覧に戻る」で一覧に戻る', async () => {
    installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);
    await user.click(await screen.findByRole('button', { name: 'cafe_store_guide.pdf を開く' }));
    await screen.findAllByRole('listitem');

    await user.click(screen.getByRole('button', { name: '資料一覧に戻る' }));

    expect(await screen.findByRole('row', { name: /cafe_store_guide\.pdf/ })).toBeInTheDocument();
  });

  it('チャンクの取得に失敗したら、固定メッセージと再試行を出す', async () => {
    installDocsFetch((url, _init, call) =>
      url === '/documents'
        ? jsonResponse(LIST)
        : call === 2
          ? errorResponse(503, '資料を取得できませんでした。しばらく待ってから再試行してください。')
          : jsonResponse(DETAIL),
    );
    const { user } = setup();
    await openDocuments(user);

    await user.click(await screen.findByRole('button', { name: 'tsubasa_dorm_rules.pdf を開く' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('資料を取得できませんでした');
    await user.click(screen.getByRole('button', { name: '再試行' }));

    expect(await screen.findAllByRole('listitem')).toHaveLength(3);
  });
});

describe('資料一覧: ログイン', () => {
  it('ログイン中は、トークンを付けて取る', async () => {
    const { calls } = installDocsFetch(() => jsonResponse(LIST));
    const { user } = setup(fakeAuth());

    await openDocuments(user);

    await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(new Headers(calls[0]!.init.headers).get('Authorization')).toBe('Bearer tok-1');
  });

  it('401 ならトークンを 1 回更新して送り直す', async () => {
    const auth = fakeAuth();
    const { calls } = installDocsFetch((_url, _init, call) =>
      call === 1 ? errorResponse(401, 'ログインが必要です。ログインし直してください。') : jsonResponse(LIST),
    );
    const { user } = setup(auth);

    await openDocuments(user);

    await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(auth.refreshAccessToken).toHaveBeenCalledTimes(1);
    expect(calls).toHaveLength(2);
    expect(new Headers(calls[1]!.init.headers).get('Authorization')).toBe('Bearer tok-2');
  });

  it('更新しても 401 ならログイン画面に戻す', async () => {
    const auth = fakeAuth({ refreshed: null });
    installDocsFetch(() => errorResponse(401, 'ログインが必要です。ログインし直してください。'));
    const { user } = setup(auth);

    await openDocuments(user);

    expect(await screen.findByRole('button', { name: 'Google でログイン' })).toBeInTheDocument();
  });

  it('ログインを使わない構成では、トークンを付けない', async () => {
    const { calls } = installDocsFetch(() => jsonResponse(LIST));
    const { user } = setup(null);

    await openDocuments(user);

    await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(new Headers(calls[0]!.init.headers).get('Authorization')).toBeNull();
  });
});

describe('資料一覧: 題名', () => {
  const TITLED = {
    configured: true,
    documents: [
      summary(1, 'tsubasa_dorm_rules.pdf', 3, 3, { title: 'つばさ大学学生寮 入寮規程' }),
      summary(2, 'cafe_store_guide.pdf', 30, 28, { title: null }),
      summary(3, 'blank_title.pdf', 1, 1, { title: '   ' }),
    ],
  };
  const DETAIL = {
    document: summary(1, 'tsubasa_dorm_rules.pdf', 1, 1, { title: 'つばさ大学学生寮 入寮規程' }),
    chunks: [chunk(0, '第1条 この規程は…')],
  };

  it('題名があれば題名を主に、ファイル名を補助に出す。題名が無い・空白だけならファイル名だけ（重ねて出さない）', async () => {
    installDocsFetch(() => jsonResponse(TITLED));
    const { user } = setup();
    await openDocuments(user);

    const titled = await screen.findByRole('row', { name: /入寮規程/ });
    expect(within(titled).getByText('つばさ大学学生寮 入寮規程')).toHaveClass('document-name');
    expect(within(titled).getByText('tsubasa_dorm_rules.pdf')).toHaveClass('document-file');
    const plain = screen.getByRole('row', { name: /cafe_store_guide\.pdf/ });
    expect(within(plain).getAllByText('cafe_store_guide.pdf')).toHaveLength(1);
    expect(plain.querySelector('.document-file')).toBeNull();
    const blank = screen.getByRole('row', { name: /blank_title\.pdf/ });
    expect(blank.querySelector('.document-file')).toBeNull();
  });

  it('「開く」ボタンの名前と詳細の見出しは題名、ファイル名は補助に出す', async () => {
    installDocsFetch((url) => jsonResponse(url === '/documents' ? TITLED : DETAIL));
    const { user } = setup();
    await openDocuments(user);

    await user.click(await screen.findByRole('button', { name: 'つばさ大学学生寮 入寮規程 を開く' }));

    const heading = await screen.findByRole('heading', { level: 2 });
    expect(heading).toHaveTextContent('つばさ大学学生寮 入寮規程');
    expect(heading).toHaveTextContent('tsubasa_dorm_rules.pdf');
  });

  it('title が文字列でも null でもない応答は、不正な応答として扱う', async () => {
    installDocsFetch(() =>
      jsonResponse({ configured: true, documents: [summary(1, 'a.pdf', 1, 1, { title: 123 })] }),
    );
    const { user } = setup();
    await openDocuments(user);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('title の項目が無い応答（古いサーバー）は、題名なしとして読む', async () => {
    installDocsFetch(() => jsonResponse(LIST));
    const { user } = setup();
    await openDocuments(user);

    expect(await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ })).toBeInTheDocument();
  });
});

describe('資料一覧: 元PDF', () => {
  const OMIT = Symbol('omit');
  const DETAIL = (originalPdf: unknown) => ({
    document: summary(1, 'tsubasa_dorm_rules.pdf', 1, 1, { title: 'つばさ大学学生寮 入寮規程' }),
    chunks: [chunk(0, '第1条 この規程は…')],
    ...(originalPdf === OMIT ? {} : { original_pdf: originalPdf }),
  });
  const TITLED_LIST = {
    configured: true,
    documents: [summary(1, 'tsubasa_dorm_rules.pdf', 1, 1, { title: 'つばさ大学学生寮 入寮規程' })],
  };
  const PDF_BYTES = '%PDF-1.4 test';
  const pdfResponse = () =>
    new Response(new Blob([PDF_BYTES], { type: 'application/pdf' }), {
      status: 200,
      headers: { 'Content-Type': 'application/pdf' },
    });

  const createObjectURL = vi.fn<(blob: Blob) => string>();
  const revokeObjectURL = vi.fn<(url: string) => void>();

  beforeEach(() => {
    let n = 0;
    createObjectURL.mockReset().mockImplementation(() => `blob:test/${++n}`);
    revokeObjectURL.mockReset();
    // jsdom には Blob URL が無い
    Object.assign(URL, { createObjectURL, revokeObjectURL });
  });
  afterEach(() => {
    Reflect.deleteProperty(URL, 'createObjectURL');
    Reflect.deleteProperty(URL, 'revokeObjectURL');
  });

  function routes(original: unknown = true, file: (call: number) => Response | Promise<Response> = pdfResponse) {
    let fileCalls = 0;
    return (url: string) => {
      if (url === '/documents') return jsonResponse(TITLED_LIST);
      if (url.endsWith('/chunks')) return jsonResponse(DETAIL(original));
      if (url.endsWith('/file')) return file(++fileCalls);
      throw new Error(`想定外の取得: ${url}`);
    };
  }

  async function openFirst(user: ReturnType<typeof userEvent.setup>) {
    await openDocuments(user);
    await user.click(await screen.findByRole('button', { name: 'つばさ大学学生寮 入寮規程 を開く' }));
  }

  it('元PDFがあれば、元PDFを先に表示する（Blob URL を iframe に渡し、AI用テキストは出さない）', async () => {
    const { calls } = installDocsFetch(routes());
    const { user } = setup();

    await openFirst(user);

    const frame = await screen.findByTitle('つばさ大学学生寮 入寮規程 の元PDF');
    expect(frame).toHaveAttribute('src', 'blob:test/1');
    expect(frame).toBeVisible();
    expect(screen.getByRole('button', { name: '元PDF' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'AI用テキスト' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
    expect(calls.map((c) => c.url)).toEqual(['/documents', '/documents/1/chunks', '/documents/1/file']);
    expect(new Headers(calls[2]!.init.headers).get('Accept')).toBe('application/pdf');
    const blob = createObjectURL.mock.calls[0]![0];
    expect(blob.type).toBe('application/pdf');
    expect(blob.size).toBe(PDF_BYTES.length);
  });

  it('「AI用テキスト」に切り替えるとチャンクが出て、「元PDF」に戻すと PDF を取り直さずに同じ表示へ戻る', async () => {
    const { calls } = installDocsFetch(routes());
    const { user } = setup();
    await openFirst(user);
    await screen.findByTitle('つばさ大学学生寮 入寮規程 の元PDF');

    await user.click(screen.getByRole('button', { name: 'AI用テキスト' }));

    expect(await screen.findAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'AI用テキスト' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTitle('つばさ大学学生寮 入寮規程 の元PDF')).not.toBeVisible();

    await user.click(screen.getByRole('button', { name: '元PDF' }));

    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
    expect(screen.getByTitle('つばさ大学学生寮 入寮規程 の元PDF')).toBeVisible();
    expect(calls.filter((c) => c.url.endsWith('/file'))).toHaveLength(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
  });

  it('Blob URL は表示中は解放せず、一覧に戻るときに解放する', async () => {
    installDocsFetch(routes());
    const { user } = setup();
    await openFirst(user);
    await screen.findByTitle('つばさ大学学生寮 入寮規程 の元PDF');
    await user.click(screen.getByRole('button', { name: 'AI用テキスト' }));
    await user.click(screen.getByRole('button', { name: '元PDF' }));
    expect(revokeObjectURL).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '資料一覧に戻る' }));

    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:test/1');
  });

  it('元PDFが無い資料は、切り替えを出さず「元PDFは利用できません」と示し、AI用テキストを出す（PDF は取りに行かない）', async () => {
    const { calls } = installDocsFetch(routes(false));
    const { user } = setup();

    await openFirst(user);

    expect(await screen.findAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByText('元PDFは利用できません。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '元PDF' })).not.toBeInTheDocument();
    expect(screen.queryByTitle(/の元PDF/)).not.toBeInTheDocument();
    expect(calls.map((c) => c.url)).toEqual(['/documents', '/documents/1/chunks']);
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('original_pdf の項目が無い応答（古いサーバー）は、元PDFなしとして読む', async () => {
    installDocsFetch(routes(OMIT));
    const { user } = setup();

    await openFirst(user);

    expect(await screen.findByText('元PDFは利用できません。')).toBeInTheDocument();
    expect(screen.getByRole('listitem')).toBeInTheDocument();
  });

  it('original_pdf が真偽値でない応答は、不正な応答として扱う', async () => {
    installDocsFetch(routes('yes'));
    const { user } = setup();

    await openFirst(user);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByTitle(/の元PDF/)).not.toBeInTheDocument();
  });

  it('PDF が取れなかった（404/409）ときは「元PDFは利用できません」と再試行を出し、AI用テキストへ切り替えて読める', async () => {
    installDocsFetch(routes(true, () => errorResponse(409, '元の PDF が、DB に保存された資料と一致しません。')));
    const { user } = setup();

    await openFirst(user);

    expect(await screen.findByRole('alert')).toHaveTextContent('元PDFは利用できません。');
    expect(screen.queryByTitle(/の元PDF/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '再試行' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'AI用テキスト' }));

    expect(await screen.findAllByRole('listitem')).toHaveLength(1);
  });

  it('PDF の取得に失敗しても、再試行で取り直せる', async () => {
    installDocsFetch(
      routes(true, (call) =>
        call === 1 ? errorResponse(503, '資料を取得できませんでした。しばらく待ってから再試行してください。') : pdfResponse(),
      ),
    );
    const { user } = setup();
    await openFirst(user);
    expect(await screen.findByRole('alert')).toHaveTextContent('資料を取得できませんでした');

    await user.click(screen.getByRole('button', { name: '再試行' }));

    expect(await screen.findByTitle('つばさ大学学生寮 入寮規程 の元PDF')).toHaveAttribute('src', 'blob:test/1');
  });

  it('PDF ではない本文が 200 で返っても、表示しない（想定外の応答）', async () => {
    installDocsFetch(routes(true, () => jsonResponse({ detail: 'x' })));
    const { user } = setup();

    await openFirst(user);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByTitle(/の元PDF/)).not.toBeInTheDocument();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('ログイン中は、PDF の取得にもトークンを付ける（401 なら 1 回更新して送り直す）', async () => {
    const auth = fakeAuth();
    const { calls } = installDocsFetch(
      routes(true, (call) =>
        call === 1 ? errorResponse(401, 'ログインが必要です。ログインし直してください。') : pdfResponse(),
      ),
    );
    const { user } = setup(auth);

    await openFirst(user);

    await screen.findByTitle('つばさ大学学生寮 入寮規程 の元PDF');
    const files = calls.filter((c) => c.url.endsWith('/file'));
    expect(files).toHaveLength(2);
    expect(new Headers(files[0]!.init.headers).get('Authorization')).toBe('Bearer tok-1');
    expect(new Headers(files[1]!.init.headers).get('Authorization')).toBe('Bearer tok-2');
    expect(auth.refreshAccessToken).toHaveBeenCalledTimes(1);
  });

  it('ログインを使わない構成では、PDF の取得にトークンを付けない', async () => {
    const { calls } = installDocsFetch(routes());
    const { user } = setup(null);

    await openFirst(user);

    await screen.findByTitle('つばさ大学学生寮 入寮規程 の元PDF');
    const file = calls.find((c) => c.url.endsWith('/file'))!;
    expect(new Headers(file.init.headers).get('Authorization')).toBeNull();
  });
});

describe('資料の詳細: 見出しと関係図の案内', () => {
  const DETAIL = { document: summary(2, 'cafe_store_guide.pdf', 30, 28), chunks: [chunk(0, '本文')], original_pdf: true };
  const routes = (url: string) => jsonResponse(url === '/documents' ? LIST : DETAIL);

  it('資料名の下に、ページ数と更新日を出す', async () => {
    installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);
    await user.click(await screen.findByRole('button', { name: 'cafe_store_guide.pdf を開く' }));

    expect(await screen.findByText('3 ページ')).toBeInTheDocument();
    expect(screen.getByText(/^更新 2026\/10\/18/)).toBeInTheDocument();
  });

  it('関係図が出ない資料には、入寮規程だけの対応であることを書く', async () => {
    installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);
    await user.click(await screen.findByRole('button', { name: 'cafe_store_guide.pdf を開く' }));

    expect(await screen.findByText(MSG_NO_GRAPH)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '関係図' })).not.toBeInTheDocument();
  });
});

describe('画面の状態は URL が持つ', () => {
  const DETAIL = { document: summary(1, 'tsubasa_dorm_rules.pdf', 3, 3), chunks: [chunk(0, '第1条 本文')] };
  const routes = (url: string) => jsonResponse(url === '/documents' ? LIST : DETAIL);

  it('資料一覧に切り替えると URL が変わり、「戻る」でチャットに戻る', async () => {
    installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);
    expect(window.location.search).toBe('?tab=documents');

    window.history.back();

    expect(await screen.findByRole('textbox', { name: 'メッセージ' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'チャット' })).toHaveAttribute('aria-current', 'page');
  });

  it('資料を開くと doc が付き、再読み込みしても同じ資料が開く。一覧に戻ると doc が外れる', async () => {
    installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);
    await user.click(await screen.findByRole('button', { name: 'tsubasa_dorm_rules.pdf を開く' }));
    expect(window.location.search).toBe('?tab=documents&doc=1');
    await screen.findByText('第1条 本文');

    // 再読み込み: 画面を作り直しても、URL から同じ画面になる
    cleanup();
    setup();
    expect(await screen.findByText('第1条 本文')).toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole('button', { name: '資料一覧に戻る' }));
    expect(window.location.search).toBe('?tab=documents');
    expect(await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ })).toBeInTheDocument();
  });

  it('URL を渡された別のセッションでも、同じ資料が開く', async () => {
    window.history.replaceState(null, '', '/?tab=documents&doc=1');
    installDocsFetch(routes);
    setup();

    expect(await screen.findByText('第1条 本文')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '資料一覧' })).toHaveAttribute('aria-current', 'page');
  });

  it('一覧に無い資料の ID は、一覧を出して、URL から外す（履歴は増やさない）', async () => {
    window.history.replaceState(null, '', '/?tab=documents&doc=99');
    const before = window.history.length;
    installDocsFetch(routes);
    setup();

    expect(await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ })).toBeInTheDocument();
    expect(window.location.search).toBe('?tab=documents');
    expect(window.history.length).toBe(before);
  });
});
