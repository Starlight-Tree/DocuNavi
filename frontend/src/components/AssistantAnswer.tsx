import { useMemo } from 'react';
import type { ReactNode } from 'react';
import { useOpenReference } from '../hooks/useOpenReference';
import { hideOpenCitation, stripCitations } from '../lib/citations';
import { renderMarkdown } from '../lib/markdown';
import { parseSources, referenceTargets, withPages } from '../lib/sources';
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
 * アシスタントの回答。本文の文末の引用（`[1]` `[1「根拠の語句」]`）を、押せる上付きの番号にし、回答の下の操作の行にある「参照資料」と同じ開き方でつなぐ。
 * 出典が無い回答では、引用の記法を本文から消す（LLM が出典の無い回答に付けた番号を、見せない）。
 */
export function AssistantAnswer({ text, context, sources: refs, generating, children, actions }: AssistantAnswerProps) {
  const docs = useMemo(() => withPages(parseSources(context ?? ''), refs), [context, refs]);
  const targets = useMemo(() => referenceTargets(docs), [docs]);
  const reference = useOpenReference(docs);

  const cite =
    docs.length > 0
      ? {
          sources: docs,
          targets,
          opening: reference.opening,
          streaming: generating,
          onOpen: (ref: { index: number; span: string | null }, opener: Element | null) => void reference.open(ref.index, ref.span, opener),
        }
      : undefined;
  const showSources = docs.length > 0 && !generating;
  const shown = cite ? text : hideOpenCitation(stripCitations(text));

  return (
    <>
      {/* 本文は必ずテキストとして描画する（HTML として解釈しない） */}
      <div className="message-text markdown">{renderMarkdown(shown, cite)}</div>
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
