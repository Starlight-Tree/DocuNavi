import { MAX_THREAD_ID_LENGTH, THREAD_ID_PATTERN } from '../api/chat';

export const THREAD_ID_STORAGE_KEY = 'zahard-chat:thread_id';

// sessionStorage は、プライベートモードやサイトデータのブロックで例外になりうる。
// どの操作も失敗を握りつぶし、保存できなくてもチャット自体は動かす。

export function loadThreadId(): string | null {
  try {
    const value = window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY);
    // 書き換えられた値をサーバーに送って 422 にならないよう、形式を確かめる
    if (value && value.length <= MAX_THREAD_ID_LENGTH && THREAD_ID_PATTERN.test(value)) {
      return value;
    }
  } catch {
    // 読めないときは新規会話として扱う
  }
  return null;
}

export const RETURN_TO_STORAGE_KEY = 'docunavi:return_to';

/** ログインで別のページへ移る前に、いまの画面の状態（整えたクエリ）を預ける。null なら預けた分を捨てる。 */
export function saveReturnTo(search: string | null): void {
  try {
    if (search) {
      window.sessionStorage.setItem(RETURN_TO_STORAGE_KEY, search);
    } else {
      window.sessionStorage.removeItem(RETURN_TO_STORAGE_KEY);
    }
  } catch {
    // 預けられなくても、ログインはできる（戻ったときに画面の位置が既定になるだけ）
  }
}

/** 預けた画面の状態を取り出して、預け場所を空にする。無い・読めないときは null。 */
export function takeReturnTo(): string | null {
  try {
    const value = window.sessionStorage.getItem(RETURN_TO_STORAGE_KEY);
    window.sessionStorage.removeItem(RETURN_TO_STORAGE_KEY);
    return value;
  } catch {
    return null;
  }
}

export function saveThreadId(threadId: string | null): void {
  try {
    if (threadId) {
      window.sessionStorage.setItem(THREAD_ID_STORAGE_KEY, threadId);
    } else {
      window.sessionStorage.removeItem(THREAD_ID_STORAGE_KEY);
    }
  } catch {
    // 保存できなくても、この画面の間は state で会話を続けられる
  }
}
