import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';
import { THEME_INIT_SCRIPT } from '../src/lib/theme';
import '../src/styles.css';
import '../src/shell.css';

export const metadata: Metadata = {
  title: 'DocuNavi',
  description: '資料を聞く・探す',
  other: { 'color-scheme': 'light dark' },
};

export const viewport: Viewport = { width: 'device-width', initialScale: 1, viewportFit: 'cover' };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // data-theme は描画前のスクリプトが付ける（サーバーの出力とは違うので、警告を止める）
    <html lang="ja" suppressHydrationWarning>
      <head>
        <script>{THEME_INIT_SCRIPT}</script>
      </head>
      <body>{children}</body>
    </html>
  );
}
