import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { AuthProvider } from './auth/AuthContext';
import {
  MSG_NOT_CONFIGURED,
  MSG_NO_DOCUMENTS,
  MSG_NO_DOCUMENTS_IN_GROUP,
  MSG_NO_GRAPH,
  MSG_NO_MATCH,
} from './components/DocumentsView';
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

/**
 * /health と /documents 系を差し替える。/chat/stream には何も返さない（呼ばれたら失敗させる）。
 * 資料グループの一覧（/collections）は、渡さなければ「DB を使っていない」を返す（呼び出しの回数には数えない）。
 */
function installDocsFetch(route: Route, collections?: () => Response | Promise<Response>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'mock' });
    if (url.endsWith('/collections')) return collections ? collections() : jsonResponse({ configured: false, collections: [] });
    if (url.endsWith('/threads')) return jsonResponse({ threads: [] }); // 会話の一覧（ログイン中だけ）。資料の呼び出しには数えない
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

describe('資料一覧: 種別（資料群）', () => {
  const GROUPED = {
    configured: true,
    documents: [
      summary(1, 'tsubasa_dorm_rules.pdf', 3, 3, { collection: 'guides' }),
      summary(2, 'aoba_aw820_manual.pdf', 35, 35, { collection: 'guides' }),
      summary(3, 'cafe_store_guide.pdf', 30, 30, { collection: 'cafe' }),
      summary(4, 'ザーハード1.pdf', 4, 4, { collection: 'zahard' }),
      summary(5, 'loose.pdf', 1, 1, { collection: null }),
    ],
  };
  // GET /collections の応答（表示名・説明・並び順は、サーバーが持つ）
  const GROUP_CATALOG = {
    configured: true,
    collections: [
      { name: 'zahard', label: '設定資料', description: 'ザーハードに関する設定資料です。', sort_order: 10, registered: true, document_count: 1, ready_count: 1 },
      { name: 'cafe', label: '店舗案内', description: '', sort_order: 20, registered: true, document_count: 1, ready_count: 1 },
      { name: 'guides', label: '規程・取扱説明書', description: '', sort_order: 30, registered: true, document_count: 2, ready_count: 2 },
    ],
  };
  const filter = () => screen.getByRole('group', { name: '種別で絞り込み' });
  const names = () => screen.getAllByRole('row').slice(1).map((row) => row.textContent ?? '');

  it('カードに種別の表示名（グループの一覧の label）を出し、未設定の資料には出さない', async () => {
    installDocsFetch(
      () => jsonResponse(GROUPED),
      () => jsonResponse(GROUP_CATALOG),
    );
    const { user } = setup();
    await openDocuments(user);

    const dorm = await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(within(dorm).getByText('規程・取扱説明書')).toBeInTheDocument();
    expect(within(screen.getByRole('row', { name: /ザーハード1\.pdf/ })).getByText('設定資料')).toBeInTheDocument();
    expect(within(screen.getByRole('row', { name: /cafe_store_guide\.pdf/ })).getByText('店舗案内')).toBeInTheDocument();
    const loose = screen.getByRole('row', { name: /loose\.pdf/ });
    expect(loose.querySelector('td[data-label="種別"]')?.textContent).toBe('');
  });

  it('グループの一覧を使えない（未設定）ときの絞り込みは、一覧にある資料群を名前順で出し、押すとその資料群だけを残す。URL にも載る', async () => {
    installDocsFetch(() => jsonResponse(GROUPED));
    const { user } = setup();
    await openDocuments(user);
    await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });

    // 固定の表示名は持たない。グループの一覧が無いときは、識別名のまま出す
    expect(within(filter()).getAllByRole('button').map((b) => b.textContent)).toEqual(['すべて', 'cafe', 'guides', 'zahard']);
    expect(within(filter()).getByRole('button', { name: 'すべて' })).toHaveAttribute('aria-pressed', 'true');
    expect(names()).toHaveLength(5);

    await user.click(within(filter()).getByRole('button', { name: 'guides' }));

    expect(names().join('|')).toMatch(/tsubasa_dorm_rules\.pdf.*\|.*aoba_aw820_manual\.pdf/);
    expect(names()).toHaveLength(2);
    expect(window.location.search).toBe('?tab=documents&collection=guides');
    expect(within(filter()).getByRole('button', { name: 'guides' })).toHaveAttribute('aria-pressed', 'true');

    await user.click(within(filter()).getByRole('button', { name: 'すべて' }));
    expect(names()).toHaveLength(5);
    expect(window.location.search).toBe('?tab=documents');
  });

  it('URL の絞り込みで開いた共有リンクは、その資料群だけを見せる。一覧に無い名前は絞り込みなしと同じ', async () => {
    installDocsFetch(() => jsonResponse(GROUPED));
    window.history.replaceState(null, '', '/?tab=documents&collection=cafe');
    setup();
    await screen.findByRole('row', { name: /cafe_store_guide\.pdf/ });
    expect(names()).toHaveLength(1);

    cleanup();
    window.history.replaceState(null, '', '/?tab=documents&collection=nothing');
    setup();
    await screen.findByRole('row', { name: /cafe_store_guide\.pdf/ });
    expect(names()).toHaveLength(5);
    expect(within(filter()).getByRole('button', { name: 'すべて' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('資料群が 1 つも無い（古いサーバー・未設定）ときは、絞り込みを出さない', async () => {
    installDocsFetch(() => jsonResponse(LIST));
    const { user } = setup();
    await openDocuments(user);
    await screen.findAllByRole('row', { name: /pdf/ });
    expect(screen.queryByRole('group', { name: '種別で絞り込み' })).not.toBeInTheDocument();
  });

  it('資料を開いて戻ると、絞り込みが残る', async () => {
    installDocsFetch((url) =>
      jsonResponse(url === '/documents' ? GROUPED : { document: GROUPED.documents[2], chunks: [chunk(0, '本文')] }),
    );
    const { user } = setup();
    await openDocuments(user);
    await user.click(await within(await screen.findByRole('group', { name: '種別で絞り込み' })).findByRole('button', { name: 'cafe' }));

    // 同じ資料は、最近更新のヒーローにも出る。一覧の表のボタンを押す
    await user.click(within(screen.getByRole('table')).getByRole('button', { name: /cafe_store_guide\.pdf を開く/ }));
    await screen.findByRole('heading', { level: 2 });
    expect(window.location.search).toContain('doc=3');
    expect(window.location.search).not.toContain('collection');

    window.history.back();
    await waitFor(() => expect(window.location.search).toBe('?tab=documents&collection=cafe'));
    expect(names()).toHaveLength(1);
  });
});

describe('資料一覧: 検索・絞り込み・並び替え', () => {
  const DAY = 24 * 60 * 60 * 1000;
  const ago = (days: number) => new Date(Date.now() - days * DAY).toISOString();
  // サーバーの順（ファイル名順）。更新・ページ数・状態を変えてある
  const LIST_5 = {
    configured: true,
    documents: [
      summary(1, 'aoba_aw820_manual.pdf', 6, 6, { collection: 'guides', pages: 4, updated_at: ago(32) }),
      summary(2, 'cafe_store_guide.pdf', 5, 5, { collection: 'cafe', pages: 4, updated_at: ago(14) }),
      summary(3, 'tsubasa_dorm_rules.pdf', 3, 3, { collection: 'guides', pages: 3, title: 'つばさ大学学生寮 入寮規程', updated_at: ago(14) }),
      summary(4, 'ザーハード1.pdf', 2, 2, { collection: 'zahard', pages: 1, updated_at: ago(10) }),
      summary(5, 'ザーハード2.pdf', 0, 0, { collection: 'zahard', pages: 1, status: 'failed', updated_at: ago(1) }),
    ],
  };
  const CATALOG = {
    configured: true,
    collections: [
      { name: 'zahard', label: '設定資料', description: '', sort_order: 10, registered: true, document_count: 2, ready_count: 1 },
      { name: 'cafe', label: '店舗案内', description: '', sort_order: 20, registered: true, document_count: 1, ready_count: 1 },
      { name: 'guides', label: '規程・取扱説明書', description: '', sort_order: 30, registered: true, document_count: 2, ready_count: 2 },
      { name: 'draft', label: '準備中の資料', description: '', sort_order: 40, registered: true, document_count: 0, ready_count: 0 },
    ],
  };
  const install = () =>
    installDocsFetch(
      () => jsonResponse(LIST_5),
      () => jsonResponse(CATALOG),
    );
  const names = () => screen.queryAllByRole('row').slice(1).map((row) => row.textContent ?? '');
  // 各行のファイル名（題名があるときは補助の行、無ければ文書名がファイル名）
  const files = () =>
    screen
      .queryAllByRole('row')
      .slice(1)
      .map((row) => (row.querySelector('.document-file') ?? row.querySelector('.document-name'))?.textContent ?? '');
  const keyword = () => screen.getByRole('searchbox', { name: /キーワード/ });
  const select = (name: string) => screen.getByRole('combobox', { name });
  const result = () => screen.getByRole('status');

  async function openList() {
    const { user } = setup();
    await openDocuments(user);
    await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });
    return user;
  }

  it('種別のカードの下に、キーワード・状態・ページ数・更新・並び替えと、件数を出す。絞り込みが無ければ「条件をクリア」は出さない', async () => {
    install();
    await openList();

    expect(keyword()).toHaveValue('');
    expect(select('状態')).toHaveValue('');
    expect(select('並び替え')).toHaveValue('default');
    expect(result()).toHaveTextContent('5 件の資料が見つかりました');
    expect(names()).toHaveLength(5);
    expect(screen.queryByRole('button', { name: '条件をクリア' })).not.toBeInTheDocument();
    // 既定の並びはサーバーの順のまま
    expect(files()).toEqual(['aoba_aw820_manual.pdf', 'cafe_store_guide.pdf', 'tsubasa_dorm_rules.pdf', 'ザーハード1.pdf', 'ザーハード2.pdf']);
  });

  it('キーワードは、題名・ファイル名・種別の表示名を探し、空白（全角も）で AND になる。打っている間は履歴を増やさず、URL に載る', async () => {
    install();
    const user = await openList();
    const length = window.history.length;

    await user.type(keyword(), '入寮');
    expect(names()).toHaveLength(1);
    expect(names()[0]).toMatch(/tsubasa_dorm_rules\.pdf/);
    expect(result()).toHaveTextContent('1 件の資料が見つかりました');
    expect(window.location.search).toBe('?tab=documents&q=%E5%85%A5%E5%AF%AE');
    expect(window.history.length).toBe(length);
    expect(screen.getByRole('button', { name: '条件をクリア' })).toBeInTheDocument();

    await user.clear(keyword());
    expect(names()).toHaveLength(5);
    expect(window.location.search).toBe('?tab=documents');

    await user.type(keyword(), '設定資料'); // 種別の表示名（グループの一覧の label）
    expect(files()).toEqual(['ザーハード1.pdf', 'ザーハード2.pdf']);

    await user.clear(keyword());
    await user.type(keyword(), '規程　取扱'); // 全角の空白 = AND（種別「規程・取扱説明書」）
    expect(files()).toEqual(['aoba_aw820_manual.pdf', 'tsubasa_dorm_rules.pdf']);
  });

  it('一致が無いときは、見つからない旨と案内を出す（一覧の表は出さない）。「条件をクリア」で全件に戻る', async () => {
    install();
    const user = await openList();

    await user.type(keyword(), 'zzzz');
    expect(result()).toHaveTextContent('該当する資料が見つかりませんでした');
    expect(screen.getByText(MSG_NO_MATCH)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '条件をクリア' }));
    expect(keyword()).toHaveValue('');
    expect(names()).toHaveLength(5);
    expect(window.location.search).toBe('?tab=documents');
  });

  it('状態・ページ数・更新は、選ぶと絞り、選択肢に件数が付く。URL に載り、戻るで元に戻る', async () => {
    install();
    const user = await openList();

    expect(within(select('状態')).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'すべて (5)',
      '取り込み済み (4)',
      '取り込み失敗 (1)',
    ]);
    expect(within(select('ページ数')).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'すべて (5)',
      '1ページ (2)',
      '2〜4ページ (3)',
      '5ページ以上 (0)',
    ]);

    await user.selectOptions(select('状態'), 'failed');
    expect(files()).toEqual(['ザーハード2.pdf']);
    expect(window.location.search).toBe('?tab=documents&status=failed');
    // 件数は、その選択肢以外の条件で数える（状態を選んだあとのページ数は、失敗した 1 件の分）
    expect(within(select('ページ数')).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'すべて (1)',
      '1ページ (1)',
      '2〜4ページ (0)',
      '5ページ以上 (0)',
    ]);

    await user.selectOptions(select('更新'), 'older');
    expect(names()).toHaveLength(0); // 失敗した資料は 1 日前の更新

    window.history.back();
    await waitFor(() => expect(window.location.search).toBe('?tab=documents&status=failed'));
    expect(files()).toEqual(['ザーハード2.pdf']);
    window.history.back();
    await waitFor(() => expect(window.location.search).toBe('?tab=documents'));
    expect(names()).toHaveLength(5);
    expect(select('状態')).toHaveValue('');
  });

  it('並び替えは、既定ではサーバーの順で、選ぶと並び替わり URL に載る。既定に戻すと URL から外れる', async () => {
    install();
    const user = await openList();

    await user.selectOptions(select('並び替え'), 'updated');
    expect(files()).toEqual(['ザーハード2.pdf', 'ザーハード1.pdf', 'cafe_store_guide.pdf', 'tsubasa_dorm_rules.pdf', 'aoba_aw820_manual.pdf']);
    expect(window.location.search).toBe('?tab=documents&sort=updated');
    // 並び替えでは件数も絞り込みの有無も変わらない
    expect(result()).toHaveTextContent('5 件の資料が見つかりました');
    expect(screen.queryByRole('button', { name: '条件をクリア' })).not.toBeInTheDocument();

    await user.selectOptions(select('並び替え'), 'pages');
    expect(files().slice(0, 3)).toEqual(['aoba_aw820_manual.pdf', 'cafe_store_guide.pdf', 'tsubasa_dorm_rules.pdf']);

    await user.selectOptions(select('並び替え'), 'default');
    expect(files()[0]).toBe('aoba_aw820_manual.pdf');
    expect(window.location.search).toBe('?tab=documents');
  });

  it('URL の条件で開いた共有リンクは、その条件で絞り・並べ、入力欄にも出る。種別の絞り込みとも併用できる', async () => {
    install();
    window.history.replaceState(null, '', '/?tab=documents&collection=guides&q=%E8%A6%8F%E7%A8%8B&status=ready&sort=name');
    setup();
    await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });

    expect(keyword()).toHaveValue('規程');
    expect(select('状態')).toHaveValue('ready');
    expect(select('並び替え')).toHaveValue('name');
    expect(files()).toEqual(['aoba_aw820_manual.pdf', 'tsubasa_dorm_rules.pdf']);
  });

  it('「条件をクリア」は、種別（カード）の絞り込みと並び替えは残す', async () => {
    install();
    window.history.replaceState(null, '', '/?tab=documents&collection=guides&q=%E5%85%A5%E5%AF%AE&sort=name');
    setup();
    await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(names()).toHaveLength(1);

    await userEvent.setup().click(screen.getByRole('button', { name: '条件をクリア' }));
    expect(files()).toEqual(['aoba_aw820_manual.pdf', 'tsubasa_dorm_rules.pdf']);
    expect(window.location.search).toBe('?tab=documents&collection=guides&sort=name');
  });

  it('資料が 1 件も無いグループでは、検索の欄を出さず、これまでの案内だけを出す', async () => {
    install();
    window.history.replaceState(null, '', '/?tab=documents&collection=draft');
    setup();
    expect(await screen.findByText(MSG_NO_DOCUMENTS_IN_GROUP)).toBeInTheDocument();
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument();
  });

  it('資料を開くと一覧の条件は URL から外れ、ブラウザーの「戻る」で、条件つきの一覧に戻る', async () => {
    window.history.replaceState(null, '', '/');
    install();
    const user = await openList();
    await user.type(keyword(), '入寮');
    const listSearch = window.location.search;
    expect(listSearch).toBe('?tab=documents&q=%E5%85%A5%E5%AF%AE');

    await user.click(screen.getByRole('button', { name: 'つばさ大学学生寮 入寮規程 を開く' }));
    expect(window.location.search).toBe('?tab=documents&doc=3');

    window.history.back();
    await waitFor(() => expect(window.location.search).toBe(listSearch));
    expect(await screen.findByRole('searchbox', { name: /キーワード/ })).toHaveValue('入寮');
    expect(names()).toHaveLength(1);
  });
});

