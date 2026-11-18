import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { openKnowledge, goChat } from './test/navigation';
import { AuthProvider } from './auth/AuthContext';
import {
  MSG_NOT_CONFIGURED,
  MSG_NO_DOCUMENTS,
  MSG_NO_MATCH,
  MSG_NO_MATCH_LEGACY,
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
function installDocsFetch(
  route: Route,
  collections?: () => Response | Promise<Response>,
  classifications?: { targets?: () => Response | Promise<Response>; docTypes?: () => Response | Promise<Response> },
) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'mock' });
    if (url.endsWith('/collections')) return collections ? collections() : jsonResponse({ configured: false, collections: [] });
    if (url.endsWith('/targets')) return classifications?.targets ? classifications.targets() : jsonResponse({ configured: false, targets: [] });
    if (url.endsWith('/doc-types')) return classifications?.docTypes ? classifications.docTypes() : jsonResponse({ configured: false, doc_types: [] });
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

/** 右のパネル（資料の詳細）。資料一覧は横に残るので、詳細の検査はこの中で行う。 */
async function panel() {
  return within(await screen.findByRole('complementary', { name: /^資料:/ }));
}

/** サイドバーの「資料」で、資料一覧を開く。 */
async function openDocuments(user: ReturnType<typeof userEvent.setup>) {
  await openKnowledge(user);
}

