-- 初期の 6 資料に、対象（target）と資料種別（doc_type）を入れる（バックフィル）。手動で 1 回だけ実行する。
-- supabase/migrations/ には置かない: 空の DB（ローカル・CI）や資料の ID が違う環境では、前提が合わず失敗するため。
-- 前提: supabase/migrations/20261103150000_document_classification.sql（M1）が適用済み。
--
-- 対応表（利用者が 2026-11-03 に承認した初期分類。識別名は M1 の初期の語彙）:
--   id  source                    collection  target         doc_type
--   1   cafe_store_guide.pdf      cafe        minato-coffee  store-guide
--   2   aoba_aw820_manual.pdf     guides      aoba-aw820     manual
--   3   tsubasa_dorm_rules.pdf    guides      tsubasa-dorm   regulation
--   4   ザーハード1.pdf           zahard      zahard         setting
--   5   ザーハード2.pdf           zahard      zahard         setting
--   6   ザーハード3.pdf           zahard      zahard         setting
--
-- 1 つの DO ブロック（= 1 つのトランザクション）で、全部入るか、何も入らないかのどちらか。次のどれかなら例外で止める（成功扱いにしない）:
--   - 期待した id の資料が無い / source か collection が期待と違う（別の資料を誤って更新しない）
--   - 分類が片方だけ入っている、または期待と違う値が入っている（手動で付けた分類を上書きしない。部分的な分類も矛盾として止める）
--   - 更新後の確認で、期待した 6 件が期待どおりの値になっていない
-- 未分類（target も doc_type も NULL）の行だけを更新する。同じ値がすでに入っている行は、何もしない（再実行しても安全）。
-- documents.collection・created_at・updated_at は更新しない（updated_at の意味を変えない）。ほかの資料（7 件目以降）は触れない。
--
-- 事前の確認（読むだけ。期待: 6 行以上。上の表と id / source / collection が一致し、target / doc_type は全部 NULL）:
--   select id, source, collection, target, doc_type, status from chatbot.documents order by id;
-- 実行後の確認（期待: 上の表のとおりの 6 行）:
--   select id, source, collection, target, doc_type, updated_at from chatbot.documents where id between 1 and 6 order by id;
--   select count(*) from chatbot.documents where target is null or doc_type is null;   -- 7 件目以降の未分類の数（6 件だけなら 0）
-- 元に戻す（バックフィルが書いたのは NULL → 値だけなので、NULL に戻せば M1 の直後の状態になる）:
--   update chatbot.documents set target = null, doc_type = null where id between 1 and 6;

do $$
declare
  e record;
  d record;
  changed integer := 0;
  unchanged integer := 0;
  verified integer;
  others integer;
begin
  for e in
    select * from (values
      (1, 'cafe_store_guide.pdf', 'cafe', 'minato-coffee', 'store-guide'),
      (2, 'aoba_aw820_manual.pdf', 'guides', 'aoba-aw820', 'manual'),
      (3, 'tsubasa_dorm_rules.pdf', 'guides', 'tsubasa-dorm', 'regulation'),
      (4, 'ザーハード1.pdf', 'zahard', 'zahard', 'setting'),
      (5, 'ザーハード2.pdf', 'zahard', 'zahard', 'setting'),
      (6, 'ザーハード3.pdf', 'zahard', 'zahard', 'setting')
    ) as v (id, source, collection, target, doc_type)
    order by v.id
  loop
    select x.id, x.source, x.collection, x.target, x.doc_type into d
      from chatbot.documents x where x.id = e.id for update;
    if not found then
      raise exception 'バックフィルを中止: id=% の資料がありません（期待: %）', e.id, e.source;
    end if;
    if d.source is distinct from e.source or d.collection is distinct from e.collection then
      raise exception 'バックフィルを中止: id=% が期待と違います（source=% collection=%、期待は source=% collection=%）',
        e.id, d.source, d.collection, e.source, e.collection;
    end if;
    if d.target is null and d.doc_type is null then
      update chatbot.documents
        set target = e.target, doc_type = e.doc_type
        where id = e.id and target is null and doc_type is null;
      changed := changed + 1;
    elsif d.target is not distinct from e.target and d.doc_type is not distinct from e.doc_type then
      unchanged := unchanged + 1;
    else
      raise exception 'バックフィルを中止: id=% (%) にすでに別の分類があります（target=% doc_type=%、期待は target=% doc_type=%）。上書きしません',
        e.id, e.source, d.target, d.doc_type, e.target, e.doc_type;
    end if;
  end loop;

  select count(*) into verified
    from chatbot.documents x
    join (values
      (1, 'minato-coffee', 'store-guide'), (2, 'aoba-aw820', 'manual'), (3, 'tsubasa-dorm', 'regulation'),
      (4, 'zahard', 'setting'), (5, 'zahard', 'setting'), (6, 'zahard', 'setting')
    ) as v (id, target, doc_type) on v.id = x.id and v.target = x.target and v.doc_type = x.doc_type;
  if verified <> 6 or changed + unchanged <> 6 then
    raise exception 'バックフィルを中止: 更新後の確認が合いません（一致 %、更新 %、すでに同じ %。期待は 6 件）', verified, changed, unchanged;
  end if;

  select count(*) into others from chatbot.documents where id not between 1 and 6;
  raise notice 'バックフィル完了: 更新 % 件、すでに同じ値 % 件、対象外の資料 % 件（触れていません）', changed, unchanged, others;
end
$$;
