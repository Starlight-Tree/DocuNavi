import type { CollectionInfo } from '../api/collections';
import { isPreparing } from '../lib/collections';

interface ScopeChipsProps {
  /** 選べるグループ。一覧を読めなかったとき（unavailable）は無視して「すべて」だけを出す。 */
  collections: ReadonlyArray<CollectionInfo>;
  /** 'unavailable' = グループの一覧を読めなかった。「すべて」だけを出し、再試行を添える。 */
  status: 'ready' | 'unavailable';
  /** 選ばれている検索範囲（null は「すべて」）。 */
  value: string | null;
  /** 会話の検索範囲が分からない間は、どれも選ばれた状態にしない。 */
  unknown: boolean;
  /** 回答を作っている間は選べない（切り替えは会話を新しくするので、生成が終わってから）。 */
  disabled: boolean;
  onSelect: (collection: string | null, trigger: HTMLElement) => void;
  onRetry: () => void;
}

/** 入力欄の上の、検索範囲の選択（すべて / 資料グループ）。会話の途中で別の範囲を選ぶと、呼び出し側が確認を出す。 */
export function ScopeChips({ collections, status, value, unknown, disabled, onSelect, onRetry }: ScopeChipsProps) {
  const shown = status === 'ready' ? collections : [];
  return (
    <div className="scope-chips" role="group" aria-label="検索範囲">
      <span className="scope-chips-label" aria-hidden="true">
        検索範囲
      </span>
      <button
        type="button"
        className="scope-chip"
        aria-pressed={!unknown && value === null}
        disabled={disabled}
        onClick={(event) => onSelect(null, event.currentTarget)}
      >
        すべて
      </button>
      {shown.map((collection) => (
        <button
          key={collection.name}
          type="button"
          className="scope-chip"
          aria-pressed={!unknown && value === collection.name}
          disabled={disabled}
          title={collection.description || undefined}
          onClick={(event) => onSelect(collection.name, event.currentTarget)}
        >
          {collection.label}
          {isPreparing(collection) && <span className="scope-chip-note">準備中</span>}
        </button>
      ))}
      {status === 'unavailable' && (
        <span className="scope-chips-failure" role="status">
          グループの一覧を読み込めませんでした。
          <button type="button" className="btn-link" onClick={onRetry}>
            再試行
          </button>
        </span>
      )}
    </div>
  );
}
