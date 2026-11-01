import { useOpenReference } from '../hooks/useOpenReference';
import { parseSources, referenceCards, REFERENCE_PREVIEW_LIMIT, withPages } from '../lib/sources';
import type { SourceDoc, SourceRef } from '../lib/sources';
import { ReferenceCard } from './ReferenceCard';

interface SourcesPanelProps {
  context: string;
  /** サーバーが検索で返した、出典ごとのページ。無ければ（古い応答）、開くときに抜粋から探す。 */
  sources?: SourceRef[];
  /** 「参照資料をすべて見る」を押したとき。この回答の出典（ページつき）を渡す。渡さなければリンクを出さない。 */
  onShowAll?: ((sources: SourceDoc[]) => void) | undefined;
}

/**
 * 回答の根拠にした資料。最大 3 枚のカードを出し、押すと資料の元PDF を、その箇所のページで開く。
 * 「参照資料をすべて見る」で専用ページへ。抜粋の全文は、折りたたみの中で見られる。
 * 出典の見出しが無い context（未検索・該当なしの定型文）では何も出さない。
 */
export function SourcesPanel({ context, sources: refs, onShowAll }: SourcesPanelProps) {
  const sources = withPages(parseSources(context), refs);
  const { opening, failureMessage, open } = useOpenReference(sources);
  if (sources.length === 0) return null;
  // 1 位と類似度が離れた 2 件目以降は、カードに出さない（番号は元の順のまま。抜粋の一覧には全部残す）
  const cards = referenceCards(sources);
  const shown = cards.slice(0, REFERENCE_PREVIEW_LIMIT);

  return (
    <div className="sources">
      <div className="reference-heading">
        <p className="source-cards-label">参照資料（{cards.length} 件）</p>
        {onShowAll && cards.length > 0 && (
          <button type="button" className="reference-all" onClick={() => onShowAll(sources)}>
            参照資料をすべて見る →
          </button>
        )}
      </div>
      <div className="source-cards" role="group" aria-label="参照資料（押すと元のPDFを開きます）">
        {shown.map(({ source, index }) => (
          <ReferenceCard
            key={`${index}-${source.name}`}
            number={index + 1}
            source={source}
            variant="compact"
            opening={opening === index}
            disabled={opening !== null}
            onOpen={() => void open(index)}
          />
        ))}
      </div>
      {failureMessage && (
        <p className="source-failure" role="alert">
          {failureMessage}
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