describe('資料一覧: 画面', () => {
  it('開くまでは資料を取らない。開くと、文書ごとの状態が出る（説明文は出さない）', async () => {
    const { calls } = installDocsFetch(() => jsonResponse(LIST));
    const { user } = setup();
    await screen.findByRole('textbox', { name: 'メッセージ' });
    expect(calls).toHaveLength(0);

    await openDocuments(user);

    const row = (await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ })) as HTMLElement;
    expect(within(row).queryByText('取り込み済み')).not.toBeInTheDocument(); // 取り込み済みの印は出さない（取り込み中・失敗だけ出す）
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

    await goChat(user);
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toHaveValue('書きかけの質問');
  });

  it('資料一覧への入口は、左下のメニューの「ナレッジ」だけ（上部に「チャット」「資料」の入口もタブも無い）。「新しい会話」はどちらの画面にも出る', async () => {
    installDocsFetch(() => jsonResponse(LIST));
    const { user } = setup();
    await screen.findByRole('textbox', { name: 'メッセージ' });
    expect(screen.queryByRole('navigation', { name: '画面' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '資料' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'チャット' })).not.toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: '表示する画面' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '新しい会話' })).toBeVisible();

    // ログインを使わない構成でも入口は出る（表示名とログアウトは出ない）
    await user.click(document.querySelector('.ws-account-button') as HTMLElement);
    const menu = within(await screen.findByRole('menu', { name: 'メニュー' }));
    expect(menu.getAllByRole('menuitem').map((item) => item.textContent)).toEqual(['ナレッジ']);
    await user.click(menu.getByRole('menuitem', { name: 'ナレッジ' }));

    expect(await screen.findByRole('heading', { name: 'ナレッジ' })).toBeVisible();
    expect(screen.getByRole('main', { name: 'ナレッジ' })).toBeVisible();
    expect(screen.getByRole('button', { name: '新しい会話' })).toBeVisible();
    await goChat(user);
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toBeVisible();
  });

  it('ロゴはチャットへ戻るリンク。資料一覧からも、チャットに戻る（入力途中の文は残る）', async () => {
    installDocsFetch(() => jsonResponse(LIST));
    const { user } = setup();
    await user.type(await screen.findByRole('textbox', { name: 'メッセージ' }), '書きかけ');
    await openDocuments(user);
    expect(window.location.search).toBe('?tab=documents');
    expect(await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ })).toBeInTheDocument();

    await user.click(screen.getByRole('link', { name: 'DocuNavi' }));

    expect(window.location.search).toBe('?tab=chat');
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toHaveValue('書きかけ');
  });

  it('ナレッジの右上の「× 閉じる」で、直前のチャットへ戻る。入力途中の文と回答対象は残り、フォーカスは入力欄へ移る', async () => {
    installDocsFetch(() => jsonResponse(LIST));
    const { user } = setup();
    await user.type(await screen.findByRole('textbox', { name: 'メッセージ' }), '書きかけ');
    await openDocuments(user);
    const main = screen.getByRole('main', { name: 'ナレッジ' });
    expect(within(main).getByRole('heading', { name: 'ナレッジ' })).toBeVisible();

    await user.click(within(main).getByRole('button', { name: '閉じる' }));

    expect(window.location.search).toBe('?tab=chat');
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toHaveValue('書きかけ');
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toHaveFocus();
    expect(screen.queryByRole('main', { name: 'ナレッジ' })).not.toBeInTheDocument();
  });

  it('DB が設定されていないときは、文書 0 件とは別の案内を出す', async () => {
    installDocsFetch(() => jsonResponse({ configured: false, documents: [] }));
    const { user } = setup();

    await openDocuments(user);

    expect(await screen.findByText(MSG_NOT_CONFIGURED)).toBeInTheDocument();
    expect(screen.queryByText(MSG_NO_DOCUMENTS)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'DocuNavi' })).toBeInTheDocument(); // 資料が無くても、チャットへ移る導線（ロゴ）は残る
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

    const items = await (await panel()).findAllByRole('listitem');
    expect(items).toHaveLength(3);
    // チャンク番号・埋め込みの状態（開発者向け）は出さない
    expect(items[0]).not.toHaveTextContent('#0');
    expect(items[0]).not.toHaveTextContent('埋め込み');
    expect(screen.queryByText(/チャンク（埋め込み済み/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '開発者向けの情報' })).not.toBeInTheDocument();
    expect(items[0]).toHaveTextContent('p.1');
    expect(items[0]).toHaveTextContent('第1章 総則');
    expect(items[0]!.querySelector('pre')!.textContent).toBe(
      '第1条 この規程は、つばさ大学学生寮の運営を定める。\n第2条 寮は3棟で構成する。',
    );
    expect(items[1]).toHaveTextContent('p.2–3');
    expect(items[1]).toHaveTextContent('つばさ大学 学生寮 入寮規程');
    expect(items[2]).toHaveTextContent('（見出しなし）');
    expect(calls.map((c) => c.url)).toEqual(['/documents', '/documents/1/chunks']);
  });

  it('行のどこを押しても開く', async () => {
    installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);

    const row = await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });
    await user.click(within(row).getByText('3')); // ページ数の欄

    expect(await (await panel()).findAllByRole('listitem')).toHaveLength(3);
  });

  it('本文は HTML として解釈せず、そのままの文字で出す', async () => {
    installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);

    await user.click(await screen.findByRole('button', { name: 'tsubasa_dorm_rules.pdf を開く' }));

    const items = await (await panel()).findAllByRole('listitem');
    const pre = items[2]!.querySelector('pre')!;
    expect(pre.textContent).toBe('<b>太字にならない</b> <script>alert(1)</script>');
    expect(pre.querySelector('b, script')).toBeNull();
  });

  it('「資料一覧に戻る」で一覧に戻る', async () => {
    installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);
    await user.click(await screen.findByRole('button', { name: 'cafe_store_guide.pdf を開く' }));
    await (await panel()).findAllByRole('listitem');

    await user.click(screen.getByRole('button', { name: 'パネルを閉じる' }));

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

    expect(await (await panel()).findAllByRole('listitem')).toHaveLength(3);
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

  it('題名があれば題名だけを出し（ファイル名は詳細のパネルで見る）、題名が無い・空白だけならファイル名を出す', async () => {
    installDocsFetch(() => jsonResponse(TITLED));
    const { user } = setup();
    await openDocuments(user);

    const titled = await screen.findByRole('row', { name: /入寮規程/ });
    expect(within(titled).getByText('つばさ大学学生寮 入寮規程')).toHaveClass('document-name');
    // ファイル名は一覧に出さない（詳細のパネルで見る）。同じ題名が重なるときだけ、見分けるために出す
    expect(titled.querySelector('.document-file')).toBeNull();
    const plain = screen.getByRole('row', { name: /cafe_store_guide\.pdf/ });
    expect(within(plain).getAllByText('cafe_store_guide.pdf')).toHaveLength(1);
    expect(plain.querySelector('.document-file')).toBeNull();
    const blank = screen.getByRole('row', { name: /blank_title\.pdf/ });
    expect(blank.querySelector('.document-file')).toBeNull();
  });

  it('同じ題名の資料が重なるときだけ、ファイル名を補助に出して見分ける', async () => {
    installDocsFetch(() =>
      jsonResponse({
        configured: true,
        documents: [
          summary(1, 'rules_v1.pdf', 1, 1, { title: '会員規約' }),
          summary(2, 'rules_v2.pdf', 1, 1, { title: '会員規約' }),
          summary(3, 'guide.pdf', 1, 1, { title: '店舗案内' }),
        ],
      }),
    );
    const { user } = setup();
    await openDocuments(user);

    const rows = (await screen.findAllByRole('row')).slice(1);
    expect(rows.map((row) => row.querySelector('.document-file')?.textContent ?? null)).toEqual(['rules_v1.pdf', 'rules_v2.pdf', null]);
  });

  it('「開く」ボタンの名前と詳細の見出しは題名。詳細の見出しにはファイル名を出さず、重ねたときの説明に残す', async () => {
    installDocsFetch((url) => jsonResponse(url === '/documents' ? TITLED : DETAIL));
    const { user } = setup();
    await openDocuments(user);

    await user.click(await screen.findByRole('button', { name: 'つばさ大学学生寮 入寮規程 を開く' }));

    const heading = (await panel()).getByRole('heading', { level: 2 });
    expect(heading).toHaveTextContent('つばさ大学学生寮 入寮規程');
    expect(heading).not.toHaveTextContent('tsubasa_dorm_rules.pdf');
    expect(heading).toHaveAttribute('title', 'tsubasa_dorm_rules.pdf');
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

describe('資料一覧: PDF', () => {
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

  // jsdom には Blob URL が無い。偽物は、このブロックの間ずっと入れておく（テストごとに外すと、外してから次のテストが
  // 入れ直すまでの隙間に、前の画面の PDF 取得が終わって、Node の本物の URL.createObjectURL が jsdom の Blob を受けて落ちうる）
  beforeAll(() => {
    Object.assign(URL, { createObjectURL, revokeObjectURL });
  });
  beforeEach(() => {
    let n = 0;
    createObjectURL.mockReset().mockImplementation(() => `blob:test/${++n}`);
    revokeObjectURL.mockReset();
  });
  afterAll(() => {
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

  it('PDFがあれば、PDFを先に表示する（Blob URL を iframe に渡し、抽出テキストは読み込まない）', async () => {
    const { calls } = installDocsFetch(routes());
    const { user } = setup();

    await openFirst(user);

    const frame = await screen.findByTitle('つばさ大学学生寮 入寮規程 の PDF');
    expect(frame).toHaveAttribute('src', 'blob:test/1');
    expect(frame).toBeVisible();
    expect(screen.getByRole('button', { name: 'PDF' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '抽出テキスト' })).toHaveAttribute('aria-pressed', 'false');
    expect((await panel()).queryByRole('listitem')).not.toBeInTheDocument();
    expect(calls.map((c) => c.url)).toEqual(['/documents', '/documents/1/chunks', '/documents/1/file']);
    expect(new Headers(calls[2]!.init.headers).get('Accept')).toBe('application/pdf');
    const blob = createObjectURL.mock.calls[0]![0];
    expect(blob.type).toBe('application/pdf');
    expect(blob.size).toBe(PDF_BYTES.length);
  });

  it('「抽出テキスト」に切り替えるとチャンクが出て、「PDF」に戻すと PDF を取り直さずに同じ表示へ戻る', async () => {
    const { calls } = installDocsFetch(routes());
    const { user } = setup();
    await openFirst(user);
    await screen.findByTitle('つばさ大学学生寮 入寮規程 の PDF');

    await user.click(screen.getByRole('button', { name: '抽出テキスト' }));

    expect(await (await panel()).findAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByRole('button', { name: '抽出テキスト' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTitle('つばさ大学学生寮 入寮規程 の PDF')).not.toBeVisible();

    await user.click(screen.getByRole('button', { name: 'PDF' }));

    expect((await panel()).queryByRole('listitem')).not.toBeInTheDocument();
    expect(screen.getByTitle('つばさ大学学生寮 入寮規程 の PDF')).toBeVisible();
    expect(calls.filter((c) => c.url.endsWith('/file'))).toHaveLength(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
  });

  it('Blob URL は表示中は解放せず、パネルを閉じるときに解放する', async () => {
    installDocsFetch(routes());
    const { user } = setup();
    await openFirst(user);
    await screen.findByTitle('つばさ大学学生寮 入寮規程 の PDF');
    await user.click(screen.getByRole('button', { name: '抽出テキスト' }));
    await user.click(screen.getByRole('button', { name: 'PDF' }));
    expect(revokeObjectURL).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'パネルを閉じる' }));

    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:test/1');
  });

  it('PDFが無い資料は、切り替えを出さず「PDFは利用できません」と示し、抽出テキストを出す（PDF は取りに行かない）', async () => {
    const { calls } = installDocsFetch(routes(false));
    const { user } = setup();

    await openFirst(user);

    expect(await (await panel()).findAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByText('PDFは利用できません。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'PDF' })).not.toBeInTheDocument();
    expect(screen.queryByTitle(/の PDF/)).not.toBeInTheDocument();
    expect(calls.map((c) => c.url)).toEqual(['/documents', '/documents/1/chunks']);
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('original_pdf の項目が無い応答（古いサーバー）は、PDFなしとして読む', async () => {
    installDocsFetch(routes(OMIT));
    const { user } = setup();

    await openFirst(user);

    expect(await screen.findByText('PDFは利用できません。')).toBeInTheDocument();
    expect((await panel()).getByRole('listitem')).toBeInTheDocument();
  });

  it('original_pdf が真偽値でない応答は、不正な応答として扱う', async () => {
    installDocsFetch(routes('yes'));
    const { user } = setup();

    await openFirst(user);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByTitle(/の PDF/)).not.toBeInTheDocument();
  });

  it('PDF が取れなかった（404/409）ときは「PDFは利用できません」と再試行を出し、抽出テキストへ切り替えて読める', async () => {
    installDocsFetch(routes(true, () => errorResponse(409, 'PDF が、DB に保存された資料と一致しません。')));
    const { user } = setup();

    await openFirst(user);

    expect(await screen.findByRole('alert')).toHaveTextContent('PDFは利用できません。');
    expect(screen.queryByTitle(/の PDF/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '再試行' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '抽出テキスト' }));

    expect(await (await panel()).findAllByRole('listitem')).toHaveLength(1);
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

    expect(await screen.findByTitle('つばさ大学学生寮 入寮規程 の PDF')).toHaveAttribute('src', 'blob:test/1');
  });

  it('PDF ではない本文が 200 で返っても、表示しない（想定外の応答）', async () => {
    installDocsFetch(routes(true, () => jsonResponse({ detail: 'x' })));
    const { user } = setup();

    await openFirst(user);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByTitle(/の PDF/)).not.toBeInTheDocument();
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

    await screen.findByTitle('つばさ大学学生寮 入寮規程 の PDF');
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

    await screen.findByTitle('つばさ大学学生寮 入寮規程 の PDF');
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

    const inPanel = await panel();
    expect(await inPanel.findByText('3 ページ')).toBeInTheDocument();
    expect(inPanel.getByText('3 ページ').parentElement).toHaveTextContent(/更新 2026\/10\/18/);
  });

  it('関係図の無い資料には、関係図のタブも案内の文も出さない（タブは「抽出テキスト」「PDF」だけ）', async () => {
    installDocsFetch(routes);
    const { user } = setup();
    await openDocuments(user);
    await user.click(await screen.findByRole('button', { name: 'cafe_store_guide.pdf を開く' }));

    const inPanel = await panel();
    const tabs = await inPanel.findByRole('group', { name: '表示の切り替え' });
    expect(within(tabs).getAllByRole('button').map((b) => b.textContent)).toEqual(['抽出テキスト', 'PDF']);
    expect(screen.queryByRole('button', { name: '関係図' })).not.toBeInTheDocument();
    expect(screen.queryByText(/入寮規程だけ/)).not.toBeInTheDocument();
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
    expect(screen.getByRole('button', { name: '新しい会話' })).toBeVisible(); // チャットに戻った（タブは無いので、ヘッダーの切り替わりで見る）
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

    await userEvent.setup().click(screen.getByRole('button', { name: 'パネルを閉じる' }));
    expect(window.location.search).toBe('?tab=documents');
    expect(await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ })).toBeInTheDocument();
  });

  it('URL を渡された別のセッションでも、同じ資料が開く', async () => {
    window.history.replaceState(null, '', '/?tab=documents&doc=1');
    installDocsFetch(routes);
    setup();

    expect(await screen.findByText('第1条 本文')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'ナレッジ' })).toBeVisible(); // 資料の画面（チャットではない）
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

describe('資料一覧: 対象と資料種別', () => {
  const CLASSIFIED = {
    configured: true,
    documents: [
      summary(1, 'tsubasa_dorm_rules.pdf', 3, 3, { collection: 'guides', target: 'tsubasa-dorm', doc_type: 'regulation' }),
      summary(2, 'aoba_aw820_manual.pdf', 35, 35, { collection: 'guides', target: 'aoba-aw820', doc_type: 'manual' }),
      summary(3, 'cafe_store_guide.pdf', 30, 30, { collection: 'cafe', target: 'minato-coffee', doc_type: 'store-guide' }),
      summary(4, 'ザーハード1.pdf', 4, 4, { collection: 'zahard', target: 'zahard', doc_type: 'setting' }),
      summary(5, 'loose.pdf', 1, 1, { collection: null }),
    ],
  };
  const item = (name: string, label: string, order: number, count: number) => ({
    name,
    label,
    description: '',
    sort_order: order,
    document_count: count,
    ready_count: count,
  });
  // GET /targets・GET /doc-types の応答（表示名・並び順は、サーバーが持つ）
  const TARGET_CATALOG = {
    configured: true,
    targets: [
      item('zahard', 'ザーハード', 10, 1),
      item('minato-coffee', 'みなとコーヒー', 20, 1),
      item('tsubasa-dorm', 'つばさ大学学生寮', 30, 1),
      item('aoba-aw820', 'あおば電機 AW-820', 40, 1),
    ],
  };
  const TYPE_CATALOG = {
    configured: true,
    doc_types: [item('setting', '設定資料', 10, 1), item('store-guide', '店舗案内', 20, 1), item('regulation', '規程', 30, 1), item('manual', '取扱説明書', 40, 1)],
  };
  // GET /collections の応答（旧フィルターの表示名にだけ使う）
  const GROUP_CATALOG = {
    configured: true,
    collections: [
      { name: 'cafe', label: '店舗案内（旧）', description: '', sort_order: 20, registered: true, document_count: 1, ready_count: 1 },
      { name: 'guides', label: '規程・取扱説明書（旧）', description: '', sort_order: 30, registered: true, document_count: 2, ready_count: 2 },
    ],
  };
  const targetSelect = () => screen.getByRole('combobox', { name: '対象' });
  const typeSelect = () => screen.getByRole('combobox', { name: '資料種別' });
  const names = () => screen.queryAllByRole('row').slice(1).map((row) => row.textContent ?? '');
  const install = (collections = false) =>
    installDocsFetch(
      () => jsonResponse(CLASSIFIED),
      collections ? () => jsonResponse(GROUP_CATALOG) : undefined,
      { targets: () => jsonResponse(TARGET_CATALOG), docTypes: () => jsonResponse(TYPE_CATALOG) },
    );

  it('「対象」「資料種別」の列に表示名（一覧の label）を出し、未分類の資料には「未分類」と出す。グループの列は無い', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);

    const dorm = await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(within(dorm).getByText('つばさ大学学生寮')).toBeInTheDocument();
    expect(within(dorm).getByText('規程')).toBeInTheDocument();
    const zahard = screen.getByRole('row', { name: /ザーハード1\.pdf/ });
    expect(within(zahard).getByText('ザーハード')).toBeInTheDocument();
    expect(within(zahard).getByText('設定資料')).toBeInTheDocument();
    expect(within(screen.getByRole('row', { name: /loose\.pdf/ })).getAllByText('未分類')).toHaveLength(2);
    expect(screen.queryByRole('columnheader', { name: 'グループ' })).not.toBeInTheDocument();
  });

  it('対象と資料種別の選択肢は、一覧の順に並び、件数が付く。未分類の資料があれば「未分類」で絞れる。選ぶと URL に載る', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);
    await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });

    expect(within(targetSelect()).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'すべて (5)',
      'ザーハード (1)',
      'みなとコーヒー (1)',
      'つばさ大学学生寮 (1)',
      'あおば電機 AW-820 (1)',
      '未分類 (1)',
    ]);
    expect(targetSelect()).toHaveValue('');

    await user.selectOptions(targetSelect(), 'tsubasa-dorm');
    expect(names()).toHaveLength(1);
    expect(names()[0]).toMatch(/tsubasa_dorm_rules\.pdf/);
    expect(window.location.search).toBe('?tab=documents&target=tsubasa-dorm');
    // 資料種別の件数は、対象の条件を残して数える
    expect(within(typeSelect()).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'すべて (1)',
      '設定資料 (0)',
      '店舗案内 (0)',
      '規程 (1)',
      '取扱説明書 (0)',
      '未分類 (0)',
    ]);

    await user.selectOptions(targetSelect(), '');
    await user.selectOptions(typeSelect(), '_none');
    expect(names()).toHaveLength(1);
    expect(names()[0]).toMatch(/loose\.pdf/);
    expect(window.location.search).toBe('?tab=documents&type=_none');
  });

  it('分類の一覧を使えない（旧サーバー）ときも、一覧にある識別名で選べる。固定の表示名は持たない', async () => {
    installDocsFetch(() => jsonResponse(CLASSIFIED));
    const { user } = setup();
    await openDocuments(user);
    await screen.findByRole('row', { name: /tsubasa_dorm_rules\.pdf/ });

    expect(within(targetSelect()).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'すべて (5)',
      'aoba-aw820 (1)',
      'minato-coffee (1)',
      'tsubasa-dorm (1)',
      'zahard (1)',
      '未分類 (1)',
    ]);
    await user.selectOptions(targetSelect(), 'zahard');
    expect(names()).toHaveLength(1);
  });

  it('URL の対象・資料種別で開いた共有リンクは、その資料だけを見せる。一覧に無い対象は、全件に戻さず 0 件にして、選択を残す', async () => {
    install();
    window.history.replaceState(null, '', '/?tab=documents&target=minato-coffee&type=store-guide');
    setup();
    await screen.findByRole('row', { name: /cafe_store_guide\.pdf/ });
    expect(names()).toHaveLength(1);
    expect(targetSelect()).toHaveValue('minato-coffee');
    expect(typeSelect()).toHaveValue('store-guide');

    cleanup();
    window.history.replaceState(null, '', '/?tab=documents&target=gone');
    setup();
    expect(await screen.findByText(MSG_NO_MATCH)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(targetSelect()).toHaveValue('gone');
  });

  it('旧 URL（?collection=）は黙って捨てず、「旧フィルターが有効です」と出して、その旧グループの資料だけを見せる。解除すると全件に戻る', async () => {
    install(true);
    window.history.replaceState(null, '', '/?tab=documents&collection=cafe');
    const { user } = setup();
    await screen.findByRole('row', { name: /cafe_store_guide\.pdf/ });

    const banner = screen.getByText(/旧フィルターが有効です/).closest('.legacy-filter') as HTMLElement;
    expect(within(banner).getByText(/旧グループ「店舗案内（旧）」/)).toBeInTheDocument();
    expect(names()).toHaveLength(1);

    await user.click(within(banner).getByRole('button', { name: '旧フィルターを解除' }));
    expect(screen.queryByText(/旧フィルターが有効です/)).not.toBeInTheDocument();
    expect(names()).toHaveLength(5);
    expect(window.location.search).toBe('?tab=documents');
  });

  it('旧フィルターと対象が同時にあるときは、AND で重ね、どちらも表示する。当てはまる資料が無ければ、その旨を出す', async () => {
    install(true);
    window.history.replaceState(null, '', '/?tab=documents&collection=guides&target=aoba-aw820');
    setup();
    await screen.findByRole('row', { name: /aoba_aw820_manual\.pdf/ });

    expect(names()).toHaveLength(1);
    expect(targetSelect()).toHaveValue('aoba-aw820');
    const banner = screen.getByText(/旧フィルターが有効です/).closest('.legacy-filter') as HTMLElement;
    expect(banner).toHaveTextContent('対象・資料種別の選択とあわせて');
    // 選択肢も件数も、旧フィルターで絞った資料の中で数える（cafe の資料の対象は、選択肢に出ない）
    expect(within(targetSelect()).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'すべて (2)',
      'つばさ大学学生寮 (1)',
      'あおば電機 AW-820 (1)',
    ]);

    // 旧グループ guides と、対象 minato-coffee の両方に当てはまる資料は無い（URL の指定は、全件に戻さず、0 件にして残す）
    cleanup();
    window.history.replaceState(null, '', '/?tab=documents&collection=guides&target=minato-coffee');
    setup();
    expect(await screen.findByText(MSG_NO_MATCH_LEGACY)).toBeInTheDocument();
    expect(screen.getByText(/旧フィルターが有効です/)).toBeInTheDocument();
    expect(targetSelect()).toHaveValue('minato-coffee');
  });

  it('旧フィルターに当てはまる資料が 1 件も無い名前でも、黙って全件に戻さず、旧フィルターを出す', async () => {
    install(true);
    window.history.replaceState(null, '', '/?tab=documents&collection=nothing');
    setup();
    expect(await screen.findByText(MSG_NO_MATCH_LEGACY)).toBeInTheDocument();
    expect(screen.getByText(/旧フィルターが有効です/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('資料を開いても、一覧は絞り込んだまま横に残る。戻ると、パネルだけが閉じる', async () => {
    installDocsFetch(
      (url) => jsonResponse(url === '/documents' ? CLASSIFIED : { document: CLASSIFIED.documents[2], chunks: [chunk(0, '本文')] }),
      undefined,
      { targets: () => jsonResponse(TARGET_CATALOG), docTypes: () => jsonResponse(TYPE_CATALOG) },
    );
    const { user } = setup();
    await openDocuments(user);
    await user.selectOptions(await screen.findByRole('combobox', { name: '対象' }), 'minato-coffee');

    await user.click(within(screen.getByRole('table')).getByRole('button', { name: /cafe_store_guide\.pdf を開く/ }));
    (await panel()).getByRole('heading', { level: 2 });
    expect(window.location.search).toBe('?tab=documents&target=minato-coffee&doc=3');
    expect(names()).toHaveLength(1);

    window.history.back();
    await waitFor(() => expect(window.location.search).toBe('?tab=documents&target=minato-coffee'));
    expect(screen.queryByRole('complementary', { name: /^資料:/ })).not.toBeInTheDocument();
    expect(names()).toHaveLength(1);
  });
});

describe('資料一覧: 検索・絞り込み・並び替え', () => {
  const DAY = 24 * 60 * 60 * 1000;
  // 基準時刻は 1 回だけ取る。呼び出しごとに Date.now() を読むと、同じ日数の 2 件が 1 ms ずれて並び順が入れ替わる
  const NOW = Date.now();
  const ago = (days: number) => new Date(NOW - days * DAY).toISOString();
  // サーバーの順（ファイル名順）。更新・ページ数・状態を変えてある
  const LIST_5 = {
    configured: true,
    documents: [
      summary(1, 'aoba_aw820_manual.pdf', 6, 6, { collection: 'guides', target: 'aoba-aw820', doc_type: 'manual', pages: 4, updated_at: ago(32) }),
      summary(2, 'cafe_store_guide.pdf', 5, 5, { collection: 'cafe', target: 'minato-coffee', doc_type: 'store-guide', pages: 4, updated_at: ago(14) }),
      summary(3, 'tsubasa_dorm_rules.pdf', 3, 3, { collection: 'guides', target: 'tsubasa-dorm', doc_type: 'regulation', pages: 3, title: 'つばさ大学学生寮 入寮規程', updated_at: ago(14) }),
      summary(4, 'ザーハード1.pdf', 2, 2, { collection: 'zahard', target: 'zahard', doc_type: 'setting', pages: 1, updated_at: ago(10) }),
      summary(5, 'ザーハード2.pdf', 0, 0, { collection: 'zahard', target: 'zahard', doc_type: 'setting', pages: 1, status: 'failed', updated_at: ago(1) }),
    ],
  };
  const entry = (name: string, label: string, order: number) => ({ name, label, description: '', sort_order: order, document_count: 1, ready_count: 1 });
  const TARGETS = {
    configured: true,
    targets: [entry('zahard', 'ザーハード', 10), entry('minato-coffee', 'みなとコーヒー', 20), entry('tsubasa-dorm', 'つばさ大学学生寮', 30), entry('aoba-aw820', 'あおば電機 AW-820', 40)],
  };
  const TYPES = {
    configured: true,
    doc_types: [entry('setting', '設定資料', 10), entry('store-guide', '店舗案内', 20), entry('regulation', '規程', 30), entry('manual', '取扱説明書', 40)],
  };
  const install = () =>
    installDocsFetch(() => jsonResponse(LIST_5), undefined, {
      targets: () => jsonResponse(TARGETS),
      docTypes: () => jsonResponse(TYPES),
    });
  const names = () => screen.queryAllByRole('row').slice(1).map((row) => row.textContent ?? '');
  // 各行の資料名（題名があれば題名、無ければファイル名。ファイル名は、同じ題名が重なるときだけ別に出す）
  const files = () =>
    screen
      .queryAllByRole('row')
      .slice(1)
      .map((row) => row.querySelector('.document-name')?.textContent ?? '');
  const keyword = () => screen.getByRole('searchbox', { name: /資料名で検索/ });
  const select = (name: string) => screen.getByRole('combobox', { name });
  const result = () => document.querySelector('.docs-summary');
  async function openList() {
    const { user } = setup();
    await openDocuments(user);
    await screen.findByRole('row', { name: /入寮規程/ });
    return user;
  }

  it('操作は検索・対象・資料種別・並び替えだけ（「フィルター」の開閉も、状態・ページ数・更新の欄も無い）。件数を出し、絞り込みが無ければ「条件をクリア」は出さない', async () => {
    install();
    await openList();

    expect(keyword()).toHaveValue('');
    expect(select('対象')).toHaveValue('');
    expect(select('資料種別')).toHaveValue('');
    expect(select('並び替え')).toHaveValue('default');
    expect(screen.queryByRole('button', { name: /^フィルター/ })).not.toBeInTheDocument();
    for (const name of ['状態', 'ページ数', '更新']) expect(screen.queryByRole('combobox', { name })).not.toBeInTheDocument();
    expect(result()).toHaveTextContent('5件の資料 · すべて回答対象');
    expect(names()).toHaveLength(5);
    expect(screen.queryByRole('button', { name: '条件をクリア' })).not.toBeInTheDocument();
    // 既定の並びはサーバーの順のまま
    expect(files()).toEqual(['aoba_aw820_manual.pdf', 'cafe_store_guide.pdf', 'つばさ大学学生寮 入寮規程', 'ザーハード1.pdf', 'ザーハード2.pdf']);
  });

  it('キーワードは、題名・ファイル名・対象と資料種別の表示名を探し、空白（全角も）で AND になる。打っている間は履歴を増やさず、URL に載る', async () => {
    install();
    const user = await openList();
    const length = window.history.length;

    await user.type(keyword(), '入寮');
    expect(names()).toHaveLength(1);
    expect(names()[0]).toMatch(/入寮規程/);
    expect(result()).toHaveTextContent('検索結果 1件 · 回答対象 4件');
    expect(window.location.search).toBe('?tab=documents&q=%E5%85%A5%E5%AF%AE');
    expect(window.history.length).toBe(length);
    expect(screen.getByRole('button', { name: '条件をクリア' })).toBeInTheDocument();

    await user.clear(keyword());
    expect(names()).toHaveLength(5);
    expect(window.location.search).toBe('?tab=documents');

    await user.type(keyword(), '設定資料'); // 資料種別の表示名（一覧の label）
    expect(files()).toEqual(['ザーハード1.pdf', 'ザーハード2.pdf']);

    await user.clear(keyword());
    await user.type(keyword(), '取扱　あおば'); // 全角の空白 = AND（資料種別「取扱説明書」と対象「あおば電機 AW-820」）
    expect(files()).toEqual(['aoba_aw820_manual.pdf']);
  });

  it('一致が無いときは、見つからない旨と案内を出す（一覧の表は出さない）。「条件をクリア」で全件に戻る', async () => {
    install();
    const user = await openList();

    await user.type(keyword(), 'zzzz');
    expect(result()).toHaveTextContent('検索結果 0件 · 回答対象 4件');
    expect(screen.getByText(MSG_NO_MATCH)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '条件をクリア' }));
    expect(keyword()).toHaveValue('');
    expect(names()).toHaveLength(5);
    expect(window.location.search).toBe('?tab=documents');
  });

  it('URL の状態の条件は、欄が無くても黙って絞らず、チップで見せる。×で外すと全件に戻り、URL からも消える', async () => {
    install();
    window.history.replaceState(null, '', '/?tab=documents&status=failed');
    const { user } = setup();
    await screen.findByRole('row', { name: /ザーハード2/ });

    expect(files()).toEqual(['ザーハード2.pdf']);
    const chips = within(screen.getByRole('list', { name: '有効な条件' }));
    expect(chips.getByText(/^状態：/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^状態：.* を外す$/ }));
    expect(names()).toHaveLength(5);
    expect(window.location.search).toBe('?tab=documents');
  });

  it('並び替えは、既定ではサーバーの順で、選ぶと並び替わり URL に載る。既定に戻すと URL から外れる', async () => {
    install();
    const user = await openList();

    await user.selectOptions(select('並び替え'), 'updated');
    expect(files()).toEqual(['ザーハード2.pdf', 'ザーハード1.pdf', 'cafe_store_guide.pdf', 'つばさ大学学生寮 入寮規程', 'aoba_aw820_manual.pdf']);
    expect(window.location.search).toBe('?tab=documents&sort=updated');
    // 並び替えでは件数も絞り込みの有無も変わらない
    expect(result()).toHaveTextContent('5件の資料 · すべて回答対象');
    expect(screen.queryByRole('button', { name: '条件をクリア' })).not.toBeInTheDocument();

    await user.selectOptions(select('並び替え'), 'pages');
    expect(files().slice(0, 3)).toEqual(['aoba_aw820_manual.pdf', 'cafe_store_guide.pdf', 'つばさ大学学生寮 入寮規程']);

    await user.selectOptions(select('並び替え'), 'default');
    expect(files()[0]).toBe('aoba_aw820_manual.pdf');
    expect(window.location.search).toBe('?tab=documents');
  });

  it('URL の条件で開いた共有リンクは、その条件で絞り・並べ、入力欄とチップにも出る。旧フィルターとも併用できる', async () => {
    install();
    window.history.replaceState(null, '', '/?tab=documents&collection=guides&q=%E5%AF%AE&status=ready&sort=name');
    setup();
    await screen.findByRole('row', { name: /入寮規程/ });

    expect(keyword()).toHaveValue('寮');
    // 状態の条件は欄が無いので、チップで見せる（見えない条件で絞られた一覧にしない）
    expect(within(screen.getByRole('list', { name: '有効な条件' })).getByText(/^状態：/)).toBeInTheDocument();
    expect(select('並び替え')).toHaveValue('name');
    expect(files()).toEqual(['つばさ大学学生寮 入寮規程']);
  });

  it('対象・資料種別も、「条件をクリア」で外れる。並び替えと旧フィルターは残す', async () => {
    install();
    window.history.replaceState(null, '', '/?tab=documents&collection=guides&target=aoba-aw820&type=manual&sort=name');
    setup();
    await screen.findByRole('row', { name: /aoba_aw820_manual\.pdf/ });

    await userEvent.setup().click(screen.getByRole('button', { name: '条件をクリア' }));
    expect(files()).toEqual(['aoba_aw820_manual.pdf', 'つばさ大学学生寮 入寮規程']);
    expect(window.location.search).toBe('?tab=documents&collection=guides&sort=name');
  });

  it('有効な条件は、チップでいつも見える。×でその条件だけ外せる。対象を 1 つ選んでいる間は、対象の列を出さない', async () => {
    install();
    window.history.replaceState(null, '', '/?tab=documents&target=zahard&type=setting');
    const user = userEvent.setup();
    setup();
    await screen.findByRole('row', { name: /ザーハード1/ });

    const chips = within(screen.getByRole('list', { name: '有効な条件' })).getAllByRole('listitem');
    expect(chips.map((c) => c.textContent)).toEqual(['対象：ザーハード×', '資料種別：設定資料×']);
    expect(screen.getAllByRole('columnheader').map((th) => th.textContent)).toEqual(['', '資料名', '資料種別', 'ページ数', '更新日時']); // 先頭は回答に使う資料のチェック列

    await user.click(screen.getByRole('button', { name: '対象：ザーハード を外す' }));

    expect(window.location.search).toBe('?tab=documents&type=setting');
    expect(screen.getAllByRole('columnheader').map((th) => th.textContent)).toEqual(['', '資料名', '対象', '資料種別', 'ページ数', '更新日時']);
    expect(within(screen.getByRole('list', { name: '有効な条件' })).getAllByRole('listitem')).toHaveLength(1);
  });

  it('「条件をクリア」は、グループの絞り込みと並び替えは残す', async () => {
    install();
    window.history.replaceState(null, '', '/?tab=documents&collection=guides&q=%E5%85%A5%E5%AF%AE&sort=name');
    setup();
    await screen.findByRole('row', { name: /入寮規程/ });
    expect(names()).toHaveLength(1);

    await userEvent.setup().click(screen.getByRole('button', { name: '条件をクリア' }));
    expect(files()).toEqual(['aoba_aw820_manual.pdf', 'つばさ大学学生寮 入寮規程']);
    expect(window.location.search).toBe('?tab=documents&collection=guides&sort=name');
  });

  it('資料を開いても一覧の条件は URL に残り、ブラウザーの「戻る」で、パネルだけが閉じる', async () => {
    install();
    const user = await openList();
    await user.type(keyword(), '入寮');
    const listSearch = window.location.search;
    expect(listSearch).toBe('?tab=documents&q=%E5%85%A5%E5%AF%AE');

    await user.click(screen.getByRole('button', { name: 'つばさ大学学生寮 入寮規程 を開く' }));
    expect(window.location.search).toBe(`${listSearch}&doc=3`);
    expect(screen.getByRole('searchbox', { name: /資料名で検索/ })).toHaveValue('入寮');

    window.history.back();
    await waitFor(() => expect(window.location.search).toBe(listSearch));
    expect(await screen.findByRole('searchbox', { name: /資料名で検索/ })).toHaveValue('入寮');
    expect(names()).toHaveLength(1);
  });
});

