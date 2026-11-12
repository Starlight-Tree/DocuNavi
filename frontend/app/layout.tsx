import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';
import { THEME_INIT_SCRIPT } from '../src/lib/theme';
import '../src/styles.css';
import '../src/shell.css';

const ICON =
  "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='16' r='9' fill='%236d5efc'/%3E%3Cellipse cx='16' cy='16' rx='15' ry='5' fill='none' stroke='%23f2a65a' stroke-width='2' transform='rotate(-20 16 16)'/%3E%3C/svg%3E";

export const metadata: Metadata = {
  title: 'DocuNavi',
  description: '資料を聞く・探す',
  icons: { icon: ICON },
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
