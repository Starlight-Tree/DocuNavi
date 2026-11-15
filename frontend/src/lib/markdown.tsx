import type { ReactNode } from 'react';
import { hideOpenCitation, splitCitations } from './citations';
import type { CitationRef } from './citations';
import { sourceLabel } from './sources';
import type { SourceDoc } from './sources';

/**
 * 回答の本文（Markdown の一部）を、React の要素にする。
 *
 * 対応: 見出し（# ）・箇条書き（- / 1.）・表・コードブロック・太字・斜体・インラインコード。
 * 本文は必ずテキストとして描画する（HTML として解釈しない）。リンクと画像は解釈せず、書かれたまま見せる
 * （`javascript:` などの URL を押せる形にしない）。生成の途中の、閉じていない記法は、そのまま文字として見える。
 */

/** 回答の文末の引用（`[1]` `[1「語句」]`）を、押せる番号にするための材料。無ければ、引用の記法も本文として見せる。 */
export interface CitationOptions {
  sources: SourceDoc[];
  /** `referenceTargets` の結果（出典の添字 → 参照資料の項目の添字）。 */
  targets: ReadonlyMap<number, number>;
  /** 押したとき。`opener` は押した要素。 */
  onOpen: (ref: CitationRef, opener: Element | null) => void;
  /** 開いている最中の出典の添字（無ければ null）。 */
  opening: number | null;
  /** 生成の途中か。途中なら、末尾の閉じていない引用を見せない。 */
  streaming: boolean;
}

type Block =
  | { type: 'code'; text: string }
  | { type: 'heading'; text: string }
  | { type: 'list'; ordered: boolean; items: string[] }
  | { type: 'table'; head: string[]; rows: string[][] }
  | { type: 'paragraph'; lines: string[] };

const BULLET = /^\s*[-*+]\s+(.*)$/;
const ORDERED = /^\s*\d+[.)]\s+(.*)$/;
const HEADING = /^\s*#{1,6}\s+(.*)$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  const flush = () => {
    if (paragraph.length > 0) blocks.push({ type: 'paragraph', lines: paragraph });
    paragraph = [];
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i] ?? '';

    if (line.trimStart().startsWith('```')) {
      flush();
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !(lines[i] ?? '').trimStart().startsWith('```')) {
        body.push(lines[i] ?? '');
        i += 1;
      }
      blocks.push({ type: 'code', text: body.join('\n') });
      continue;
    }

    if (line.trim() === '') {
      flush();
      continue;
    }

    const next = lines[i + 1];
    if (line.includes('|') && next !== undefined && TABLE_SEPARATOR.test(next) && next.includes('-')) {
      flush();
      const head = splitRow(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && (lines[i] ?? '').includes('|') && (lines[i] ?? '').trim() !== '') {
        rows.push(splitRow(lines[i] ?? ''));
        i += 1;
      }
      i -= 1;
      blocks.push({ type: 'table', head, rows });
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      flush();
      blocks.push({ type: 'heading', text: heading[1] ?? '' });
      continue;
    }

    const bullet = BULLET.exec(line);
    const ordered = bullet ? null : ORDERED.exec(line);
    const item = bullet ?? ordered;
    if (item) {
      flush();
      const isOrdered = ordered !== null;
      const last = blocks[blocks.length - 1];
      if (last?.type === 'list' && last.ordered === isOrdered) last.items.push(item[1] ?? '');
      else blocks.push({ type: 'list', ordered: isOrdered, items: [item[1] ?? ''] });
      continue;
    }

    paragraph.push(line);
  }
  flush();
  return blocks;
}

// 太字・斜体・インラインコード。`*` の前後が空白なら、記法とみなさない（「2 * 3 * 4」を壊さない）
const INLINE = /(`[^`\n]+`|\*\*[^*\n]+?\*\*|\*[^*\s][^*\n]*?\*)/g;

function citationLabel(ref: CitationRef, sources: SourceDoc[]): string {
  const source = sources[ref.index];
  const pages = source?.page === undefined ? '' : source.pageEnd === undefined ? ` p.${source.page}` : ` p.${source.page}–${source.pageEnd}`;
  return `参照資料 ${ref.number}${source ? `: ${sourceLabel(source)}${pages}` : ''}を右のパネルで開く${ref.span ? '（根拠の語句を強調）' : ''}`;
}

function renderCitations(text: string, cite: CitationOptions, keyBase: string): ReactNode[] {
  return splitCitations(text, cite.sources, cite.targets).map((part, index) => {
    if (typeof part === 'string') return part;
    return (
      <span key={`${keyBase}-${index}`} className="cite-group">
        {part.refs.map((ref) => (
          <button
            key={`${ref.index}-${ref.span ?? ''}`}
            type="button"
            className="cite"
            aria-label={citationLabel(ref, cite.sources)}
            disabled={cite.opening !== null}
            onClick={(event) => cite.onOpen(ref, event.currentTarget)}
          >
            <sup>{ref.number}</sup>
          </button>
        ))}
      </span>
    );
  });
}

function renderInline(text: string, cite?: CitationOptions): ReactNode[] {
  return text.split(INLINE).map((part, index) => {
    if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
      return <code key={index}>{part.slice(1, -1)}</code>;
    }
    if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) {
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    }
    if (part.length > 2 && part.startsWith('*') && part.endsWith('*')) {
      return <em key={index}>{part.slice(1, -1)}</em>;
    }
    return cite ? <span key={index}>{renderCitations(part, cite, String(index))}</span> : part;
  });
}

export function renderMarkdown(text: string, cite?: CitationOptions): ReactNode {
  // 出典が無い回答（未検索・該当なし）は、引用を読まない（本文の「[1]」を消さない）
  const active = cite && cite.sources.length > 0 ? cite : undefined;
  const body = active?.streaming ? hideOpenCitation(text) : text;
  return parseBlocks(body).map((block, index) => {
    switch (block.type) {
      case 'code':
        return (
          <pre key={index} className="md-code">
            <code>{block.text}</code>
          </pre>
        );
      case 'heading':
        // 回答は 1 つの発言の中なので、見出しの階層は 1 つにそろえる（画面の見出しの構造を乱さない）
        return (
          <p key={index} className="md-heading" role="heading" aria-level={4}>
            {renderInline(block.text, active)}
          </p>
        );
      case 'list': {
        const Tag = block.ordered ? 'ol' : 'ul';
        return (
          <Tag key={index}>
            {block.items.map((item, i) => (
              <li key={i}>{renderInline(item, active)}</li>
            ))}
          </Tag>
        );
      }
      case 'table':
        return (
          <div key={index} className="md-table-wrap">
            <table>
              <thead>
                <tr>
                  {block.head.map((cell, i) => (
                    <th key={i} scope="col">
                      {renderInline(cell, active)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, r) => (
                  <tr key={r}>
                    {row.map((cell, c) => (
                      <td key={c}>{renderInline(cell, active)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      case 'paragraph':
        return (
          <p key={index}>
            {block.lines.map((line, i) => (
              <span key={i}>
                {i > 0 && <br />}
                {renderInline(line, active)}
              </span>
            ))}
          </p>
        );
    }
  });
}
