-- 資料の種別（資料群）。storage/pdf/<名前>/ のディレクトリ名（cafe / guides / zahard）を、整理用の資料群の名前として持つ。
-- 資料一覧のバッジと絞り込みに使う。表示名（店舗案内 など）は画面の側で持つ。DB には英数字の名前だけを置く。
-- 適用済み: MCP の apply_migration で、同じ内容。Supabase の履歴の版: 20261018175020（UTC 表記。JST では 2026-10-19 02:50）、名前 document_collection。
-- 検証: cafe 1 / guides 2 / zahard 3（null なし）。
--
-- 適用の順序: この SQL を実行する → 検証の SELECT で値を確かめる → コードを配る。
--   API が d.collection を SELECT するので、順序が逆（コードが先）だと、資料一覧が 503 になる。
-- 権限: 所有者は chatbot_app で、列ごとの権限（ACL）は無い。列を足すだけで API から読める。GRANT は要らない
--   （anon / authenticated へは付けない）。
-- 再実行しても安全（add column if not exists は、列があれば、制約も含めて何もしない。update は null の行だけ）。
-- null のまま残してよい（資料群が分からない資料。画面は種別のラベルを出さず、絞り込みの「すべて」にだけ出る）。
-- インデックスは作らない。資料は数件で、一覧 API が全件を返し、絞り込みは画面の側で行う。
--   サーバー側で絞る（数千件を超える）ようになったら、そのときに collection のインデックスを足す。

alter table chatbot.documents
  add column if not exists collection text
  constraint documents_collection_format check (collection is null or collection ~ '^[A-Za-z0-9_-]{1,40}$');

-- 既存の 6 資料の初期値（source = ファイル名。storage/pdf/<名前>/ の配置と同じ）
update chatbot.documents set collection = 'cafe'
  where source = 'cafe_store_guide.pdf' and collection is null;
update chatbot.documents set collection = 'guides'
  where source in ('aoba_aw820_manual.pdf', 'tsubasa_dorm_rules.pdf') and collection is null;
update chatbot.documents set collection = 'zahard'
  where source in ('ザーハード1.pdf', 'ザーハード2.pdf', 'ザーハード3.pdf') and collection is null;

-- 検証（期待: cafe 1 / guides 2 / zahard 3、null 0）
--   select collection, count(*) from chatbot.documents group by 1 order by 1;

-- 元に戻す手順（先にコードを前の版へ戻す。API が d.collection を読まなくなってから）
--   alter table chatbot.documents drop column collection;   -- 制約 documents_collection_format も一緒に消える
