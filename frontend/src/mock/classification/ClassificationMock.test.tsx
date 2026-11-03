import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import ClassificationMock from './ClassificationMock';
import { DOCUMENTS, targetCatalog } from './data';
import { EMPTY_FILTER, UNCLASSIFIED, filterDocuments } from './Library';

function setup() {
  return userEvent.setup();
}

const rowsOf = () => within(screen.getByRole('table')).getAllByRole('row').slice(1);

describe('分類のモックのデータ', () => {
  it('承認された対応表のとおり、6 件が 1 資料 1 対象・1 資料種別に入る', () => {
    const six = DOCUMENTS.filter((d) => d.id <= 6);
    expect(six.map((d) => [d.id, d.target, d.docType])).toEqual([
      [1, 'minato-coffee', 'store-guide'],
      [2, 'aoba-aw820', 'manual'],
      [3, 'tsubasa-dorm', 'regulation'],
      [4, 'zahard', 'setting'],
      [5, 'zahard', 'setting'],
      [6, 'zahard', 'setting'],
    ]);
  });

  it('対象を選んだ一覧には、その対象の資料だけが出る（別の対象は混ざらない）', () => {
    for (const target of ['zahard', 'minato-coffee', 'tsubasa-dorm', 'aoba-aw820']) {
      const found = filterDocuments(DOCUMENTS, { ...EMPTY_FILTER, target }, null);
      expect(found.length).toBeGreaterThan(0);
      expect(found.every((d) => d.target === target)).toBe(true);
    }
    expect(filterDocuments(DOCUMENTS, { ...EMPTY_FILTER, target: UNCLASSIFIED }, null).map((d) => d.id)).toEqual([8]);
  });

  it('旧グループと対象は AND（片方を捨てて広げない）', () => {
    expect(filterDocuments(DOCUMENTS, { ...EMPTY_FILTER, target: 'aoba-aw820' }, 'guides').map((d) => d.id)).toEqual([2]);
    expect(filterDocuments(DOCUMENTS, { ...EMPTY_FILTER, target: 'aoba-aw820' }, 'cafe')).toEqual([]);
  });

  it('質問対象として選べるのは、見える資料のある対象だけ。取り込み中だけの資料の件数も数える', () => {
    const catalog = targetCatalog();
    expect(catalog.map((t) => [t.name, t.documentCount, t.readyCount])).toEqual([
      ['zahard', 3, 3],
      ['minato-coffee', 2, 1],
      ['tsubasa-dorm', 1, 1],
      ['aoba-aw820', 1, 1],
    ]);
    expect(targetCatalog(DOCUMENTS.filter((d) => d.target !== 'aoba-aw820')).map((t) => t.name)).not.toContain('aoba-aw820');
  });
});

describe('資料ライブラリ', () => {
  it('主な操作は 検索・対象・資料種別。状態・ページ数・更新・並び替えは「詳細フィルター」にある', async () => {
    const user = setup();
    render(<ClassificationMock />);
    expect(screen.getByRole('searchbox', { name: /資料名で検索/ })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '対象' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '資料種別' })).toBeInTheDocument();
    const toggle = screen.getByRole('button', { name: /詳細フィルター/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('combobox', { name: '状態' })).not.toBeInTheDocument(); // 閉じている間は出ない（hidden）

    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    for (const name of ['状態', 'ページ数', '更新', '並び替え']) expect(screen.getByRole('combobox', { name })).toBeInTheDocument();
  });

  it('対象を選ぶと、その対象の資料だけが表に出る。資料種別も重ねられる', async () => {
    const user = setup();
    render(<ClassificationMock />);
    expect(rowsOf()).toHaveLength(8);
    await user.selectOptions(screen.getByRole('combobox', { name: '対象' }), 'zahard');
    expect(rowsOf()).toHaveLength(3);
    expect(rowsOf().every((row) => within(row).getAllByText('ザーハード').length > 0)).toBe(true);
    await user.selectOptions(screen.getByRole('combobox', { name: '資料種別' }), 'manual');
    expect(screen.getByText('該当する資料が見つかりませんでした')).toBeInTheDocument();
  });

  it('詳細の条件が有効な間は、詳細フィルターを開いたままにして、件数を示す', async () => {
    const user = setup();
    render(<ClassificationMock />);
    await user.click(screen.getByRole('button', { name: /詳細フィルター/ }));
    await user.selectOptions(screen.getByRole('combobox', { name: '状態' }), 'processing');
    const toggle = screen.getByRole('button', { name: /詳細フィルター/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(toggle).toBeDisabled();
    expect(rowsOf()).toHaveLength(1);
  });

  it('旧 URL: 旧グループのフィルターが有効だと示し、解除できる（黙って無視しない）', async () => {
    const user = setup();
    render(<ClassificationMock />);
    await user.click(screen.getByRole('radio', { name: '旧 URL（collection）' }));
    expect(screen.getByText(/旧フィルターが有効です/)).toBeInTheDocument();
    expect(rowsOf()).toHaveLength(2); // 旧グループ cafe の資料（店舗案内・メニュー表）だけ
    await user.click(screen.getByRole('button', { name: '旧フィルターを解除' }));
    expect(screen.queryByText(/旧フィルターが有効です/)).not.toBeInTheDocument();
    expect(rowsOf()).toHaveLength(8);
  });

  it('旧 URL と対象が同時にあるときは、両方を適用して両方を示す', async () => {
    const user = setup();
    render(<ClassificationMock />);
    await user.click(screen.getByRole('radio', { name: '旧 URL + 対象' }));
    expect(screen.getByText(/旧フィルターが有効です/)).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '対象' })).toHaveValue('aoba-aw820');
    expect(rowsOf()).toHaveLength(1);
  });
});

