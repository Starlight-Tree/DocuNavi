import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { QUERY_MAX_LENGTH, SORT_KEYS, SORT_LABELS, isFiltered, splitKeywords } from '../lib/documentFilter';
import type { DocumentFilter, FacetOption, PageBand, UpdatedBand } from '../lib/documentFilter';

export interface DocumentFilterOptions {
  target: FacetOption[];
  docType: FacetOption[];
  status: FacetOption[];
  pages: FacetOption<PageBand>[];
  updated: FacetOption<UpdatedBand>[];
}

interface DocumentFilterBarProps {
  filter: DocumentFilter;
  options: DocumentFilterOptions;
  /** 件数と回答対象を 1 行にまとめた表示（呼び出し側が組み立てる）。 */
  summary: ReactNode;
  /** 条件を変える。`replace` は、入力の途中のように、履歴を増やしたくない変更。 */
  onChange: (next: Partial<DocumentFilter>, mode?: 'push' | 'replace') => void;
  onClear: () => void;
  /** 絞り込みの欄と件数の行の間に置くもの（旧フィルターの表示など）。 */
  notice?: ReactNode;
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
 * 資料一覧の、検索・絞り込み・並び替えと、件数。状態は持たない（URL が持つ）。
 * 操作は、検索・対象・資料種別・並び替え。状態・ページ数・更新の欄は持たない（URL で指定された条件は、チップで見せて外せる）。
 * キーワードの入力欄だけ、日本語の変換中の文字が URL の往復で崩れないよう、手元の文字を持ち、URL が外から変わったときに合わせる。
 */
export function DocumentFilterBar({ filter, options, summary, onChange, onClear, notice }: DocumentFilterBarProps) {
  const [text, setText] = useState(filter.query);
  useEffect(() => {
    setText(filter.query);
  }, [filter.query]);
  // 有効な条件は、いつも見える形（チップ）で出す。×で、その条件だけ外せる
  const labelOf = <T extends string>(list: FacetOption<T>[], value: T | null) => list.find((o) => o.value === value)?.label ?? value ?? '';
  const chips: { key: string; text: string; clear: () => void }[] = [
    ...(splitKeywords(filter.query).length > 0 ? [{ key: 'query', text: `検索：${filter.query.trim()}`, clear: () => onChange({ query: '' }) }] : []),
    ...(filter.target !== null ? [{ key: 'target', text: `対象：${labelOf(options.target, filter.target)}`, clear: () => onChange({ target: null }) }] : []),
    ...(filter.docType !== null ? [{ key: 'docType', text: `資料種別：${labelOf(options.docType, filter.docType)}`, clear: () => onChange({ docType: null }) }] : []),
    ...(filter.status !== null ? [{ key: 'status', text: `状態：${labelOf(options.status, filter.status)}`, clear: () => onChange({ status: null }) }] : []),
    ...(filter.pages !== null ? [{ key: 'pages', text: `ページ数：${labelOf(options.pages, filter.pages)}`, clear: () => onChange({ pages: null }) }] : []),
    ...(filter.updated !== null ? [{ key: 'updated', text: `更新：${labelOf(options.updated, filter.updated)}`, clear: () => onChange({ updated: null }) }] : []),
  ];

  return (
    <>
      <section className="document-filter" aria-label="資料の検索・絞り込み・並び替え">
        <label className="document-filter-field document-filter-query">
          <span className="document-filter-label">資料名で検索（空白で AND）</span>
          <input
            type="search"
            value={text}
            maxLength={QUERY_MAX_LENGTH}
            placeholder="資料名・ファイル名・対象"
            onChange={(event) => {
              setText(event.target.value);
              onChange({ query: event.target.value }, 'replace');
            }}
          />
        </label>
        <FacetSelect label="対象" value={filter.target} options={options.target} onSelect={(target) => onChange({ target })} />
        <FacetSelect label="資料種別" value={filter.docType} options={options.docType} onSelect={(docType) => onChange({ docType })} />
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
      {notice}
      <div className="document-filter-result">
        {summary}
        {chips.length > 0 && (
          <ul className="document-chips" aria-label="有効な条件">
            {chips.map((chip) => (
              <li key={chip.key} className="document-chip">
                <span>{chip.text}</span>
                <button type="button" aria-label={`${chip.text} を外す`} onClick={chip.clear}>
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
        {isFiltered(filter) && (
          <button type="button" className="btn btn-secondary btn-small" onClick={onClear}>
            条件をクリア
          </button>
        )}
      </div>
    </>
  );
}
