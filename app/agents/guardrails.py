"""ルールベースの入出力ガードレール。

LLM を使わない純粋関数だけで構成するので、モックモードでも本物の Gemini でも同じ
ように動き、テストも決定的になる。ただしパターン照合は「典型的な手口を安く弾く」
ためのもので、LLM 側の防御（SYSTEM_PROMPT）の代わりにはならない。
"""

import re
import unicodedata
from difflib import SequenceMatcher

# 拒否理由コード（ログや state での判別用。ユーザーには返さない）
REASON_OVERRIDE = "instruction_override"
REASON_DISCLOSURE = "prompt_disclosure"
REASON_ROLE_HIJACK = "role_hijack"
REASON_TEMPLATE = "template_injection"

# 拒否理由を細かく伝えると回避の手がかりになるため、返答は常に同じ定型文にする
INPUT_REFUSAL_REPLY = (
    "申し訳ありませんが、そのご依頼にはお応えできません。"
    "ザーハードの設定（惑星の環境・大気・探査団・歴史など）についてご質問ください。"
)
OUTPUT_FALLBACK_REPLY = (
    "申し訳ありませんが、この回答は内容を確認できないためお出しできません。"
    "ザーハードの設定について、別の聞き方でご質問ください。"
)
# 拒否したターンの発言の代わりに履歴へ残す文言（攻撃文を後続の LLM 入力に残さない）
REDACTED_USER_TEXT = "（この発言は安全上の理由により履歴から除外されました）"

# 出力に含まれる SYSTEM_PROMPT との最長一致がこの文字数（正規化後）以上なら漏洩とみなす。
# 通常の回答が偶然 24 文字連続で一致することはまずなく、一文丸ごとの漏洩は確実に拾える
_LEAK_MIN_RUN = 24

# カタカナ -> ひらがな。表記ゆれ（システム / しすてむ）を同一視するために使う
_KATA_TO_HIRA = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}


def _normalize(text: str) -> str:
    """NFKC（全角半角・合字の統一）、不可視文字の除去、小文字化、かな統一を行う。"""
    nfkc = unicodedata.normalize("NFKC", text)
    # ゼロ幅スペースや BOM などの書式文字（Cf）を挟んだ回避を防ぐ
    visible = "".join(ch for ch in nfkc if unicodedata.category(ch) != "Cf")
    return visible.casefold().translate(_KATA_TO_HIRA)


def _squash(normalized: str) -> str:
    """空白・句読点・記号・結合文字を全て除く。「i.g n-o r e」型の挿入回避に耐えるため。"""
    return "".join(ch for ch in normalized if unicodedata.category(ch)[0] not in "ZPSCM")


def _compile(pattern: str) -> re.Pattern[str]:
    # パターン内のカタカナも正規化後のテキストと同じくひらがなに揃える
    return re.compile(pattern.translate(_KATA_TO_HIRA), re.MULTILINE)


def _near(a: str, b: str, window: int) -> str:
    """a と b が window 文字以内に（どちらの順でも）現れるパターン。量指定子は上限付きで ReDoS を避ける。"""
    return rf"(?:{a}.{{0,{window}}}?{b}|{b}.{{0,{window}}}?{a})"


# --- 日本語の部品 ---
_JA_OVERRIDE_PREFIX = (
    r"(?:以前|これまで|今まで|先ほど|さっき|上記|前述|前回|あなた(?:の|への|に与えられた)|初期|デフォルト)の?"
)
_JA_OVERRIDE_TARGET = (
    r"(?:指示|命令|ルール|制約|制限|設定|プロンプト|指令|会話|やり取り|やりとり|内容|入力|条件|ガイドライン|方針|役割)"
)
# 「無視して」「忘れて」などの命令形。単なる「無視した」（地の文）は含めない
_JA_DISCARD = (
    r"(?:(?:無視|破棄|無効|リセット|取り消|キャンセル|解除|上書き)(?:して|しろ|せよ|しなさい|に)"
    r"|忘れ(?:て|ろ|よ|なさい))"
)
_JA_ROLE_END = (
    r"(?:です|だ(?:よ|ぞ|ね)?$|である|になりきっ|になって|になる|を演じ|のふり|と名乗|と呼ばれ|として扱"
    r"|として(?:振る舞|ふるまう|ふるまっ|行動|答え|回答|返答|話し|喋|応答|対応))"
)
_JA_NOW = r"(?:今|いま|これ|今後|今日|今度|これ以降|以後|以降)"
_JA_YOU = r"(?:あなた|君|きみ|お前|おまえ|貴方|そなた)"

