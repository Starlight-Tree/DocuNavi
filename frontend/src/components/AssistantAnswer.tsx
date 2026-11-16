import { useMemo } from 'react';
import type { ReactNode } from 'react';
import { useOpenReference } from '../hooks/useOpenReference';
import { hideOpenCitation, stripCitations } from '../lib/citations';
import { renderMarkdown } from '../lib/markdown';
import { parseSources, withPages } from '../lib/sources';
import type { SourceRef } from '../lib/sources';
import { SourcesPanel } from './SourcesPanel';

interface AssistantAnswerProps {
  text: string;
  /** 検索結果（出典の見出し + 本文）。無ければ（未検索・古い履歴）、出典も引用番号も出さない。 */
  context?: string;
  /** サーバーが検索で返した、出典ごとのページ・資料の ID。無ければ（古い応答）、開くときに抜粋・名前から探す。 */
  sources?: SourceRef[];
  /** 生成の途中か。途中は、出典（回答の終わりに届く）がまだ無いので、引用の記法を見せない。 */
  generating: boolean;
  /** 本文のすぐ下に出すもの（例: 対象内に資料が無かったときの案内）。操作の行より上。 */
  children?: ReactNode;
  /** 回答の下の操作の行の先頭に置くもの（コピー）。「参照資料」のボタンは、その右に続く。 */
  actions?: ReactNode;
}

/**
 * アシスタントの回答。本文の文末の引用（`[1]` `[1「根拠の語句」]`）は見せず消す。出典は、回答の下の操作の行の「N 件のソース」から開く。
 */
export function AssistantAnswer({ text, context, sources: refs, generating, children, actions }: AssistantAnswerProps) {
  const docs = useMemo(() => withPages(parseSources(context ?? ''), refs), [context, refs]);
  const reference = useOpenReference(docs);

  const showSources = docs.length > 0 && !generating;
  // 本文に引用の番号は出さない（出典は、回答の下の「N 件のソース」から開く）。LLM が付けた `[n]` は、生成の途中のものも含めて消す
  const shown = hideOpenCitation(stripCitations(text));

  return (
    <>
      {/* 本文は必ずテキストとして描画する（HTML として解釈しない） */}
      <div className="message-text markdown">{renderMarkdown(shown)}</div>
      {children}
      {(actions || showSources) && (
        <div className="message-actions">
          {actions}
          {showSources && <SourcesPanel sources={docs} reference={reference} />}
        </div>
      )}
    </>
  );
}
