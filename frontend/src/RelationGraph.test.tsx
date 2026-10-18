import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { AuthProvider } from './auth/AuthContext';
import { MSG_GRAPH_NOTE, MSG_NO_IMPACT, MSG_NO_SELECTION } from './components/RelationGraphView';
import { errorResponse, jsonResponse } from './test/helpers';

// jsdom には、React Flow が要る描画の機能（ResizeObserver など）が無い。図そのものは、同じ props を受け取る
// 簡単な部品に差し替え、図に何を渡したか（描くノード・影響の分類）と、押したときの選択を確かめる。
// 図の見た目は、実際のブラウザーで確かめる。
vi.mock('./components/RelationCanvas', async () => {
  const { createElement } = await import('react');
  return {
    default: (props: {
      placed: Array<{ node: { id: string; label: string } }>;
      kinds: Map<string, string>;
      walked: Set<string>;
      selected: string | null;
      onSelect: (id: string) => void;
    }) =>
      createElement(
        'div',
        { 'data-testid': 'canvas', 'data-selected': props.selected ?? '', 'data-walked': [...props.walked].join(',') },
        props.placed.map(({ node }) =>
          createElement(
            'button',
            { key: node.id, type: 'button', onClick: () => props.onSelect(node.id) },
            `${node.label}|${props.kinds.get(node.id) ?? '-'}`,
          ),
        ),
      ),
  };
});

const DORM = 'tsubasa_dorm_rules.pdf';

function summary(id: number, source: string, extra: object = {}) {
  return {
    id,
    source,
    status: 'ready',
    failure_reason: null,
    pages: 3,
    updated_at: '2026-10-18T00:00:00+00:00',
    chunk_count: 1,
    embedded_count: 1,
    ...extra,
  };
}

const LIST = {
  configured: true,
  documents: [
    summary(3, DORM, { title: 'つばさ大学学生寮 入寮規程' }),
    summary(4, 'cafe_store_guide.pdf', { title: 'カフェ手引き' }),
  ],
};

function chunks(document: object, originalPdf: boolean) {
  return {
    document,
    chunks: [
      {
        chunk_index: 0,
        page: 1,
        page_end: null,
        kind: 'text',
        heading: null,
        content: '本文',
        embedded: true,
        embedding_model: 'm',
      },
    ],
    original_pdf: originalPdf,
  };
}

function node(id: string, type: string, label: string, extra: object = {}) {
  return { id, type, label, text: null, parent: null, page: null, ...extra };
}

function edge(type: string, src: string, dst: string, evidence: string, page = 2) {
  return { type, src, dst, evidence, page, extracted_by: 'rule', updated_at: '2026-10-18T00:00:00Z' };
}

const GRAPH = {
  document: summary(3, DORM),
  title: 'つばさ大学学生寮 入寮規程',
  nodes: [
    node('ch3', 'chapter', '第3章 費用'),
    node('a13', 'article', '第13条 寮費は、月額とし、次のとおりとする。', { parent: 'ch3', page: 2 }),
    node('a13p1', 'paragraph', '第13条 第1項', { text: '寮費は、月額とし、次のとおりとする。', parent: 'a13', page: 2 }),
    node('a13p2', 'paragraph', '第13条 第2項', { text: 'ひばり棟の個室は 38,000 円とする。', parent: 'a13', page: 2 }),
    node('a15', 'article', '第15条 寮費及び共益費は、毎月 25 日までに納める。', { parent: 'ch3', page: 2 }),
    node('a15p1', 'paragraph', '第15条 第1項', { text: '寮費及び共益費は、毎月 25 日までに納める。', parent: 'a15', page: 2 }),
    node('a17', 'article', '第17条 退寮する月の寮費は、全額を納める。', { parent: 'ch3', page: 3 }),
    node('a17p1', 'paragraph', '第17条 第1項', { text: '退寮する月の寮費は、全額を納める。', parent: 'a17', page: 3 }),
    node('a19p1', 'paragraph', '第19条 第1項', { text: '門限は、午後 11 時とする。', parent: 'a19', page: 3 }),
  ],
  edges: [
    edge('same_chapter', 'a13', 'ch3', '第3章'),
    edge('references', 'a13p1', 'a13p2', '次のとおり'),
    edge('uses_term', 'a15p1', 'a13p1', '寮費'),
    edge('references', 'a17p1', 'a15p1', '前項'),
  ],
};

