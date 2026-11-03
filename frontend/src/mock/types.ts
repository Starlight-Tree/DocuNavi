/** UI モックの型。実 API の型（`api/`）とは独立で、実データには触れない。 */

export type DocStatus = 'processing' | 'ready' | 'failed';

export interface MockDoc {
  id: string;
  name: string;
  /** 表示用の種別（PDF / Word / Excel …）。拡張子から決める。 */
  kind: string;
  status: DocStatus;
  /** 追加日時（ISO 8601）。 */
  addedAt: string;
  size: number | null;
  /** 取り込み中のとき、完了する時刻（epoch ms）。モックの時計が見る。 */
  processingUntil: number | null;
  /** 取り込みが失敗する資料（ファイル名に「エラー」「error」を含む）。再試行は成功する。 */
  willFail: boolean;
  failureReason: string | null;
  /** チャットから追加したとき、その会話の ID。画面の案内に使う。 */
  origin: string | null;
}

export interface PassagePage {
  page: number;
  heading: string;
  paragraphs: string[];
}

/** 質問対象。`all` は取り込み済みの資料だけ。`selected` は選んだ資料（取り込み中の予約を含みうる）。 */
export type Scope = { mode: 'all' } | { mode: 'selected'; ids: string[] };

export interface SourceRef {
  docId: string;
  /** 回答を作った時点の資料名。資料が削除されても表示に使う。 */
  docName: string;
  page: number;
  /** ページ内の何番目の段落が根拠か。 */
  para: number;
  /** 「p.2 ・ 2. 申請と締め日」のような位置の表示。 */
  location: string;
}

export interface MockMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  text: string;
  sources?: SourceRef[];
}

export interface MockThread {
  id: string;
  title: string;
  scope: Scope;
  messages: MockMessage[];
  updatedAt: string;
  /** 回答の生成を待っている（モックの遅延）。 */
  pending: boolean;
}

/** 右パネルが見せる対象。`focus` があれば、回答の出典として該当箇所を強調する。 */
export interface PanelTarget {
  docId: string;
  /** 資料が削除されたあとでも出せる名前（出典から開いたとき）。 */
  docName?: string;
  focus?: { page: number; para: number; location: string };
  /** 出典を開いたときの、回答中の番号（1 始まり）。 */
  citation?: number;
}

export type DialogState =
  | { type: 'upload'; origin: 'chat' | 'documents' }
  | { type: 'scope' }
  | { type: 'delete'; docId: string }
  | { type: 'settings' };

export interface Toast {
  id: string;
  text: string;
  tone: 'info' | 'error';
  until: number;
}

export type View = 'chat' | 'documents';