describe('資料一覧: ヒーローは出さず、最近更新した資料は並び替えで探す', () => {
  const LIST_3 = {
    configured: true,
    documents: [
      summary(1, 'a.pdf', 1, 1, { updated_at: '2026-10-01T03:00:00+00:00', hero_image: true }),
      summary(2, 'b.pdf', 1, 1, { updated_at: '2026-10-07T03:00:00+00:00', title: 'B 店の案内', hero_image: true }),
      summary(3, 'c.pdf', 1, 1, { updated_at: null }),
    ],
  };

  it('「最近更新された資料」のカルーセルを出さず、ヒーロー画像（/documents/{id}/hero）も取りに行かない（画像のある資料でも）', async () => {
    const { calls } = installDocsFetch(() => jsonResponse(LIST_3));
    const { user } = setup();
    await openDocuments(user);
    await screen.findByRole('row', { name: /a\.pdf/ });
    // 画像の取得は、遅れて始まることもある。次の描画まで待ってから確かめる
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(screen.queryByRole('region', { name: '最近更新された資料' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /資料を開く/ })).not.toBeInTheDocument();
    expect(calls.map((c) => c.url).filter((url) => url.includes('/hero'))).toEqual([]);
  });

  it('更新の新しい順は、並び替え「更新が新しい順」で探せる。更新日が無い資料は最後', async () => {
    installDocsFetch(() => jsonResponse(LIST_3));
    const { user } = setup();
    await openDocuments(user);
    await screen.findByRole('row', { name: /a\.pdf/ });

    await user.selectOptions(screen.getByRole('combobox', { name: '並び替え' }), 'updated');

    const files = screen.getAllByRole('row').slice(1).map((row) => row.querySelector('.document-name')?.textContent);
    expect(files).toEqual(['B 店の案内', 'a.pdf', 'c.pdf']);
  });
});

