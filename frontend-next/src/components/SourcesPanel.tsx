import { useState } from 'react';
import { useOpenSource } from '../hooks/useDocuments';
import type { OpenSourceResult } from '../hooks/useDocuments';
import { formatPages, parseSources, withPages } from '../lib/sources';
import type { SourceRef } from '../lib/sources';

interface SourcesPanelProps {
  context: string;
  /** サーバーが検索で返した、出典ごとのページ。無ければ（古い応答）、開くときに抜粋から探す。 */
  sources?: SourceRef[];
}

const MSG_NOT_FOUND = '資料一覧に見つかりませんでした。';
const MSG_ERROR = '資料を開けませんでした。しばらく待ってから、もう一度お試しください。';

/**
 * 回答の根拠にした資料。出典のチップを押すと、資料の元PDF を、その箇所のページで開く。
 * 抜粋の全文は、折りたたみの中で見られる。出典の見出しが無い context（未検索・該当なしの定型文）では何も出さない。
 */
export function SourcesPanel({ context, sources: refs }: SourcesPanelProps) {
  const sources = withPages(parseSources(context), refs);
  const openSource = useOpenSource();
  const [opening, setOpening] = useState<number | null>(null);
  const [failure, setFailure] = useState<Exclude<OpenSourceResult, 'opened'> | null>(null);
  if (sources.length === 0) return null;
  // 1 位と類似度が離れた 2 件目以降は、カードに出さない（番号は元の順のまま。抜粋の一覧には全部残す）
  const cards = sources.map((source, index) => ({ source, index })).filter(({ source }) => !source.weak);

  const open = async (index: number) => {
    const source = sources[index];
    if (!source || opening !== null) return;
    setOpening(index);
    setFailure(null);
    const result = await openSource(source.name, source.text, source.page);
    setOpening(null);
    if (result !== 'opened') setFailure(result);
  };

  return (
    <div className="sources">
      <p className="source-cards-label">出典</p>
      <div className="source-cards" role="group" aria-label="出典（押すと元のPDFを開きます）">
        {cards.map(({ source, index }) => (
          <button
            key={`${index}-${source.name}`}
            type="button"
            className="source-card"
            disabled={opening !== null}
            onClick={() => void open(index)}
          >
            <span className="source-card-head">
              <span className="source-chip-number" aria-hidden="true">
                {index + 1}
              </span>
              <span className="source-card-name">{source.name}</span>
              {formatPages(source.page, source.pageEnd) && (
                <span className="source-card-page">{formatPages(source.page, source.pageEnd)}</span>
              )}
            </span>
            <span className="source-card-text">{source.text}</span>
            <span className="source-card-open">{opening === index ? '開いています…' : 'PDF を開く →'}</span>
          </button>
        ))}
      </div>
      {failure && (
        <p className="source-failure" role="alert">
          {failure === 'notfound' ? MSG_NOT_FOUND : MSG_ERROR}
        </p>
      )}
      <details className="source-excerpts">
        <summary>参照した設定資料の抜粋（{sources.length} 件）</summary>
        <div className="sources-body">
          {sources.map((source, index) => (
            <section key={`${index}-${source.name}`} className="source">
              <h3 className="source-name">{source.name}</h3>
              {source.text && <p className="source-text">{source.text}</p>}
            </section>
          ))}
        </div>
      </details>
    </div>
  );
}
