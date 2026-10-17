/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API のベース URL。未設定・空なら同一オリジン（相対 URL）を使う。 */
  readonly VITE_API_BASE_URL?: string;
  /** Supabase のプロジェクト URL。VITE_SUPABASE_PUBLISHABLE_KEY とそろっているときだけ、Google ログインを使う。 */
  readonly VITE_SUPABASE_URL?: string;
  /** Supabase の publishable（anon）キー。公開してよい鍵。service_role は入れない。 */
  readonly VITE_SUPABASE_PUBLISHABLE_KEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