describe('資料一覧: テーブル', () => {
  const DAY = 24 * 60 * 60 * 1000;
  const TABLE_LIST = {
    configured: true,
    documents: [
      summary(1, 'a.pdf', 4, 4, { title: 'A の資料', collection: 'guides', target: 'tsubasa-dorm', doc_type: 'regulation', pages: 6, updated_at: new Date(Date.now() - DAY).toISOString() }),
      summary(2, 'b.pdf', 0, 0, { collection: null, pages: null, updated_at: null, status: 'failed', failure_reason: '読み取れませんでした' }),
    ],
  };
  const classifications = {
    targets: () => jsonResponse({ configured: true, targets: [{ name: 'tsubasa-dorm', label: 'つばさ大学学生寮', description: '', sort_order: 1, document_count: 1, ready_count: 1 }] }),
    docTypes: () => jsonResponse({ configured: true, doc_types: [{ name: 'regulation', label: '規程', description: '', sort_order: 1, document_count: 1, ready_count: 1 }] }),
  };

  it('列は、API にある情報だけ（資料名・対象・資料種別・状態・ページ数・更新日時）。チャンク数の列や、API に無い追加日時・サイズは作らない', async () => {
    installDocsFetch(() => jsonResponse(TABLE_LIST), undefined, classifications);
    const { user } = setup();
    await openDocuments(user);
    await screen.findByRole('row', { name: /A の資料/ });

    const head = screen.getAllByRole('columnheader').map((th) => th.textContent);
    expect(head).toEqual(['', '資料名', '対象', '資料種別', 'ページ数', '更新日時']);

    const row = screen.getByRole('row', { name: /A の資料/ });
    const cells = within(row).getAllByRole('cell');
    expect(cells.map((c) => c.textContent)).toEqual(['', 'つばさ大学学生寮', '規程', '6', expect.stringMatching(/^\d{4}\/\d{2}\/\d{2}$/)]);
    // 資料名のボタンが、詳細を開く入口。ファイル名は一覧に出さず、詳細のパネルで見る。取り込み済みの印も出さない
    expect(within(row).getByRole('button', { name: 'A の資料 を開く' })).not.toHaveTextContent('a.pdf');
    expect(within(row).queryByText('取り込み済み')).not.toBeInTheDocument();
  });

  it('更新日時やページ数の記録が無い資料は、欠けていることが分かる表示にする（日時を作らない）。失敗の理由は状態の下に出す', async () => {
    installDocsFetch(() => jsonResponse(TABLE_LIST), undefined, classifications);
    const { user } = setup();
    await openDocuments(user);
    const row = await screen.findByRole('row', { name: /b\.pdf/ });

    expect(within(row).getByText('記録なし')).toHaveAttribute('title', 'この資料には、更新日時の記録がありません');
    expect(within(row).getByText('—')).toHaveAttribute('title', 'ページ数の記録がありません');
    expect(within(row).getAllByText('未分類')).toHaveLength(2);
    expect(within(row).getByText('取り込み失敗')).toBeInTheDocument();
    expect(within(row).getByText('読み取れませんでした')).toBeInTheDocument();
    expect(row.querySelector('time')).toBeNull();
  });
});
