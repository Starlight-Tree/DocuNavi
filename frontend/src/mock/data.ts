import { formatPages } from '../lib/sources';
import type { MockDoc, MockMessage, MockThread, PassagePage, Scope, SourceRef } from './types';

/** UI モックのサンプルデータ。資料名・状態は全画面で同じ ID から引く。内容はすべて架空。 */

const KINDS: Record<string, string> = {
  pdf: 'PDF',
  docx: 'Word',
  doc: 'Word',
  xlsx: 'Excel',
  xls: 'Excel',
  pptx: 'PowerPoint',
  txt: 'テキスト',
  md: 'Markdown',
  csv: 'CSV',
};

/** アップロードで受け付ける拡張子（モックの検証用）。 */
export const ACCEPTED_EXTENSIONS = Object.keys(KINDS);
export const ACCEPT_ATTRIBUTE = ACCEPTED_EXTENSIONS.map((ext) => `.${ext}`).join(',');
export const MAX_UPLOAD_FILES = 10;

export function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot < 0 ? '' : name.slice(dot + 1).toLowerCase();
}

export function kindOf(name: string): string {
  return KINDS[extensionOf(name)] ?? 'その他';
}

export function isAcceptedName(name: string): boolean {
  return ACCEPTED_EXTENSIONS.includes(extensionOf(name));
}

