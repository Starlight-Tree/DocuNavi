import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import MockApp from './MockApp';
import { INGEST_MS, REPLY_DELAY_MS } from './data';

afterEach(() => {
  vi.useRealTimers();
});

function setup() {
  return userEvent.setup();
}

async function openDocuments(user: ReturnType<typeof setup>) {
  await user.click(screen.getByRole('button', { name: /^資料/ }));
  return screen.getByRole('table');
}

describe('画面の構成', () => {
  it('チャットで始まり、サイドバーだけで画面を切り替える（上部のタブはない）', async () => {
    const user = setup();
    render(<MockApp />);
    expect(screen.getByRole('heading', { name: '何を調べますか？' })).toBeInTheDocument();
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'チャット' })).toHaveAttribute('aria-current', 'page');

    await openDocuments(user);
    expect(screen.getByRole('heading', { name: '資料', level: 1 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^資料/ })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: 'チャット' })).not.toHaveAttribute('aria-current');
  });

  it('資料画面は、同じ資料名・状態を表で見せる。検索と状態フィルターで絞れる', async () => {
    const user = setup();
    render(<MockApp />);
    const table = await openDocuments(user);
    expect(within(table).getAllByRole('row')).toHaveLength(9); // 見出し + 8 件
    expect(within(table).getByText('取引先リスト_スキャン.pdf')).toBeInTheDocument();
    expect(within(table).getByText(/テキストを抽出できませんでした/)).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText('状態'), 'failed');
    expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(2);

    await user.selectOptions(screen.getByLabelText('状態'), 'all');
    await user.type(screen.getByLabelText('資料名で検索'), '存在しない資料');
    expect(screen.getByText('条件に合う資料がありません')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '条件をクリア' }));
    expect(screen.getByRole('table')).toBeInTheDocument();
  });
});

describe('質問対象の選択', () => {
  it('取り込み中・エラーの資料は選べず理由が出る。0 件では適用できない', async () => {
    const user = setup();
    render(<MockApp />);
    await user.click(screen.getByRole('button', { name: /質問対象/ }));
    const dialog = screen.getByRole('dialog', { name: '質問対象の資料' });
    await user.click(within(dialog).getByRole('radio', { name: /資料を選択/ }));

    expect(within(dialog).getByRole('button', { name: '適用' })).toBeDisabled();
    expect(within(dialog).getByRole('checkbox', { name: /製品仕様書_v2/ })).toBeDisabled();
    expect(within(dialog).getByText(/取り込み中のため選べません/)).toBeInTheDocument();
    expect(within(dialog).getByRole('checkbox', { name: /取引先リスト_スキャン/ })).toBeDisabled();
    expect(within(dialog).getByText(/エラーのため選べません/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole('checkbox', { name: /契約条件/ }));
    expect(within(dialog).getByText(/1件を選択中/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: '適用' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /質問対象/ })).toHaveTextContent('選択した資料 1件');
  });

  it('Esc でキャンセルでき、変更は反映されない', async () => {
    const user = setup();
    render(<MockApp />);
    await user.click(screen.getByRole('button', { name: /質問対象/ }));
    await user.click(screen.getByRole('radio', { name: /資料を選択/ }));
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /質問対象/ })).toHaveTextContent('すべての資料');
    expect(screen.getByRole('button', { name: /質問対象/ })).toHaveFocus();
  });
});

describe('質問・回答・出典', () => {
  it('質問すると回答と出典が出て、出典を押すと右パネルで根拠が強調され、会話は残る', async () => {
    const user = setup();
    render(<MockApp />);
    await user.type(screen.getByLabelText('質問'), '経費の締め日はいつ？');
    await user.click(screen.getByRole('button', { name: '送信' }));
    expect(screen.getByText('経費の締め日はいつ？', { selector: '.mk-bubble' })).toBeInTheDocument();
    expect(screen.getByText(/回答を作成しています/)).toBeInTheDocument();

    const [source] = await screen.findAllByRole('button', { name: /経費精算ガイド\.pdf/ }, { timeout: REPLY_DELAY_MS + 2000 });
    if (!source) throw new Error('出典のボタンがありません');
    expect(screen.getByText('モック応答')).toBeInTheDocument();
    await user.click(source);

    const panel = screen.getByRole('complementary', { name: /回答の出典: 経費精算ガイド\.pdf/ });
    expect(within(panel).getByText(/出典: p\.2/)).toBeInTheDocument();
    const mark = panel.querySelector('mark');
    expect(mark).toHaveTextContent('発生日の翌月5日まで');
    expect(source).toHaveAttribute('aria-pressed', 'true');
    // 会話は残っている
    expect(screen.getByText('経費の締め日はいつ？', { selector: '.mk-bubble' })).toBeInTheDocument();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
    expect(screen.getByText('経費の締め日はいつ？', { selector: '.mk-bubble' })).toBeInTheDocument();
  });

  it('資料詳細と出典は、同じパネルで表示する', async () => {
    const user = setup();
    render(<MockApp />);
    const table = await openDocuments(user);
    await user.click(within(table).getByRole('button', { name: '契約条件.pdf' }));
    const panel = screen.getByRole('complementary', { name: /資料の詳細: 契約条件\.pdf/ });
    expect(within(panel).getByText('PDF')).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: 'この資料で質問' })).toBeEnabled();
    expect(panel.querySelector('mark')).toBeNull();
  });
});

