import type { CollectionInfo } from '../api/collections';
import { isPreparing } from '../lib/collections';

interface CollectionCardsProps {
  collections: ReadonlyArray<CollectionInfo>;
  /** 一覧を絞り込んでいるグループ（null は「すべて」）。検索範囲とは別のもの。 */
  active: string | null;
  /** 「すべて」のカードに出す、資料の総数。 */
  totalCount: number;
  /** カードを選ぶ。資料の一覧を絞り込むだけで、チャットの検索範囲は変えない。 */
  onFilter: (collection: string | null) => void;
  /** 「このグループに質問」。検索範囲を変える、ただ 1 つの操作。渡さなければ、ボタンは出さない。 */
  onAsk?: ((collection: string, trigger: HTMLElement) => void) | undefined;
}

/** 資料一覧の上の、資料グループのカード。選ぶと一覧が絞られ、「このグループに質問」でチャットの検索範囲が変わる。 */
export function CollectionCards({ collections, active, totalCount, onFilter, onAsk }: CollectionCardsProps) {
  return (
    <div className="collection-cards" role="group" aria-label="資料グループ（押すと一覧を絞り込みます）">
      <div className="collection-card" data-active={active === null ? 'true' : undefined}>
        <button type="button" className="collection-card-main" aria-pressed={active === null} onClick={() => onFilter(null)}>
          <span className="collection-card-label">すべて</span>
          <span className="collection-card-description">すべての資料</span>
          <span className="collection-card-meta">資料 {totalCount} 件</span>
        </button>
      </div>
      {collections.map((collection) => {
        const preparing = isPreparing(collection);
        return (
          <div key={collection.name} className="collection-card" data-active={active === collection.name ? 'true' : undefined}>
            <button
              type="button"
              className="collection-card-main"
              aria-pressed={active === collection.name}
              onClick={() => onFilter(collection.name)}
            >
              <span className="collection-card-label">{collection.label}</span>
              {collection.description && <span className="collection-card-description">{collection.description}</span>}
              <span className="collection-card-meta" data-preparing={preparing ? 'true' : undefined}>
                {preparing
                  ? collection.documentCount === 0
                    ? '準備中・資料はまだありません'
                    : '準備中・検索できる資料はまだありません'
                  : `資料 ${collection.documentCount} 件`}
              </span>
            </button>
            {onAsk && (
              <button
                type="button"
                className="btn btn-small collection-card-ask"
                aria-label={`${collection.label}: このグループに質問`}
                onClick={(event) => onAsk(collection.name, event.currentTarget)}
              >
                このグループに質問
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