describe('チャットの質問対象', () => {
  it('帯のメニューで選ぶ（入力欄の上のチップ列は無い）。会話が無ければ、そのまま切り替わる', async () => {
    const user = setup();
    render(<ClassificationMock />);
    await user.click(screen.getByRole('link', { name: 'チャット' }));
    expect(screen.queryByRole('group', { name: '検索範囲' })).not.toBeInTheDocument();
    const trigger = screen.getByRole('button', { name: /質問対象.*すべての資料/ });
    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    await user.click(screen.getByRole('menuitemradio', { name: /ザーハード/ }));
    expect(screen.getByRole('button', { name: /質問対象.*ザーハード（3件）/ })).toBeInTheDocument();
  });

  it('資料画面の「この対象について質問」は、対象を質問対象にしてチャットへ移る', async () => {
    const user = setup();
    render(<ClassificationMock />);
    await user.selectOptions(screen.getByRole('combobox', { name: '対象' }), 'tsubasa-dorm');
    await user.click(screen.getByRole('button', { name: 'この対象について質問' }));
    expect(screen.getByRole('button', { name: /質問対象.*つばさ大学学生寮（1件）/ })).toBeInTheDocument();
  });

  it('会話があるときに変えると確認が出て、確認すると新しい会話になる。取り消せば会話は残る', async () => {
    const user = setup();
    render(<ClassificationMock />);
    await user.click(screen.getByRole('link', { name: 'チャット' }));
    await user.click(screen.getByRole('button', { name: /質問対象/ }));
    await user.click(screen.getByRole('menuitemradio', { name: /ザーハード/ }));
    await user.type(screen.getByRole('textbox', { name: 'メッセージ' }), '大気について{Enter}');
    expect((await screen.findAllByText(/ザーハードの大気は薄く/)).length).toBeGreaterThan(0);

    await user.click(screen.getByRole('button', { name: /質問対象/ }));
    await user.click(screen.getByRole('menuitemradio', { name: /みなとコーヒー/ }));
    const dialog = screen.getByRole('alertdialog');
    expect(within(dialog).getByText(/質問対象を「みなとコーヒー」に変えますか/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'いまの会話を続ける' }));
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(screen.getAllByText(/ザーハードの大気は薄く/).length).toBeGreaterThan(0);

    await user.click(screen.getByRole('button', { name: /質問対象/ }));
    await user.click(screen.getByRole('menuitemradio', { name: /みなとコーヒー/ }));
    await user.click(screen.getByRole('button', { name: '新しい会話にする' }));
    expect(screen.queryAllByText(/ザーハードの大気は薄く/)).toHaveLength(0);
    expect(screen.getByRole('button', { name: /質問対象.*みなとコーヒー（2件）/ })).toBeInTheDocument();
  });

  it('旧グループの会話は「旧グループ」と示し、新しくは選べない。別の対象には確認を経て移れる', async () => {
    const user = setup();
    render(<ClassificationMock />);
    await user.click(screen.getByRole('radio', { name: '旧グループの会話を開いた' }));
    const trigger = screen.getByRole('button', { name: /質問対象.*旧グループ: 規程・取扱説明書/ });
    await user.click(trigger);
    expect(screen.getByRole('menuitemradio', { name: /旧グループ: 規程・取扱説明書/ })).toBeDisabled();
    await user.click(screen.getByRole('menuitemradio', { name: /あおば電機/ }));
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
  });

  it('対象が使えなくなった会話は、「すべて」とは別に、利用できないと示す', async () => {
    const user = setup();
    render(<ClassificationMock />);
    await user.click(screen.getByRole('radio', { name: '対象が使えない会話' }));
    expect(screen.getByText(/この会話の対象は、いま利用できません/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /質問対象.*この会話の対象/ })).toBeInTheDocument();
  });
});
