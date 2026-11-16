/** 表示名の長さの上限（文字数。コードポイント単位）。画面（frontend/src/lib/displayName.ts）と同じ値。 */
export const DISPLAY_NAME_MAX = 30;

/**
 * `/update-user` の本文の検査。Better Auth の更新 API は name の中身を検査しないので、ここで守る。
 * 通すのは `{ name: string }` だけ（image・追加項目の書き換えは拒否）。name は前後の空白を除いた 1〜30 文字で、制御文字（Cc）を含まないこと。
 * 問題なければ null、だめなら固定の理由コード（本文の値は返さない）。
 */
export function checkUpdateUserBody(body: unknown): 'invalid_body' | 'invalid_name' | null {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) return 'invalid_body';
  const keys = Object.keys(body);
  if (keys.length !== 1 || keys[0] !== 'name') return 'invalid_body';
  const name = (body as { name: unknown }).name;
  if (typeof name !== 'string') return 'invalid_name';
  // 保存される値そのものを検査する（前後に空白があるものは、画面が除いてから送る）
  if (name !== name.trim()) return 'invalid_name';
  const length = Array.from(name).length;
  if (length < 1 || length > DISPLAY_NAME_MAX || /\p{Cc}/u.test(name)) return 'invalid_name';
  return null;
}
