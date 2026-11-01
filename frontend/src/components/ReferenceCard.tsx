import { useCollectionCatalog } from '../hooks/useCollections';
import { collectionLabel } from '../lib/collections';
import { formatPages } from '../lib/sources';
import type { SourceDoc } from '../lib/sources';

interface ReferenceCardProps {
  /** 回答の出典の中での番号（1 始まり）。 */
  number: number;
  source: SourceDoc;
  /** `compact` は回答の下（サムネイルは低め・抜粋なし）、`full` は参照資料の専用ページ（抜粋つき）。 */
  variant: 'compact' | 'full';
  opening: boolean;
  disabled: boolean;
  onOpen: () => void;
}

/** 資料の画像がない間（生成は未承認）の灰色の代替表示。画像が入る場所は、このブロックと同じ。 */
function ThumbPlaceholder() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
    </svg>
  );
}

/**
 * 参照した資料の 1 枚のカード。押すと、その箇所のページで元の PDF を開く（開き方は呼び出し側）。
 * 上のサムネイルは、画像がないので灰色の代替表示。種別は、資料グループが分かるときだけ出す。
 */
export function ReferenceCard({ number, source, variant, opening, disabled, onOpen }: ReferenceCardProps) {
  const { state: catalog } = useCollectionCatalog();
  const groups = catalog.status === 'ready' ? catalog.data.collections : undefined;
  const pages = formatPages(source.page, source.pageEnd);
  const meta = [source.collection ? collectionLabel(source.collection, groups) : null, pages].filter(Boolean).join(' ・ ');

  return (
    <button type="button" className={`reference-card reference-card-${variant}`} disabled={disabled} onClick={onOpen}>
      <span className="reference-thumb" aria-hidden="true">
        <ThumbPlaceholder />
      </span>
      <span className="reference-body">
        <span className="reference-title">
          <span className="source-chip-number" aria-hidden="true">
            {number}
          </span>
          <span className="reference-name">{source.name}</span>
        </span>
        {meta && <span className="reference-meta">{meta}</span>}
        {variant === 'full' && <span className="reference-excerpt">{source.text}</span>}
        <span className="reference-open">{opening ? '開いています…' : '資料を開く →'}</span>
      </span>
    </button>
  );
}
