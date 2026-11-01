import { useEffect, useState } from 'react';
import { PAGE_BANDS, QUERY_MAX_LENGTH, SORT_KEYS, SORT_LABELS, UPDATED_BANDS, isFiltered } from '../lib/documentFilter';
import type { DocumentFilter, FacetOption, PageBand, UpdatedBand } from '../lib/documentFilter';

export interface DocumentFilterOptions {
  status: FacetOption[];
  pages: FacetOption<PageBand>[];
  updated: FacetOption<UpdatedBand>[];
}

interface DocumentFilterBarProps {
  filter: DocumentFilter;
  options: DocumentFilterOptions;
  /** 条件に合う資料の数（並び替えの前後で変わらない）。 */
  resultCount: number;
  /** 条件を変える。`replace` は、入力の途中のように、履歴を増やしたくない変更。 */
  onChange: (next: Partial<DocumentFilter>, mode?: 'push' | 'replace') => void;
  onClear: () => void;
}

interface FacetSelectProps<T extends string> {
  label: string;
  value: T | null;
  options: FacetOption<T>[];
  onSelect: (value: T | null) => void;
}

function FacetSelect<T extends string>({ label, value, options, onSelect }: FacetSelectProps<T>) {
  return (
    <label className="document-filter-field">
      <span className="document-filter-label">{label}</span>
      <select
        value={value ?? ''}
        onChange={(event) => onSelect(options.find((o) => o.value === event.target.value)?.value ?? null)}
      >
        {options.map((o) => (
          <option key={o.value ?? ''} value={o.value ?? ''}>
            {`${o.label} (${o.count})`}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * 資料一覧の、検索・絞り込み・並び替え（1 行）と、件数。状態は持たない（URL が持つ）。
 * キーワードの入力欄だけ、日本語の変換中の文字が URL の往復で崩れないよう、手元の文字を持ち、URL が外から変わったときに合わせる。
 */
export function DocumentFilterBar({ filter, options, resultCount, onChange, onClear }: DocumentFilterBarProps) {
  const [text, setText] = useState(filter.query);
  useEffect(() => {
    setText(filter.query);
  }, [filter.query]);

  return (
    <>
      <section className="document-filter" aria-label="資料の検索・絞り込み・並び替え">
        <label className="document-filter-field document-filter-query">
          <span className="document-filter-label">キーワード（空白で AND）</span>
          <input
            type="search"
            value={text}
            maxLength={QUERY_MAX_LENGTH}
            placeholder="文書名・ファイル名・種別"
            onChange={(event) => {
              setText(event.target.value);
              onChange({ query: event.target.value }, 'replace');
            }}
          />
        </label>
        <FacetSelect label="状態" value={filter.status} options={options.status} onSelect={(status) => onChange({ status })} />
        <FacetSelect
          label="ページ数"
          value={filter.pages}
          options={options.pages}
          onSelect={(pages) => onChange({ pages: PAGE_BANDS.find((b) => b === pages) ?? null })}
        />
        <FacetSelect
          label="更新"
          value={filter.updated}
          options={options.updated}
          onSelect={(updated) => onChange({ updated: UPDATED_BANDS.find((b) => b === updated) ?? null })}
        />
        <label className="document-filter-field">
          <span className="document-filter-label">並び替え</span>
          <select
            value={filter.sort}
            onChange={(event) => onChange({ sort: SORT_KEYS.find((k) => k === event.target.value) ?? 'default' })}
          >
            {SORT_KEYS.map((key) => (
              <option key={key} value={key}>
                {SORT_LABELS[key]}
              </option>
            ))}
          </select>
        </label>
      </section>
      <div className="document-filter-result">
        <p role="status">{resultCount > 0 ? `${resultCount} 件の資料が見つかりました` : '該当する資料が見つかりませんでした'}</p>
        {isFiltered(filter) && (
          <button type="button" className="btn btn-secondary btn-small" onClick={onClear}>
            条件をクリア
          </button>
        )}
      </div>
    </>
  );
}