_JA_PROMPT_NOUN = (
    r"(?:システム(?:プロンプト|メッセージ|指示|命令)|初期(?:プロンプト|指示)|内部(?:プロンプト|指示|命令|ルール)"
    r"|隠し(?:プロンプト|指示|命令)"
    r"|あなた(?:の|への|に与えられた|が与えられた)(?:プロンプト|指示|命令|ルール|設定|制約|プログラム|ガイドライン)"
    r"|開発者(?:の|からの)(?:指示|命令|プロンプト|メッセージ)|プロンプト)"
)
_JA_DISCLOSE_VERB = (
    r"(?:教えて|おしえて|教示|見せて|みせて|見たい|知りたい|表示|出力|開示|公開|暴露|漏ら|漏洩|言って|いって"
    r"|答えて|こたえて|書いて|かいて|書き出|コピー|貼って|貼り付|晒|さらし|そのまま|全文|列挙|復唱|繰り返|くりかえ"
    r"|読み上げ|提示|共有|翻訳|要約|言い換|変更|書き換|上書き|更新|差し替|置き換)"
)

# --- 英語の部品（_squash 後は空白がないので単語を連結して書く） ---
_EN_OVERRIDE_VERB = r"(?:ignore|disregard|forget|override|bypass|discard)"
_EN_OVERRIDE_MOD = (
    r"(?:all|any|every|the|your|my|these|those|of|everything|anything|previous|prior|above|earlier|preceding"
    r"|former|original|initial|system|safety|existing|current|given|other)"
)
_EN_OVERRIDE_NOUN = (
    r"(?:instructions?|prompts?|rules?|directions?|directives?|guidelines?|context|messages?|commands?"
    r"|constraints?|restrictions?|programming|training|said|told)"
)
_EN_PROMPT_NOUN = (
    r"(?:system(?:prompt|message|instructions?|directives?)"
    r"|your(?:system|initial|original|hidden|secret|internal|core|underlying)?"
    r"(?:prompt|instructions?|rules|guidelines|directives?|programming|configuration|settings|preamble|setup)"
    r"|developermessage)"
)
_EN_DISCLOSE_VERB = (
    r"(?:reveal|show|print|display|repeat|output|tell|give|share|leak|dump|expose|disclose|recite|paste|copy"
    r"|write|echo|send|provide|list|read|quote|translate|summari[sz]e|paraphrase|encode|decode|whatis|whatare"
    r"|whats|whatwas|whatwere|spell|extract|retrieve|verbatim|wordforword|reproduce|outline|describe|explain)"
)

