import { useRef } from 'react';
import { Modal } from '../components/Modal';
import { useMock } from './MockProvider';
import { docById } from './store';

/** 削除の確認。モックのデータだけを更新する。開いたときは、安全な「キャンセル」にフォーカスを置く。 */
export function DeleteDialog({ docId, onClose }: { docId: string; onClose: () => void }) {
  const { state, dispatch } = useMock();
  const doc = docById(state, docId);
  const cancelRef = useRef<HTMLButtonElement>(null);
  if (!doc) return null;
  return (
    <Modal title="資料を削除しますか？" initialFocus={cancelRef} closeOnBackdrop={false} onClose={onClose}>
      <div className="ws-modal-body">
        <p>
          「<strong>{doc.name}</strong>」を一覧から削除します。この資料は質問対象からも外れます。過去の回答の出典には、資料の名前だけが残ります。
        </p>
        <p className="ws-note">モック: 画面のデータだけを更新します。実データは削除されません。</p>
      </div>
      <footer className="ws-modal-foot">
        <button ref={cancelRef} type="button" className="btn btn-secondary" onClick={onClose}>
          キャンセル
        </button>
        <button type="button" className="btn ws-btn-danger" onClick={() => dispatch({ type: 'deleteDoc', docId, now: Date.now() })}>
          削除する
        </button>
      </footer>
    </Modal>
  );
}

/** 設定（必要最小限）。モックの説明と、初期状態に戻す操作だけ。 */
export function SettingsDialog({ onClose }: { onClose: () => void }) {
  const { state, dispatch } = useMock();
  return (
    <Modal title="設定" onClose={onClose}>
      <div className="ws-modal-body">
        <section>
          <h3 className="ws-section-title">このモックについて</h3>
          <p className="ws-note">
            サンプルデータで動く画面の試作です。回答は定型文で、検索・生成・保存はしません。資料の追加・削除・取り込みの状態は、この画面を閉じる（再読み込みする）と元に戻ります。
          </p>
        </section>
        <section>
          <h3 className="ws-section-title">サンプルデータ</h3>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => dispatch({ type: 'reset', now: Date.now(), view: state.view })}
          >
            初期状態に戻す
          </button>
        </section>
      </div>
      <footer className="ws-modal-foot">
        <button type="button" className="btn btn-primary" onClick={onClose}>
          閉じる
        </button>
      </footer>
    </Modal>
  );
}
