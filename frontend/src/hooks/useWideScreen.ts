import { useEffect, useState } from 'react';

/** CSS の `@media (min-width: 1024px)`（PC 幅）と同じ境目。サイドバーを閉じたとき、PC は細いレール、それより狭いと完全に隠す。 */
const WIDE_QUERY = '(min-width: 1024px)';

/** PC 幅か。`matchMedia` が無い環境（単体テスト）では false。 */
export function useWideScreen(): boolean {
  const [wide, setWide] = useState(() => window.matchMedia?.(WIDE_QUERY).matches ?? false);
  useEffect(() => {
    const query = window.matchMedia?.(WIDE_QUERY);
    if (!query) return;
    const update = () => setWide(query.matches);
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  return wide;
}
