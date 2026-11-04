import { expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import type userEvent from '@testing-library/user-event';

type User = ReturnType<typeof userEvent.setup>;

/** 左下のメニューの「ナレッジ」で、資料一覧を開く（上部に「資料」の入口は無い）。 */
export async function openKnowledge(user: User) {
  await waitFor(() => expect(document.querySelector('.ws-account-button')).not.toBeNull());
  await user.click(document.querySelector('.ws-account-button') as HTMLElement);
  await user.click(await screen.findByRole('menuitem', { name: 'ナレッジ' }));
}

/** ロゴ（「DocuNavi」）で、チャットへ戻る（上部に「チャット」の入口は無い）。 */
export async function goChat(user: User) {
  await user.click(screen.getByRole('link', { name: 'DocuNavi' }));
}
