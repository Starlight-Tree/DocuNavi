import { useEffect, useState } from 'react';
import { ChipSelect } from './ChipSelect';
import { QUERY_MAX_LENGTH, isFiltered, splitKeywords } from '../lib/documentFilter';
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

/** ラベルと、いまの値を並べたチップ。押すと選択肢が真下に開く。選ぶと強調する。 */
function FacetSelect<T extends string>({ label, value, options, onSelect }: FacetSelectProps<T>) {
  return (
    <ChipSelect
      label={label}
      value={value ?? ''}
      active={value !== null}
      options={options.map((o) => ({ value: o.value ?? '', label: o.label, count: o.count }))}
      onSelect={(next) => onSelect(options.find((o) => (o.value ?? '') === next)?.value ?? null)}
    />
  );
}

/**
 * 資料一覧の、検索・絞り込みと、件数。状態は持たない（URL が持つ）。
 * 操作は、検索・対象・資料種別（並び替えは、表の列名を押す）（検索欄とチップを 1 行に並べる。840px 以上は折り返さず、検索欄を先に縮め、値は「…」で省略する）。状態・ページ数・更新の欄は持たない（URL で指定された条件は、チップで見せて外せる）。
 * キーワードの入力欄だけ、日本語の変換中の文字が URL の往復で崩れないよう、手元の文字を持ち、URL が外から変わったときに合わせる。
 */
export function DocumentFilterBar({ filter, options, onChange, onClear }: DocumentFilterBarProps) {
  const [text, setText] = useState(filter.query);
  useEffect(() => {
    setText(filter.query);
  }, [filter.query]);
  // 有効な条件は、いつも見える形（チップ）で出す。×で、その条件だけ外せる
  const labelOf = <T extends string>(list: FacetOption<T>[], value: T | null) => list.find((o) => o.value === value)?.label ?? value ?? '';
  const chips: { key: string; text: string; clear: () => void }[] = [
    ...(splitKeywords(filter.query).length > 0 ? [{ key: 'query', text: `検索：${filter.query.trim()}`, clear: () => onChange({ query: '' }) }] : []),
    ...(filter.target !== null ? [{ key: 'target', text: `対象：${labelOf(options.target, filter.target)}`, clear: () => onChange({ target: null }) }] : []),
    ...(filter.docType !== null ? [{ key: 'docType', text: `種類：${labelOf(options.docType, filter.docType)}`, clear: () => onChange({ docType: null }) }] : []),
    ...(filter.status !== null ? [{ key: 'status', text: `状態：${labelOf(options.status, filter.status)}`, clear: () => onChange({ status: null }) }] : []),
    ...(filter.pages !== null ? [{ key: 'pages', text: `ページ数：${labelOf(options.pages, filter.pages)}`, clear: () => onChange({ pages: null }) }] : []),
    ...(filter.updated !== null ? [{ key: 'updated', text: `更新：${labelOf(options.updated, filter.updated)}`, clear: () => onChange({ updated: null }) }] : []),
  ];

  return (
    <>
      <section className="document-filter" aria-label="資料の検索・絞り込み・並び替え">
        <label className="document-filter-field document-filter-query">
          <span className="visually-hidden">資料名で検索（空白で AND）</span>
          <input
            type="search"
            value={text}
            maxLength={QUERY_MAX_LENGTH}
            placeholder="資料を検索…"
            title="空白で区切ると、すべてを含む資料だけに絞ります（AND）"
            onChange={(event) => {
              setText(event.target.value);
              onChange({ query: event.target.value }, 'replace');
            }}
          />
        </label>
        <FacetSelect label="対象" value={filter.target} options={options.target} onSelect={(target) => onChange({ target })} />
        <FacetSelect label="種類" value={filter.docType} options={options.docType} onSelect={(docType) => onChange({ docType })} />
      </section>
      {/* 有効な条件があるときだけ出る行（件数は見出しの横） */}
      {(chips.length > 0 || isFiltered(filter)) && (
        <div className="document-filter-result">
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
      )}
    </>
  );
}
