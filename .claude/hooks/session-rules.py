#!/usr/bin/env python3
"""SessionStart（resume / compact）: 重要ルールの固定文を文脈へ入れる。読み込み失敗でも作業は止めない。"""
import json

MAX_CHARS = 1200  # 超えたら注入しない（長文の事故防止。文書上の上限は 10,000 字）
RULES = """【再注入: 必ず守る】
1. 本番 DB・Cloud Run・Cloudflare 本番（Worker・D1）への書き込み、push、昇格は、操作ごとに承認を得てから。前の承認は次に及ばない。
2. 実 API(課金)は、質問文・回数・概算費用を示して承認を得てから。
3. 案内・手順は MCP → API → ターミナル → 画面の順に検討。自分でできることは先に自分でやる。画面手順は完全な URL 付き。
4. キー・トークン・接続文字列を、表示・貼り付け・保存しない。
5. 承認待ちの項目は、承認が出るまで始めない。見送られた項目は再提案しない。
6. フック・権限・CLAUDE.md の変更は、差分を示して承認を得る。止められたら回避せず報告。
7. 依頼の完了条件が終わりの線。満たしたら報告して止まる。
詳細は CLAUDE.md と記憶。"""


def main():
    text = RULES.strip()
    if len(text) > MAX_CHARS:
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}, ensure_ascii=False))


main()
