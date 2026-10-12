/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API のベース URL。未設定・空なら同一オリジン（相対 URL）を使う。 */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