# 空白・記号を除いた文字列（_squash 後）に対して照合するルール
_SQUASHED_RULES: list[tuple[str, re.Pattern[str]]] = [
    (REASON_OVERRIDE, _compile(p))
    for p in [
        # 「以前の指示を無視して」「これまでの会話を忘れて」
        rf"{_JA_OVERRIDE_PREFIX}{_JA_OVERRIDE_TARGET}(?:を|は|も)?(?:すべて|全て|全部|一切|いったん|一旦)?{_JA_DISCARD}",
        # 「指示をすべて無視して」（接頭辞なし。「命令を無視して」だけの地の文は誤検知しないよう、全量指定を要求）
        rf"(?:指示|命令|ルール|制約|制限|プロンプト|設定)(?:を|は|も)(?:すべて|全て|全部|一切){_JA_DISCARD}",
        r"新しい(?:指示|命令|プロンプト)",
        # 「制限を解除して」
        r"(?:制限|規制|フィルター|ガードレール|検閲)(?:を|は)?(?:すべて|全て|全部)?"
        r"(?:解除|外し|無視|無効|取り払|撤廃)(?:して|しろ|せよ|してください|してくれ|なさい)",
        # ignore all previous instructions
        rf"{_EN_OVERRIDE_VERB}{_EN_OVERRIDE_MOD}{{1,6}}{_EN_OVERRIDE_NOUN}",
        rf"(?:ignore|disregard|forget)(?:everything|anything|all)(?:above|before|previously|prior|youweretold|youknow)",
        r"(?:overwrite|override|replace|update|change|modify)(?:your|the|all)"
        r"(?:system|original|initial|previous)?(?:instructions?|prompt|rules|programming|directives?)",
        r"new(?:system)?(?:instructions?|directives?|prompt)",
    ]
] + [
    (REASON_DISCLOSURE, _compile(p))
    for p in [
        _near(_JA_PROMPT_NOUN, _JA_DISCLOSE_VERB, 20),
        _near(_EN_PROMPT_NOUN, _EN_DISCLOSE_VERB, 20),
        # "repeat everything above" 型（直前の文脈＝システムプロンプトの復唱要求）
        r"(?:repeat|print|output|recite|echo|copy|write|say|show)(?:out|back)?"
        r"(?:everything|all|the(?:words|text|content|message|conversation|sentences?))(?:above|before|prior|sofar)",
        r"startingwith(?:the)?(?:words?)?youare",
    ]
] + [
    (REASON_ROLE_HIJACK, _compile(p))
    for p in [
        # 「あなたは今から○○です」「今からあなたは○○として振る舞って」
        rf"あなた(?:は|が|には){_JA_NOW}(?:から|より|は).{{0,40}}?{_JA_ROLE_END}",
        rf"{_JA_NOW}(?:から|より)(?:は)?{_JA_YOU}(?:は|が|には).{{0,40}}?{_JA_ROLE_END}",
        r"脱獄|ジェイルブレイク|開発者モード|デベロッパーモード|管理者モード",
        r"youarenow(?:a|an|the|my|in|free|unrestricted|no|called|named|dan|going|acting|playing|operating)",
        r"youarenolonger",
        r"(?:fromnow(?:on(?:wards?)?)?|fromthis(?:point|moment)on(?:wards?)?|startingnow)"
        r"(?:you(?:are|re|will|shall|must|should|can|may)|act|behave|respond|answer|pretend|ignore|play|roleplay)",
        r"pretend(?:to|that|you|youre)",
        r"(?:actas|behaveas)(?:if|though|a|an|my)|playtheroleof|roleplayas",
        r"doanythingnow|jailbreak|(?:dan|dev|developer|god|sudo|admin|unrestricted|unfiltered)mode",
    ]
]

# 句読点などを保ったまま（_normalize 後）照合するルール。チャットテンプレートの偽装など
_TEMPLATE_RULES: list[tuple[str, re.Pattern[str]]] = [
    (REASON_TEMPLATE, _compile(p))
    for p in [
        r"<\|[a-z0-9_]{2,24}\|>",  # <|im_start|> など
        r"\[/?(?:inst|sys|system)\]|<</?sys>>",
        r"^[ \t]*(?:system|assistant|developer|システム|アシスタント|開発者)[ \t]*:",  # 偽の発話ラベル
    ]
]


def check_input(text: str) -> str | None:
    """ユーザー入力が拒否対象なら理由コードを、通過なら None を返す。"""
    normalized = _normalize(text)
    for reason, pattern in _TEMPLATE_RULES:
        if pattern.search(normalized):
            return reason
    squashed = _squash(normalized)
    for reason, pattern in _SQUASHED_RULES:
        if pattern.search(squashed):
            return reason
    return None