const IMPACT = {
  document: summary(3, DORM),
  node: 'a13p1',
  hops: 2,
  start: ['a13p1'],
  impacted: [
    { id: 'a15p1', hops: 1 },
    { id: 'a17p1', hops: 2 },
  ],
  articles: ['a15', 'a17'],
  edges: [edge('uses_term', 'a15p1', 'a13p1', '寮費'), edge('references', 'a17p1', 'a15p1', '前項')],
};

type Route = (url: string, call: number) => Response | Promise<Response>;

function install(route: Route, originalPdf = true) {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'mock' });
      calls.push(url);
      if (url === '/documents') return jsonResponse(LIST);
      if (url === '/documents/3/chunks') return jsonResponse(chunks(LIST.documents[0]!, originalPdf));
      if (url === '/documents/4/chunks') return jsonResponse(chunks(LIST.documents[1]!, true));
      if (url.endsWith('/file')) {
        return new Response(new Blob(['%PDF'], { type: 'application/pdf' }), {
          status: 200,
          headers: { 'Content-Type': 'application/pdf' },
        });
      }
      return route(url, calls.length);
    }),
  );
  return { calls: () => calls.filter((u) => u.includes('/graph') || u.includes('/impact')) };
}

async function open(name: string) {
  Object.assign(URL, { createObjectURL: () => 'blob:test', revokeObjectURL: () => {} });
  const user = userEvent.setup();
  render(
    <AuthProvider client={null}>
      <App />
    </AuthProvider>,
  );
  await user.click(await screen.findByRole('button', { name: '資料一覧' }));
  await user.click(await screen.findByRole('button', { name }));
  return user;
}

const OPEN_DORM = 'つばさ大学学生寮 入寮規程 を開く';

function defaultRoute(url: string): Response {
  if (url === '/documents/3/graph') return jsonResponse(GRAPH);
  if (url.startsWith('/documents/3/impact')) return jsonResponse(IMPACT);
  throw new Error(`想定外の取得: ${url}`);
}

