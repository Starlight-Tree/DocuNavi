import { useEffect, useState } from 'react';

function currentMatch(query: string): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia(query).matches;
}

/** CSS のメディアクエリが合っているか。変わったら追従する。`matchMedia` が無い環境（テストなど）は、合っていない扱い。 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => currentMatch(query));

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const list = window.matchMedia(query);
    const update = () => setMatches(list.matches);
    update();
    list.addEventListener('change', update);
    return () => list.removeEventListener('change', update);
  }, [query]);

  return matches;
}
