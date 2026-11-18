import type { ChunkItem, DocumentSummary } from '../api/documents';
import { formatPages } from '../lib/sources';

const STATUS_LABELS: Record<string, string> = { ready: '取り込み済み', failed: '取り込み失敗' };

export function statusName(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function statusLabel(document: DocumentSummary): string {
  const label = statusName(document.status);
  return document.failureReason ? `${label}（${document.failureReason}）` : label;
}

const DATE_FORMAT = new Intl.DateTimeFormat('ja-JP', {
  timeZone: 'Asia/Tokyo',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
});

const DAY_FORMAT = new Intl.DateTimeFormat('ja-JP', { timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit', day: '2-digit' });

/** 一覧の列に出す、日付だけの表記。記録が無い・読めないときは null（呼び出し側が、欠けていることが分かる表示にする）。 */
export function formatDay(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : DAY_FORMAT.format(date);
}

export function formatDate(value: string | null): string {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '-' : DATE_FORMAT.format(date);
}

/** 題名があれば題名を主に、ファイル名を補助に出す。題名が無ければ、ファイル名だけ（同じ文字を 2 度出さない）。 */
export function displayName(document: DocumentSummary): string {
  return document.title ?? document.source;
}

/** PDF を保存するときのファイル名。取り込んだ元のファイル名（日本語のまま）。拡張子が無ければ `.pdf` を付ける。 */
export function pdfDownloadName(document: DocumentSummary): string {
  return /\.pdf$/i.test(document.source) ? document.source : `${document.source}.pdf`;
}

/** `showFile` を false にすると、ファイル名を出さない（一覧は、同じ題名の資料を見分けるときだけ出す）。 */
export function DocumentName({ document, showFile = true }: { document: DocumentSummary; showFile?: boolean }) {
  return (
    <>
      <span className="document-name">{displayName(document)}</span>
      {showFile && document.title && <span className="document-file">{document.source}</span>}
    </>
  );
}

/** 更新日時。記録が無いときは、「-」ではなく、欠けていることが分かる文言にする（日時を作らない）。 */
export function UpdatedAt({ value, withTime = false }: { value: string | null; withTime?: boolean }) {
  const text = withTime ? formatDate(value) : formatDay(value);
  if (!text || text === '-') {
    return (
      <span className="doc-missing" title="この資料には、更新日時の記録がありません">
        記録なし
      </span>
    );
  }
  return <time dateTime={value ?? undefined}>{text}</time>;
}

export function pageLabel(chunk: ChunkItem): string | null {
  if (chunk.page === null) return null;
  const end = chunk.pageEnd !== null && chunk.pageEnd !== chunk.page ? chunk.pageEnd : undefined;
  return formatPages(chunk.page, end, chunk.pageLabel ?? undefined, chunk.pageEndLabel ?? undefined);
}