describe('関係図: タブ', () => {
  it('入寮規程には「関係図」が出る。開くまでは、グラフを取らない', async () => {
    const { calls } = install(defaultRoute);
    await open(OPEN_DORM);

    expect(await screen.findByRole('button', { name: '関係図' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '関係図' })).toHaveAttribute('aria-pressed', 'false');
    expect(calls()).toEqual([]);
  });

  it('他の資料には出さない', async () => {
    install(defaultRoute);
    await open('カフェ手引き を開く');

    expect(await screen.findByRole('button', { name: 'AI用テキスト' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '関係図' })).not.toBeInTheDocument();
  });

  it('元PDFが使えない入寮規程にも出さない（サーバーは元の PDF から計算する）', async () => {
    install(defaultRoute, false);
    await open(OPEN_DORM);

    expect(await screen.findByText('元PDFは利用できません。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '関係図' })).not.toBeInTheDocument();
  });

  it('開くと、GET /documents/3/graph を 1 回だけ取り、注意書きと、選択欄・凡例を出す', async () => {
    const { calls } = install(defaultRoute);
    const user = await open(OPEN_DORM);

    await user.click(await screen.findByRole('button', { name: '関係図' }));

    expect(await screen.findByText(MSG_GRAPH_NOTE)).toBeInTheDocument();
    expect(calls()).toEqual(['/documents/3/graph']);
    expect(screen.getByRole('button', { name: '関係図' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('list', { name: '凡例' })).toHaveTextContent('直接（1 ホップ）');
    expect(screen.getByRole('list', { name: '凡例' })).toHaveTextContent('間接（2 ホップ）');
    expect(screen.getByText(MSG_NO_SELECTION)).toBeInTheDocument();
  });

  it('図に渡すのは、依存の辺に出てくる項だけ。依存のない項は、選択欄に「図にない」として残る', async () => {
    install(defaultRoute);
    const user = await open(OPEN_DORM);
    await user.click(await screen.findByRole('button', { name: '関係図' }));

    const canvas = await screen.findByTestId('canvas');
    const labels = within(canvas)
      .getAllByRole('button')
      .map((b) => b.textContent);
    expect(labels).toEqual(['第13条 第1項|-', '第13条 第2項|-', '第15条 第1項|-', '第17条 第1項|-']);
    expect(screen.getByRole('option', { name: '第19条 第1項（図にない）' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '第13条 第1項' })).toBeInTheDocument();
  });

  it('「資料一覧に戻る」と、また開いたときは、関係図を取り直す', async () => {
    const { calls } = install(defaultRoute);
    const user = await open(OPEN_DORM);
    await user.click(await screen.findByRole('button', { name: '関係図' }));
    await screen.findByTestId('canvas');

    await user.click(screen.getByRole('button', { name: '資料一覧に戻る' }));
    await user.click(await screen.findByRole('button', { name: OPEN_DORM }));
    await user.click(await screen.findByRole('button', { name: '関係図' }));
    await screen.findByTestId('canvas');

    expect(calls()).toEqual(['/documents/3/graph', '/documents/3/graph']);
  });
});

describe('関係図: 項を選ぶ', () => {
  async function openGraph() {
    const harness = install(defaultRoute);
    const user = await open(OPEN_DORM);
    await user.click(await screen.findByRole('button', { name: '関係図' }));
    await screen.findByTestId('canvas');
    return { user, ...harness };
  }

  it('図のノードを押すと、GET /documents/3/impact?node=…&hops=2 を取り、影響を直接・間接に分けて図に渡す', async () => {
    const { user, calls } = await openGraph();

    await user.click(screen.getByRole('button', { name: '第13条 第1項|-' }));

    expect(await screen.findByRole('button', { name: '第15条 第1項|direct' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '第13条 第1項|start' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '第17条 第1項|indirect' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '第13条 第2項|-' })).toBeInTheDocument();
    expect(calls()).toEqual(['/documents/3/graph', '/documents/3/impact?node=a13p1&hops=2']);
    expect(screen.getByTestId('canvas')).toHaveAttribute('data-selected', 'a13p1');
    // たどった辺は強調するために図へ渡す
    expect(screen.getByTestId('canvas').getAttribute('data-walked')).toBe('uses_term|a15p1|a13p1,references|a17p1|a15p1');
  });

  it('右（下）のパネルに、本文・出典・依存している先と根拠・影響（直接/間接）を出す', async () => {
    const { user } = await openGraph();

    await user.click(screen.getByRole('button', { name: '第13条 第1項|-' }));

    const panel = await screen.findByRole('complementary', { name: '選んだ項の詳細' });
    await within(panel).findByText('間接');
    expect(within(panel).getByRole('heading', { name: '第13条 第1項' })).toBeInTheDocument();
    expect(within(panel).getByText('寮費は、月額とし、次のとおりとする。')).toBeInTheDocument();
    expect(within(panel).getByText(/つばさ大学学生寮 入寮規程\s+p\.2/)).toBeInTheDocument();
    expect(within(panel).getByText(DORM)).toBeInTheDocument();
    expect(within(panel).getByText('第13条 第2項')).toBeInTheDocument();
    expect(within(panel).getByText('根拠「次のとおり」 p.2')).toBeInTheDocument();
    expect(within(panel).getByText('この項を変えたときの影響（2 ホップまで）')).toBeInTheDocument();
    const impacted = within(panel).getAllByRole('listitem').filter((li) => /^(直接|間接)/.test(li.textContent ?? ''));
    expect(impacted.map((li) => li.textContent)).toEqual(['直接第15条 第1項p.2', '間接第17条 第1項p.3']);
    expect(within(panel).getByText('影響が及ぶ条: 第15条、第17条')).toBeInTheDocument();
  });

  it('選択欄からも選べる。図にない項（依存なし）は、「影響を受ける項はありません」', async () => {
    install((url) => {
      if (url === '/documents/3/graph') return jsonResponse(GRAPH);
      return jsonResponse({ ...IMPACT, node: 'a19p1', start: ['a19p1'], impacted: [], articles: [], edges: [] });
    });
    const user = await open(OPEN_DORM);
    await user.click(await screen.findByRole('button', { name: '関係図' }));
    await screen.findByTestId('canvas');

    await user.selectOptions(screen.getByRole('combobox'), '第19条 第1項（図にない）');

    const panel = await screen.findByRole('complementary', { name: '選んだ項の詳細' });
    expect(await within(panel).findByText(MSG_NO_IMPACT)).toBeInTheDocument();
    expect(within(panel).getByText('門限は、午後 11 時とする。')).toBeInTheDocument();
    expect(within(panel).getByText('ありません。')).toBeInTheDocument(); // 依存している先
  });

  it('選択を外すと、パネルは案内に戻り、図の強調も消える', async () => {
    const { user } = await openGraph();
    await user.click(screen.getByRole('button', { name: '第13条 第1項|-' }));
    await screen.findByRole('button', { name: '第15条 第1項|direct' });

    await user.selectOptions(screen.getByRole('combobox'), '（選んでいません）');

    expect(screen.getByText(MSG_NO_SELECTION)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '第15条 第1項|-' })).toBeInTheDocument();
  });

  it('影響の取得に失敗したら、パネルに固定メッセージと再試行を出し、再試行で取り直せる', async () => {
    const message = '条文の関係を読み取れませんでした。';
    let impactCalls = 0;
    install((url) => {
      if (url === '/documents/3/graph') return jsonResponse(GRAPH);
      impactCalls += 1;
      return impactCalls === 1 ? errorResponse(500, message) : jsonResponse(IMPACT);
    });
    const user = await open(OPEN_DORM);
    await user.click(await screen.findByRole('button', { name: '関係図' }));
    await screen.findByTestId('canvas');

    await user.click(screen.getByRole('button', { name: '第13条 第1項|-' }));

    const panel = screen.getByRole('complementary', { name: '選んだ項の詳細' });
    expect(await within(panel).findByRole('alert')).toHaveTextContent(message);
    expect(within(panel).getByText('寮費は、月額とし、次のとおりとする。')).toBeInTheDocument(); // 本文は出せる
    await user.click(within(panel).getByRole('button', { name: '再試行' }));
    expect(await within(panel).findByText('間接')).toBeInTheDocument();
  });
});

describe('関係図: 取得の失敗', () => {
  it('グラフの取得に失敗したら、固定メッセージと再試行を出す', async () => {
    const message = '条文の関係を読み取れませんでした。';
    let calls = 0;
    install((url) => {
      if (url !== '/documents/3/graph') throw new Error(`想定外の取得: ${url}`);
      calls += 1;
      return calls === 1 ? errorResponse(500, message) : jsonResponse(GRAPH);
    });
    const user = await open(OPEN_DORM);

    await user.click(await screen.findByRole('button', { name: '関係図' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByTestId('canvas')).toBeInTheDocument();
  });

  it('サーバーのエラー本文（英語・接続先など）は見せない', async () => {
    install(() => new Response('Internal Server Error postgresql://user:pw@host/db', { status: 500 }));
    const user = await open(OPEN_DORM);

    await user.click(await screen.findByRole('button', { name: '関係図' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('HTTP 500');
    expect(alert).not.toHaveTextContent('postgresql');
  });

  it.each([
    ['nodes が配列でない', { ...GRAPH, nodes: 'x' }],
    ['ノードの種類が未知', { ...GRAPH, nodes: [node('x1', 'galaxy', 'x')] }],
    ['辺の種類が未知', { ...GRAPH, edges: [edge('causes', 'a13p1', 'a13p2', 'x')] }],
    ['辺の page が数でない', { ...GRAPH, edges: [{ ...edge('references', 'a13p1', 'a13p2', 'x'), page: 'p2' }] }],
    ['title が文字列でも null でもない', { ...GRAPH, title: 3 }],
  ])('形が違う応答（%s）は、想定外の応答として扱う', async (_name, body) => {
    install(() => jsonResponse(body));
    const user = await open(OPEN_DORM);

    await user.click(await screen.findByRole('button', { name: '関係図' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('想定外の応答');
  });

  it('描ける項が 1 つも無い（辺が無い）ときは、その旨を出す', async () => {
    install(() => jsonResponse({ ...GRAPH, edges: [edge('same_chapter', 'a13', 'ch3', '第3章')] }));
    const user = await open(OPEN_DORM);

    await user.click(await screen.findByRole('button', { name: '関係図' }));

    expect(await screen.findByText('関係を表示できる項がありません。')).toBeInTheDocument();
  });
});
