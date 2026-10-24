import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderMarkdown } from './markdown';

function show(text: string) {
  return render(<div data-testid="md">{renderMarkdown(text)}</div>);
}

describe('renderMarkdown', () => {
  it('箇条書きと番号付きの一覧にする', () => {
    show('- りんご\n- みかん\n\n1. 一つ目\n2. 二つ目');
    const lists = screen.getAllByRole('list');
    expect(within(lists[0]!).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['りんご', 'みかん']);
    expect(lists[1]!.tagName).toBe('OL');
    expect(within(lists[1]!).getAllByRole('listitem')).toHaveLength(2);
  });

  it('太字・斜体・インラインコードにする', () => {
    show('これは **重要** で *強調* して `code` です');
    const root = screen.getByTestId('md');
    expect(root.querySelector('strong')!.textContent).toBe('重要');
    expect(root.querySelector('em')!.textContent).toBe('強調');
    expect(root.querySelector('code')!.textContent).toBe('code');
  });

  it('表にする（見出しの行と、区切りの行）', () => {
    show('| 項目 | 値 |\n| --- | --- |\n| 定員 | 120 名 |\n| 期間 | 1 年 |');
    const table = screen.getByRole('table');
    expect(within(table).getAllByRole('columnheader').map((c) => c.textContent)).toEqual(['項目', '値']);
    expect(within(table).getAllByRole('row')).toHaveLength(3);
    expect(within(table).getByRole('cell', { name: '120 名' })).toBeInTheDocument();
  });

  it('見出しとコードブロック', () => {
    show('# 概要\n本文\n\n```\nconst a = 1;\n```');
    expect(screen.getByRole('heading', { name: '概要' })).toBeInTheDocument();
    expect(screen.getByTestId('md').querySelector('pre code')!.textContent).toBe('const a = 1;');
  });

  it('HTML・リンク・画像は解釈せず、書かれたまま見せる', () => {
    const { container } = show('<script>alert(1)</script> <b>太字</b> [x](javascript:alert(1)) ![i](http://e/a.png)');
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(container.querySelector('a')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('<script>alert(1)</script>');
    expect(container.textContent).toContain('[x](javascript:alert(1))');
  });

  it('生成の途中の、閉じていない記法は、そのまま文字で見える', () => {
    const { container } = show('途中の **太字 と `コード');
    expect(container.querySelector('strong')).toBeNull();
    expect(container.textContent).toBe('途中の **太字 と `コード');
  });

  it('「2 * 3 * 4」のような式を斜体にしない', () => {
    const { container } = show('計算は 2 * 3 * 4 です');
    expect(container.querySelector('em')).toBeNull();
    expect(container.textContent).toBe('計算は 2 * 3 * 4 です');
  });

  it('1 つの改行は改行として見せ、空行で段落を分ける', () => {
    const { container } = show('一行目\n二行目\n\n次の段落');
    expect(container.querySelectorAll('p')).toHaveLength(2);
    expect(container.querySelectorAll('br')).toHaveLength(1);
  });

  it('閉じていないコードブロックも、最後まで出す（生成の途中）', () => {
    const { container } = show('```\nabc');
    expect(container.querySelector('pre code')!.textContent).toBe('abc');
  });
});
