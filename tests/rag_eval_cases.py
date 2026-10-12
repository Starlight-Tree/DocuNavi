"""RAG 検索の評価セット。

storage/chunk/ の 3 ファイルを精読して手で作った小さな評価セット（実装を見て合わせ込んでいない）。
件数が少ないため、指標は「大きな退行の検知」には使えるが、統計的な精度保証にはならない。

各ケースの意味:
  expected_files: 回答の根拠が書かれているファイル。空タプルは「該当なし（ヒットしないこと）」を表す。
                  hit@k は「上位 k 件の出典に expected_files のどれかが含まれる」で判定する。
  evidence:       検索結果の本文に含まれていてほしい根拠の部分文字列（どれか 1 つで可）。
                  ファイル単位の hit では測れない「答えの文が返ってきたか」を見る。
  kind:           難易度・種別のラベル（集計用）。
"""

from typing import NamedTuple

F1 = "ザーハード1.txt"
F2 = "ザーハード2.txt"
F3 = "ザーハード3.txt"


class RagCase(NamedTuple):
    query: str
    expected_files: tuple[str, ...]
    evidence: tuple[str, ...]
    kind: str


RELATED_CASES: tuple[RagCase, ...] = (
    # --- 資料の文言にほぼそのまま対応する質問 ---
    RagCase("ザーハードの大気の酸素濃度はどれくらい？", (F1,), ("地球の1/10以下",), "direct"),
    RagCase("遺跡の防衛システムについて教えて", (F2,), ("高度な防衛システム",), "direct"),
    RagCase("遺跡の構造はどうなっていますか", (F2,), ("多層的な構造",), "direct"),
    RagCase("エネルギー源が暴走するとどうなる？", (F3,), ("惑星を破壊する可能性",), "direct"),
    RagCase("ザーハードの最大の魅力は何ですか", (F3,), ("最大の魅力",), "direct"),
    RagCase("守護機構は侵入者に対してどう働く？", (F3,), ("侵入者に対して攻撃的",), "direct"),
    RagCase("ザーハリアン文明が崩壊した原因は？", (F2,), ("文明が崩壊", "滅亡と関係"), "direct"),
    # --- 短いキーワードのみ ---
    RagCase("硫化水素", (F1,), ("硫化水素や二酸化炭素",), "keyword"),
    RagCase("放射線", (F3,), ("放射線を放出",), "keyword"),
    RagCase("象形文字", (F1,), ("未解読の象形文字",), "keyword"),
    RagCase("磁場", (F3,), ("強力な磁場",), "keyword"),
    # --- 言い換え・資料にない語彙 ---
    RagCase("昼と夜の気温差について教えて", (F1,), ("日中と夜間の温度差",), "paraphrase"),
    RagCase("毒ガスは含まれていますか", (F1,), ("有害なガス", "硫化水素や二酸化炭素"), "paraphrase"),
    RagCase("砂漠を移動するには何が必要？", (F1,), ("高機動の車両",), "paraphrase"),
    RagCase("エネルギー源は自然にできたものなの？", (F3,), ("自然に発生したものではなく",), "paraphrase"),
    RagCase("エネルギーを制御できたらどうなる", (F3,), ("無限のエネルギー供給",), "paraphrase"),
    # --- 口語・助詞違い ---
    RagCase("ザーハリアンって何者？", (F2,), ("ザーハリアン文明",), "colloquial"),
    RagCase("探査団は何を装備して挑むの", (F1,), ("生命維持装置",), "colloquial"),
    RagCase("メタンガスって入ってる？", (F1,), ("メタンガス",), "colloquial"),
    RagCase("冒険者が集まるのはなぜですか", (F2,), ("冒険者たちが集まり",), "colloquial"),
    RagCase("古い文明の遺跡はどこに埋もれているの", (F1,), ("砂漠の中に埋もれている",), "colloquial"),
)

# ザーハードと無関係な質問。1 件もヒットしない（該当なし）ことを期待する。
UNRELATED_CASES: tuple[RagCase, ...] = (
    RagCase("今日の東京の天気を教えてください", (), (), "unrelated"),
    RagCase("おすすめのカレーのレシピは？", (), (), "unrelated"),
    RagCase("Pythonでリストをソートする方法", (), (), "unrelated"),
    RagCase("サッカーのワールドカップで優勝したのはどこ？", (), (), "unrelated"),
    RagCase("こんにちは、元気ですか？", (), (), "unrelated"),
    # 別の惑星の話。「惑星」「星の」など資料と共通する語が混ざる
    RagCase("木星の大きさはどのくらいですか", (), (), "unrelated"),
    # 資料にも出てくる「生命」を含むが、ザーハードの話ではない
    RagCase("火星には生命がいますか", (), (), "unrelated"),
    # 資料にも出てくる「地球」を含むが、ザーハードの話ではない
    RagCase("地球の人口は何人ですか", (), (), "unrelated"),
)

ALL_CASES: tuple[RagCase, ...] = RELATED_CASES + UNRELATED_CASES
