import { parseSources } from '../lib/sources';

interface SourcesPanelProps {
  context: string;
}

/** 回答の根拠にした設定資料。出典の見出しが無い context（未検索・該当なしの定型文）では何も出さない。 */
export function SourcesPanel({ context }: SourcesPanelProps) {
  const sources = parseSources(context);
  if (sources.length === 0) return null;

  return (
    <details className="sources">
      <summary>参照した設定資料（{sources.length} 件）</summary>
      <div className="sources-body">
        {sources.map((source, index) => (
          <section key={`${index}-${source.name}`} className="source">
            <h3 className="source-name">{source.name}</h3>
            {source.text && <p className="source-text">{source.text}</p>}
          </section>
        ))}
      </div>
    </details>
  );
}
