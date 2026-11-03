import type { Metadata } from 'next';
import ClassificationClientApp from './classification-client-app';

// `UI_MOCK=1` で起動・ビルドしたときだけ開く（next.config.ts が、それ以外では / へ戻す）
export const metadata: Metadata = { title: 'ドキュナビ（対象・資料種別の UI モック）', robots: { index: false, follow: false } };

export default function Page() {
  return <ClassificationClientApp />;
}