const DATE_TIME = new Intl.DateTimeFormat('ja-JP', {
  timeZone: 'Asia/Tokyo',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

/** 「2026/11/03 14:20」。 */
export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : DATE_TIME.format(date);
}

export function formatSize(bytes: number | null): string {
  if (bytes === null) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export const STATUS_LABEL = {
  ready: '利用可能',
  processing: '取り込み中',
  failed: 'エラー',
} as const;

/** 取り込み中の資料が質問対象に選べない理由。 */
export const UNSELECTABLE_REASON = {
  processing: '取り込み中のため選べません（完了後に選べます）',
  failed: 'エラーのため選べません（資料画面で再試行できます）',
} as const;

const DEMO_FAIL_NAME = /エラー|error/i;

/** ファイル名に「エラー」か「error」を含むと、取り込みが失敗するモック。 */
export function willFailByName(name: string): boolean {
  return DEMO_FAIL_NAME.test(name);
}

export const FAILURE_REASON_SCANNED = 'テキストを抽出できませんでした（画像だけの PDF の可能性があります）。';
export const FAILURE_REASON_DEMO = 'テキストを抽出できませんでした（モック: ファイル名に「エラー」を含む資料は失敗します）。';

/** 取り込みの所要時間（モックの時計）。 */
export const INGEST_MS = 4500;
export const INGEST_STAGGER_MS = 1200;
export const RETRY_MS = 3000;
export const REPLY_DELAY_MS = 900;
export const TOAST_MS = 6000;

/** 起動の直後から取り込みが進んでいる資料が、完了するまでの時間。 */
const SEED_PROCESSING_MS = 12_000;

// ---- 資料の原文サンプル（すべて架空） ----

const PASSAGES: Record<string, PassagePage[]> = {
  d1: [
    {
      page: 1,
      heading: '第1章 はじめに',
      paragraphs: [
        '本マニュアルは、日常業務の基本的な進め方をまとめたものです。',
        '業務の依頼は、原則として社内の依頼フォームから受け付けます。メールや口頭で受けた依頼も、担当者がフォームへ転記します。',
      ],
    },
    {
      page: 2,
      heading: '第2章 依頼の受付と対応',
      paragraphs: [
        '依頼を受けたら、2営業日以内に担当者を決め、依頼者へ連絡します。',
        '緊急の依頼は、担当部署の責任者に口頭で伝え、その後フォームへ記録します。',
        '対応が完了したら、依頼フォームのステータスを「完了」に更新し、依頼者へ結果を知らせます。',
      ],
    },
    {
      page: 3,
      heading: '第3章 記録と共有',
      paragraphs: [
        '作業の記録は、週次で共有フォルダーの所定の様式に追記します。',
        '他の担当者に引き継ぐ事項は、依頼フォームのコメント欄に残します。',
      ],
    },
  ],
  d2: [
    {
      page: 1,
      heading: '第1条 契約期間',
      paragraphs: [
        '契約期間は、契約締結日から1年間とします。',
        '期間満了の60日前までに、いずれからも書面による申し出がないときは、同一の条件でさらに1年間更新されるものとします。',
      ],
    },
    {
      page: 2,
      heading: '第2条 中途解約',
      paragraphs: [
        '中途解約を希望する場合は、解約希望日の90日前までに、書面で相手方へ通知します。',
        '解約に伴う違約金は、発生しないものとします。ただし、すでに発生した費用の精算は妨げません。',
      ],
    },
    {
      page: 3,
      heading: '第3条 支払条件',
      paragraphs: ['請求書は月末に締め、翌月末日までに支払うものとします。', '支払いが遅れたときは、遅延日数に応じた遅延損害金を請求できます。'],
    },
  ],
  d3: [
    {
      page: 1,
      heading: '2026年10月 定例会議',
      paragraphs: [
        '日時: 2026年10月28日 10:00〜11:00 / 出席: 5名',
        '決定事項: 新しい依頼フォームの運用を、11月1日から開始する。',
        '決定事項: 契約条件の見直しは、来月の定例会議で改めて検討する。',
      ],
    },
    {
      page: 2,
      heading: '次回までの対応',
      paragraphs: ['依頼フォームの操作手順を、全員へ10月31日までに共有する。', '契約条件の論点を、担当者が1ページにまとめる。'],
    },
  ],
  d4: [
    {
      page: 1,
      heading: '第1章 総則',
      paragraphs: ['この規則は、従業員の就業に関する基本的な事項を定めるものです。'],
    },
    {
      page: 2,
      heading: '第2章 勤務時間と休暇',
      paragraphs: [
        '所定労働時間は、1日8時間、1週40時間とします。',
        '年次有給休暇は、入社6か月後に10日を付与します。以後、1年ごとに勤続年数に応じて付与日数が増えます。',
        '休暇を取るときは、原則として3営業日前までに、上長へ申請します。',
      ],
    },
  ],
  d5: [
    {
      page: 1,
      heading: '1. 対象となる経費',
      paragraphs: ['業務に必要な交通費、消耗品費、会議費が対象です。私的な支出は対象になりません。'],
    },
    {
      page: 2,
      heading: '2. 申請と締め日',
      paragraphs: [
        '経費の申請は、発生日の翌月5日までに、経費精算システムから行います。',
        '領収書は、申請時に画像を添付し、原本は月末まで保管します。',
        '1件3万円を超える経費は、事前に上長の承認を受けます。',
      ],
    },
  ],
  d6: [
    {
      page: 1,
      heading: 'シート: 見積一覧',
      paragraphs: ['案件A ｜ 120万円 ｜ 2026/09/12 ｜ 提出済み', '案件B ｜ 85万円 ｜ 2026/10/03 ｜ 返答待ち', '案件C ｜ 240万円 ｜ 2026/10/21 ｜ 受注'],
    },
  ],
};

/** 追加した資料の原文サンプル。実ファイルは読まないので、固定の説明文になる。 */
function generatedPages(name: string): PassagePage[] {
  return [
    {
      page: 1,
      heading: 'サンプル本文（モック）',
      paragraphs: [
        `これは「${name}」のサンプル本文です。実際のファイルの内容は読み取っていません。`,
        '実バックエンドに接続すると、ここに取り込んだ資料の本文が、ページごとに表示されます。',
      ],
    },
    {
      page: 2,
      heading: '出典の表示（モック）',
      paragraphs: ['回答の出典から開くと、根拠になった箇所がこのように強調されます。'],
    },
  ];
}

/** 資料の原文サンプル。種類ごとの固定データ、または追加した資料の生成文。 */
export function passagesFor(doc: Pick<MockDoc, 'id' | 'name'>): PassagePage[] {
  return PASSAGES[doc.id] ?? generatedPages(doc.name);
}

/** 原文の総ページ数（モック）。 */
export function pageCountFor(doc: Pick<MockDoc, 'id' | 'name'>): number {
  return passagesFor(doc).length;
}

export function locationLabel(pages: PassagePage[], page: number): string {
  const found = pages.find((p) => p.page === page);
  const pageText = formatPages(page, undefined) ?? `p.${page}`;
  return found ? `${pageText} ・ ${found.heading}` : pageText;
}

export function makeSource(doc: Pick<MockDoc, 'id' | 'name'>, page: number, para: number): SourceRef {
  return { docId: doc.id, docName: doc.name, page, para, location: locationLabel(passagesFor(doc), page) };
}

// ---- 初期データ ----

export function seedDocs(now: number): MockDoc[] {
  const base = (partial: Partial<MockDoc> & Pick<MockDoc, 'id' | 'name' | 'addedAt'>): MockDoc => ({
    kind: kindOf(partial.name),
    status: 'ready',
    size: null,
    processingUntil: null,
    willFail: false,
    failureReason: null,
    origin: null,
    ...partial,
  });
  return [
    base({ id: 'd1', name: '業務マニュアル.pdf', addedAt: '2026-10-12T01:20:00Z', size: 2_480_000 }),
    base({ id: 'd2', name: '契約条件.pdf', addedAt: '2026-10-15T06:05:00Z', size: 640_000 }),
    base({ id: 'd3', name: '会議メモ.docx', addedAt: '2026-10-28T02:10:00Z', size: 88_000 }),
    base({ id: 'd4', name: '就業規則.pdf', addedAt: '2026-10-30T09:40:00Z', size: 1_120_000 }),
    base({ id: 'd5', name: '経費精算ガイド.pdf', addedAt: '2026-11-01T04:30:00Z', size: 760_000 }),
    base({ id: 'd6', name: '見積一覧_2026.xlsx', addedAt: '2026-11-02T00:15:00Z', size: 54_000 }),
    base({
      id: 'd7',
      name: '製品仕様書_v2.docx',
      addedAt: '2026-11-03T01:00:00Z',
      size: 410_000,
      status: 'processing',
      processingUntil: now + SEED_PROCESSING_MS,
    }),
    base({
      id: 'd8',
      name: '取引先リスト_スキャン.pdf',
      addedAt: '2026-11-03T01:05:00Z',
      size: 5_300_000,
      status: 'failed',
      failureReason: FAILURE_REASON_SCANNED,
    }),
  ];
}

const ALL: Scope = { mode: 'all' };

function answer(id: string, text: string, sources?: SourceRef[]): MockMessage {
  return sources ? { id, role: 'assistant', text, sources } : { id, role: 'assistant', text };
}

export function seedThreads(docs: MockDoc[]): MockThread[] {
  const byId = (id: string) => docs.find((d) => d.id === id);
  const src = (id: string, page: number, para: number): SourceRef[] => {
    const doc = byId(id);
    return doc ? [makeSource(doc, page, para)] : [];
  };
  return [
    {
      id: 't1',
      title: '経費精算の締め日',
      scope: ALL,
      updatedAt: '2026-11-02T08:30:00Z',
      pending: false,
      messages: [
        { id: 't1-1', role: 'user', text: '経費精算の締め日はいつですか？' },
        answer('t1-2', '経費の申請は、発生日の翌月5日までに経費精算システムから行います[1]。（モック: 実際の検索・生成ではありません）', src('d5', 2, 0)),
      ],
    },
    {
      id: 't2',
      title: '契約の更新条件',
      scope: { mode: 'selected', ids: ['d2'] },
      updatedAt: '2026-11-01T05:10:00Z',
      pending: false,
      messages: [
        { id: 't2-1', role: 'user', text: '契約は自動で更新されますか？' },
        answer(
          't2-2',
          '期間満了の60日前までにいずれからも書面による申し出がなければ、同一条件でさらに1年間更新されます[1]。（モック: 実際の検索・生成ではありません）',
          src('d2', 1, 1),
        ),
      ],
    },
    {
      id: 't3',
      title: '10月の会議の決定事項',
      scope: { mode: 'selected', ids: ['d3'] },
      updatedAt: '2026-10-29T03:00:00Z',
      pending: false,
      messages: [
        { id: 't3-1', role: 'user', text: '10月の定例会議で決まったことは？' },
        answer(
          't3-2',
          '新しい依頼フォームの運用を11月1日から開始すること、契約条件の見直しは来月の定例会議で検討することが決まりました[1]。（モック: 実際の検索・生成ではありません）',
          src('d3', 1, 1),
        ),
      ],
    },
  ];
}

// ---- 定型の回答（モック） ----

interface Rule {
  pattern: RegExp;
  text: string;
  cites: Array<{ docId: string; page: number; para: number }>;
}

const RULES: Rule[] = [
  {
    pattern: /経費|精算|領収|立替/,
    text: '経費の申請は、発生日の翌月5日までに経費精算システムから行います[1]。1件3万円を超える場合は、事前に上長の承認が必要です[2]。',
    cites: [
      { docId: 'd5', page: 2, para: 0 },
      { docId: 'd5', page: 2, para: 2 },
    ],
  },
  {
    pattern: /契約|解約|更新|違約/,
    text: '契約期間は1年間で、満了の60日前までに申し出がなければ同一条件で更新されます[1]。中途解約は、希望日の90日前までに書面で通知します[2]。',
    cites: [
      { docId: 'd2', page: 1, para: 1 },
      { docId: 'd2', page: 2, para: 0 },
    ],
  },
  {
    pattern: /会議|決定|議事|定例/,
    text: '10月の定例会議では、新しい依頼フォームの運用を11月1日から始めることが決まりました[1]。',
    cites: [{ docId: 'd3', page: 1, para: 1 }],
  },
  {
    pattern: /休暇|有給|勤務|労働|就業/,
    text: '年次有給休暇は、入社6か月後に10日が付与されます[1]。取得は、原則として3営業日前までに上長へ申請します[2]。',
    cites: [
      { docId: 'd4', page: 2, para: 1 },
      { docId: 'd4', page: 2, para: 2 },
    ],
  },
  {
    pattern: /依頼|フォーム|受付|対応/,
    text: '依頼を受けたら、2営業日以内に担当者を決めて依頼者へ連絡します[1]。',
    cites: [{ docId: 'd1', page: 2, para: 0 }],
  },
  {
    pattern: /見積|案件|金額/,
    text: '見積一覧には、案件A〜Cが載っています。案件Cは240万円で受注済みです[1]。',
    cites: [{ docId: 'd6', page: 1, para: 2 }],
  },
];

const MOCK_NOTE = '（モック: 定型の回答です。実際の検索・生成はしていません）';

/**
 * 質問に対するモックの回答。対象の資料（readyDocs）の中だけから出典を選ぶ。
 * 規則の資料がすべて対象に含まれるときだけ規則を使い、そうでなければ、対象の資料の先頭の箇所を例として示す。
 */
export function mockReply(question: string, readyDocs: MockDoc[], id: string): MockMessage {
  const available = new Map(readyDocs.map((d) => [d.id, d]));
  for (const rule of RULES) {
    if (!rule.pattern.test(question)) continue;
    if (!rule.cites.every((c) => available.has(c.docId))) continue;
    const sources = rule.cites.flatMap((c) => {
      const doc = available.get(c.docId);
      return doc ? [makeSource(doc, c.page, c.para)] : [];
    });
    return answer(id, `${rule.text}${MOCK_NOTE}`, sources);
  }
  const sources = readyDocs.slice(0, 2).map((doc) => makeSource(doc, passagesFor(doc)[0]?.page ?? 1, 0));
  if (sources.length === 0) return answer(id, `対象の資料がありません。${MOCK_NOTE}`);
  return answer(
    id,
    `質問に合う定型の回答を用意していないため、対象の資料から先頭の箇所を例として示します${sources.map((_, i) => `[${i + 1}]`).join('')}。${MOCK_NOTE}`,
    sources,
  );
}
