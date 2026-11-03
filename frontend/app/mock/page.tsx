import type { Metadata } from 'next';
import MockClientApp from './mock-client-app';

// `UI_MOCK=1` で起動・ビルドしたときだけ開く（next.config.ts が、それ以外では / へ戻す）
export const metadata: Metadata = { title: 'ドキュナビ（UI モック）', robots: { index: false, follow: false } };

export default function Page() {
  return <MockClientApp />;
}
