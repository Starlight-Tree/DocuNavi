/**
 * PDF を保存するときのファイル名の規則（画面の保存アイコンと、PDF の中継の Route Handler が同じものを使う）。
 * 題名（または元のファイル名）から、ファイル名に使えない文字（/ \ : * ? " < > | と制御文字）を「_」にし、
 * 前後の空白と末尾の `.pdf` を外し、100 文字までに切って `.pdf` を付ける。日本語はそのまま。空なら `document.pdf`。
 */
const MAX_BASE_LENGTH = 100;

export function pdfFileName(base: string): string {
  const safe = base
    .replace(/[\\/:*?"<>|\u0000-\u001f\u007f]/g, '_')
    .trim()
    .replace(/\.pdf$/i, '')
    .trim();
  // 切るときはコードポイント単位（サロゲートペアを割らない）
  const cut = Array.from(safe).slice(0, MAX_BASE_LENGTH).join('').trim();
  return `${cut || 'document'}.pdf`;
}

/** Content-Disposition の filename*（RFC 5987）に入れる値。encodeURIComponent が残す ' ( ) * も % にする。 */
export function encodeRfc5987(value: string): string {
  return encodeURIComponent(value).replace(/['()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);
}