describe('アップロードと取り込み', () => {
  it('資料画面から追加すると、取り込み中として一覧に出て、時間が経つと利用可能になる', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // ブラウザーの accept の絞り込みは外し、ダイアログ側の形式の検査を確かめる
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime, applyAccept: false });
    render(<MockApp />);
    await openDocuments(user);

    await user.click(screen.getAllByRole('button', { name: /資料を追加/ })[0] as HTMLElement);
    const dialog = screen.getByRole('dialog', { name: '資料を追加' });
    expect(within(dialog).getByRole('button', { name: '追加' })).toBeDisabled();
    expect(within(dialog).getByText(/会話だけの一時的な添付はありません/)).toBeInTheDocument();

    const input = within(dialog).getByTestId('mk-file-input');
    await user.upload(input, [new File(['x'], '新製品の資料.pdf'), new File(['x'], '禁止.exe')]);
    expect(within(dialog).getByRole('alert')).toHaveTextContent('禁止.exe');
    expect(within(dialog).getByText('新製品の資料.pdf')).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: '「新製品の資料.pdf」を外す' }));
    expect(within(dialog).queryByText('新製品の資料.pdf')).not.toBeInTheDocument();
    await user.upload(input, new File(['x'], '新製品の資料.pdf'));
    await user.click(within(dialog).getByRole('button', { name: '1件を追加' }));

    const row = screen.getByRole('row', { name: /新製品の資料\.pdf/ });
    expect(within(row).getByText('取り込み中')).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(INGEST_MS + 1000);
    });
    expect(within(screen.getByRole('row', { name: /新製品の資料\.pdf/ })).getByText('利用可能')).toBeInTheDocument();
  });

  it('チャットから追加すると、取り込み中は質問対象として使えない旨を出し、完了後に質問できる', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<MockApp />);

    // 先に「製品仕様書_v2」だけを選ぶ（取り込み中のため選べないので、まず契約条件を選んでおく）
    await user.click(screen.getByRole('button', { name: /質問対象/ }));
    await user.click(screen.getByRole('radio', { name: /資料を選択/ }));
    await user.click(screen.getByRole('checkbox', { name: /契約条件/ }));
    await user.click(screen.getByRole('button', { name: '適用' }));

    await user.click(screen.getByRole('button', { name: /資料を追加/ }));
    const dialog = screen.getByRole('dialog', { name: '資料を追加' });
    expect(within(dialog).getByText(/この会話の質問対象にも入ります/)).toBeInTheDocument();
    await user.upload(within(dialog).getByTestId('mk-file-input'), new File(['x'], '追加資料.docx'));
    await user.click(within(dialog).getByRole('button', { name: '1件を追加' }));

    expect(screen.getByRole('button', { name: /質問対象/ })).toHaveTextContent('選択した資料 2件');
    expect(screen.getByText(/「追加資料\.docx」を取り込み中です。完了後に質問対象になります/)).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(INGEST_MS + 1000);
    });
    await waitFor(() => expect(screen.queryByText(/を取り込み中です。完了後に質問対象になります/)).not.toBeInTheDocument());
    expect(screen.getByText(/「追加資料\.docx」が利用可能になりました/)).toBeInTheDocument();
  });
});

describe('削除', () => {
  it('操作メニューから削除すると確認が出て、確定したときだけ一覧から消える', async () => {
    const user = setup();
    render(<MockApp />);
    const table = await openDocuments(user);

    await user.click(within(table).getByRole('button', { name: '「会議メモ.docx」の操作' }));
    await user.click(screen.getByRole('menuitem', { name: /削除/ }));
    const dialog = screen.getByRole('dialog', { name: '資料を削除しますか？' });
    expect(within(dialog).getByRole('button', { name: 'キャンセル' })).toHaveFocus();

    await user.click(within(dialog).getByRole('button', { name: 'キャンセル' }));
    expect(screen.getByText('会議メモ.docx')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '「会議メモ.docx」の操作' }));
    await user.click(screen.getByRole('menuitem', { name: /削除/ }));
    await user.click(screen.getByRole('button', { name: '削除する' }));
    expect(within(screen.getByRole('table')).queryByText('会議メモ.docx')).not.toBeInTheDocument();
  });

  it('「この資料で質問」は、その資料だけを選んだ会話を開く', async () => {
    const user = setup();
    render(<MockApp />);
    const table = await openDocuments(user);
    await user.click(within(table).getByRole('button', { name: '「就業規則.pdf」の操作' }));
    await user.click(screen.getByRole('menuitem', { name: /この資料で質問/ }));
    expect(screen.getByRole('button', { name: 'チャット' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: /質問対象/ })).toHaveTextContent('選択した資料 1件');
  });
});
