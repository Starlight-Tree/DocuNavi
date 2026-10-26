import { useEffect, useState } from 'react';
import { fetchHealth } from '../api/chat';

/** 起動時に /health を 1 回だけ呼び、バックエンドのモード（mock / gemini）を返す。失敗したら null。 */
export function useBackendMode(): string | null {
  const [mode, setMode] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchHealth(controller.signal)
      .then((health) => setMode(health.mode))
      .catch(() => {
        // バッジが出ないだけで、チャットには影響しない
      });
    return () => controller.abort();
  }, []);

  return mode;
}
