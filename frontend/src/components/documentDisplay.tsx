import type { ChunkItem, DocumentSummary } from '../api/documents';

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

export function formatDate(value: string | null): string {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '-' : DATE_FORMAT.format(date);
}

/** 題名があれば題名を主に、ファイル名を補助に出す。題名が無ければ、ファイル名だけ（同じ文字を 2 度出さない）。 */
export function displayName(document: DocumentSummary): string {
  return document.title ?? document.source;
}

export function DocumentName({ document }: { document: DocumentSummary }) {
  return (
    <>
      <span className="document-name">{displayName(document)}</span>
      {document.title && <span className="document-file">{document.source}</span>}
    </>
  );
}

export function pageLabel(chunk: ChunkItem): string | null {
  if (chunk.page === null) return null;
  return chunk.pageEnd !== null && chunk.pageEnd !== chunk.page ? `p.${chunk.page}–${chunk.pageEnd}` : `p.${chunk.page}`;
}
