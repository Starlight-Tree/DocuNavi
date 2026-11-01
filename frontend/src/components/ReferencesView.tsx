import { useEffect, useRef } from 'react';
import { useOpenReference } from '../hooks/useOpenReference';
import { referenceCards } from '../lib/sources';
import type { SourceDoc } from '../lib/sources';
import { ReferenceCard } from './ReferenceCard';

interface ReferencesViewProps {
  /** 回答の出典（ページつき）。`weak` の分は、ここでも出さない。 */
  sources: SourceDoc[];
  onBack: () => void;
}

/** 参照資料の専用ページ。ひとつの回答の根拠にした資料を、抜粋つきのカードで並べる。押すと元の PDF を、その箇所のページで開く。 */
export function ReferencesView({ sources, onBack }: ReferencesViewProps) {
  const { opening, failureMessage, open } = useOpenReference(sources);
  const cards = referenceCards(sources);
  const titleRef = useRef<HTMLHeadingElement>(null);
  // 開いたら見出しへ。読み上げでも、画面が替わったことが分かる
  useEffect(() => {
    titleRef.current?.focus();
  }, []);

  return (
    <main className="references" aria-label="参照資料">
      <div className="references-head">
        <button type="button" className="btn btn-secondary" onClick={onBack}>
          ← チャットに戻る
        </button>
        <h2 className="references-title" ref={titleRef} tabIndex={-1}>
          この回答の参照資料（{cards.length} 件）
        </h2>
      </div>
      {failureMessage && (
        <p className="source-failure" role="alert">
          {failureMessage}
        </p>
      )}
      <div className="references-grid">
        {cards.map(({ source, index }) => (
          <ReferenceCard
            key={`${index}-${source.name}`}
            number={index + 1}
            source={source}
            variant="full"
            opening={opening === index}
            disabled={opening !== null}
            onOpen={() => void open(index)}
          />
        ))}
      </div>
      <p className="references-note">この回答の根拠にした資料だけを表示しています。すべての資料は「資料一覧」で確認できます。</p>
    </main>
  );
}
