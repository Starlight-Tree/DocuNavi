import type { Metadata, Viewport } from 'next';
import '@fontsource-variable/noto-sans-jp/wght.css';
import { Inter } from 'next/font/google';
import type { ReactNode } from 'react';
import { THEME_INIT_SCRIPT } from '../src/lib/theme';
import '../src/styles.css';
import '../src/shell.css';

// 可変フォント（1 ファイルで全ウェイト）。ビルド時に取得して自前の配信にする（実行時に Google へは接続しない）。
const inter = Inter({ subsets: ['latin'], variable: '--font-inter', display: 'swap' });

export const metadata: Metadata = {
  title: 'DocuNavi',
  description: '資料を聞く・探す',
  other: { 'color-scheme': 'light dark' },
};

export const viewport: Viewport = { width: 'device-width', initialScale: 1, viewportFit: 'cover' };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // data-theme は描画前のスクリプトが付ける（サーバーの出力とは違うので、警告を止める）
    <html lang="ja" className={inter.variable} suppressHydrationWarning>
      <head>
        <script>{THEME_INIT_SCRIPT}</script>
      </head>
      <body>{children}</body>
    </html>
  );
}