describe('資料一覧: 最近更新された資料（ヒーローカルーセル）', () => {
  // サーバーの順（ファイル名順）とは別に、更新日の新しい順に並ぶことを確かめる。7 冊のうち 1 冊は更新日なし
  const LIST_7 = {
    configured: true,
    documents: [
      summary(1, 'a.pdf', 1, 1, { collection: 'guides', updated_at: '2026-10-01T03:00:00+00:00' }),
      summary(2, 'b.pdf', 1, 1, { collection: 'cafe', updated_at: '2026-10-07T03:00:00+00:00', title: 'B 店の案内' }),
      summary(3, 'c.pdf', 1, 1, { collection: 'zahard', updated_at: '2026-10-03T03:00:00+00:00' }),
      summary(4, 'd.pdf', 1, 1, { collection: 'guides', updated_at: '2026-10-06T03:00:00+00:00' }),
      summary(5, 'e.pdf', 1, 1, { collection: 'cafe', updated_at: '2026-10-02T03:00:00+00:00' }),
      summary(6, 'f.pdf', 1, 1, { collection: 'zahard', updated_at: '2026-10-05T03:00:00+00:00' }),
      summary(7, 'g.pdf', 1, 1, { collection: 'guides', updated_at: null }),
    ],
  };
  const CATALOG = {
    configured: true,
    collections: [
      { name: 'zahard', label: '設定資料', description: '', sort_order: 10, registered: true, document_count: 2, ready_count: 2 },
      { name: 'cafe', label: '店舗案内', description: '', sort_order: 20, registered: true, document_count: 2, ready_count: 2 },
      { name: 'guides', label: '規程・取扱説明書', description: '', sort_order: 30, registered: true, document_count: 3, ready_count: 3 },
    ],
  };

  const install = (list: object = LIST_7) =>
    installDocsFetch(
      (url) => jsonResponse(url === '/documents' ? list : { document: LIST_7.documents[1], chunks: [chunk(0, '本文')] }),
      () => jsonResponse(CATALOG),
    );
  const carousel = () => screen.findByRole('region', { name: '最近更新された資料' });
  // 見えている 1 枚（見えていないスライドは aria-hidden なので、ロールでは取れない）
  const shownSlide = (region: HTMLElement) => within(region).getByRole('group', { name: /^\d+ \/ \d+$/ });
  const position = (region: HTMLElement) => within(region).getByText(/^\d+ \/ \d+$/).textContent;

  it('一覧の一番上（種別のカードより前）に、更新が新しい順の 5 件を 1 枚ずつ出す。更新日のない資料は入れない', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);
    const region = await carousel();

    expect(position(region)).toBe('1 / 5');
    const names = Array.from(region.querySelectorAll('.hero-title'), (el) => el.textContent);
    expect(names).toEqual(['B 店の案内', 'd.pdf', 'f.pdf', 'c.pdf', 'e.pdf']);
    const group = screen.getByRole('group', { name: /資料グループ/ });
    expect(region.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // ヒーローは、一覧の表とは別。一覧には 7 冊すべてが残る
    await screen.findByRole('row', { name: /g\.pdf/ });
    expect(screen.getAllByRole('row').slice(1)).toHaveLength(7);
  });

  it('見えている 1 枚に、文書名・種別・更新日を出す。画像が無いので灰色の代替表示', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);
    const slide = shownSlide(await carousel());

    expect(slide).toHaveTextContent('B 店の案内');
    expect(slide).toHaveTextContent('店舗案内');
    expect(slide).toHaveTextContent('更新 2026/10/07');
    expect(slide.querySelector('img')).toBeNull();
    expect(slide.querySelector('.hero-media')).not.toBeNull();
  });

  it('「資料を開く」を押すと、その資料の詳細へ移る', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);

    await user.click(within(await carousel()).getByRole('button', { name: '資料を開く：B 店の案内' }));

    await screen.findByRole('heading', { level: 2 });
    expect(window.location.search).toContain('doc=2');
  });

  it('種別のカードや検索・絞り込みには連動しない', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);
    const region = await carousel();

    await user.click(within(screen.getByRole('group', { name: /資料グループ/ })).getByRole('button', { name: /^設定資料\s*資料/ }));
    await user.type(await screen.findByRole('searchbox', { name: /キーワード/ }), 'zzz');

    expect(screen.getByText(MSG_NO_MATCH)).toBeInTheDocument();
    const after = await carousel();
    expect(position(after)).toBe('1 / 5');
    expect(shownSlide(after)).toHaveTextContent('B 店の案内');
    expect(after).toBe(region);
  });

  it('矢印で 1 枚ずつ動かす（端では反対側へ回る）', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);
    const region = await carousel();

    await user.click(within(region).getByRole('button', { name: '次の資料へ' }));
    expect(position(region)).toBe('2 / 5');
    expect(shownSlide(region)).toHaveTextContent('d.pdf');

    await user.click(within(region).getByRole('button', { name: '前の資料へ' }));
    await user.click(within(region).getByRole('button', { name: '前の資料へ' }));
    expect(position(region)).toBe('5 / 5');
  });

  it('資料が 1 件だけなら、動かす操作は出さない', async () => {
    install({ configured: true, documents: LIST_7.documents.slice(1, 2) });
    const { user } = setup();
    await openDocuments(user);
    const region = await carousel();

    expect(shownSlide(region)).toHaveTextContent('B 店の案内');
    expect(within(region).queryByRole('button', { name: '次の資料へ' })).not.toBeInTheDocument();
  });

  it('更新日のある資料が無ければ、カルーセルは出さない', async () => {
    install({ configured: true, documents: [summary(1, 'a.pdf', 1, 1, { updated_at: null })] });
    const { user } = setup();
    await openDocuments(user);

    await screen.findByRole('row', { name: /a\.pdf/ });
    expect(screen.queryByRole('region', { name: '最近更新された資料' })).not.toBeInTheDocument();
  });
});