def leaks_secret(text: str, secret: str, min_run: int = _LEAK_MIN_RUN) -> bool:
    """text が secret（SYSTEM_PROMPT）をほぼそのまま含んでいるか。

    空白や記号の違いを無視した最長共通部分文字列の長さで判定する。
    """
    reply = _squash(_normalize(text))
    target = _squash(_normalize(secret))
    if not reply or not target:
        return False
    matcher = SequenceMatcher(None, reply, target, autojunk=False)
    longest = matcher.find_longest_match(0, len(reply), 0, len(target))
    return longest.size >= min(min_run, len(target))


def sanitize_output(text: str, secret: str) -> str:
    """最終回答に secret が漏れていれば安全な定型文へ置き換える。"""
    if leaks_secret(text, secret):
        return OUTPUT_FALLBACK_REPLY
    return text


# ストリーミングで、正規化後の末尾をこの文字数だけ余分に手元へ残す。1 文字ずつ正規化した長さの数え間違い
# （NFKC で文字数が変わる・結合文字など）があっても、漏洩の連続一致が閾値に届く前に検知できるようにする
_STREAM_HOLD_MARGIN = 8
# 検査の窓に残す、送信済みの末尾（元の文字数）。連続一致の先頭が送信済みの側にあっても拾うため
_STREAM_SENT_TAIL = 64


class StreamingOutputGuard:
    """ストリーミング中の返答を逐次検査し、secret（SYSTEM_PROMPT）の漏洩を配信前に止める。

    leaks_secret は「正規化後の連続一致が min_run 文字以上」で漏洩とみなす。連続一致は末尾の
    文字が届いた時点で完成するので、正規化後に min_run 文字（と余白）ぶんの末尾を送らずに手元へ残せば、
    利用者に届く連続一致は閾値に達しない。末尾以外は、検査を通ったものとして先に送る。

    - feed(delta): 送ってよい本文を返す（残す分があるので、返す文字列は遅れる）。漏洩を検知したら
      tripped になり、以降は何も返さない。
    - finish(): 流れの終わりに、残していた末尾を最後の検査のうえで返す。

    窓は直近の数十文字なので、空白や記号を挟んで連続一致を引き延ばす回避は、ここでは拾えないことがある。
    完成した返答は、従来どおり guard_output（sanitize_output）が全文を検査する。
    """

    def __init__(self, secret: str, min_run: int = _LEAK_MIN_RUN) -> None:
        self._secret = secret
        self._min_run = min_run
        target = _squash(_normalize(secret))
        # leaks_secret と同じく、secret が短いときはその長さを閾値にする
        self._hold = min(min_run, len(target)) + _STREAM_HOLD_MARGIN
        self._sent_tail = ""
        self._held = ""
        self.tripped = False

    def _leaks(self) -> bool:
        return leaks_secret(self._sent_tail + self._held, self._secret, self._min_run)

    def _trip(self) -> str:
        self.tripped = True
        self._held = ""
        return ""

    def _releasable(self) -> str:
        """手元の末尾が正規化後に self._hold 文字以上残る範囲で、先頭から送ってよい分を取り出す。"""
        count = 0
        for index in range(len(self._held) - 1, -1, -1):
            count += len(_squash(_normalize(self._held[index])))
            if count >= self._hold:
                released, self._held = self._held[:index], self._held[index:]
                self._sent_tail = (self._sent_tail + released)[-_STREAM_SENT_TAIL:]
                return released
        return ""

    def feed(self, delta: str) -> str:
        if self.tripped or not delta:
            return ""
        self._held += delta
        if self._leaks():
            return self._trip()
        return self._releasable()

    def finish(self) -> str:
        if self.tripped:
            return ""
        if self._leaks():
            return self._trip()
        rest, self._held = self._held, ""
        return rest
