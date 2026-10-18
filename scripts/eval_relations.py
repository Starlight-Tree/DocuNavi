"""入寮規程の関係抽出（app/relations）を、手作りの正解（evals/relations/tsubasa_gold.yaml）に対して測る。

API も DB も使わない（PDF を読むだけ）。合格ラインは正解ファイルの pass_line に、測定前に固定してある。

    python -m scripts.eval_relations            # 結果を表示する
    python -m scripts.eval_relations --write    # evals/relations/result.json にも書く
"""

import argparse
import json
from pathlib import Path

import yaml

from app.ingestion.pdf_parser import extract_pages
from app.relations.articles import Regulation, parse_regulation
from app.relations.extract import Edge, extract_edges

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "storage/pdf/guides/tsubasa_dorm_rules.pdf"
GOLD = ROOT / "evals/relations/tsubasa_gold.yaml"
TERMS = ROOT / "evals/relations/tsubasa_terms.yaml"
RESULT = ROOT / "evals/relations/result.json"

Key = tuple[str, str, str]


def _f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if p + r else 0.0


def check_structure(reg: Regulation, gold: dict) -> dict:
    expected = gold["expected_structure"]
    ranges: dict[str, list[int]] = {}
    for c in reg.chapters:
        numbers = [a.number for a in reg.articles if a.chapter == c.number]
        if numbers:
            ranges[f"ch{c.number}"] = [min(numbers), max(numbers)]
    got_chapters = ranges
    got_paragraphs = {a.number: len(a.paragraphs) for a in reg.articles}
    ok = got_chapters == expected["chapters"] and got_paragraphs == expected["paragraphs"]
    return {
        "ok": ok,
        "articles": len(reg.articles),
        "paragraphs": sum(got_paragraphs.values()),
        "chapters": len(reg.chapters),
        "got_chapters": got_chapters,
        "diff_paragraphs": {
            k: [expected["paragraphs"].get(k), got_paragraphs.get(k)]
            for k in sorted(set(expected["paragraphs"]) | set(got_paragraphs))
            if expected["paragraphs"].get(k) != got_paragraphs.get(k)
        },
    }


def evidence_ok(reg: Regulation, edges: list[Edge]) -> list[Edge]:
    """根拠本文が出典で確かめられない辺（空であるべき）。same_chapter は章の見出しで確かめる。"""
    texts = {p.id: p.raw for a in reg.articles for p in a.paragraphs} | {f.id: f.text for f in reg.footnotes}
    headings = {c.heading for c in reg.chapters}
    bad = []
    for e in edges:
        if e.type == "same_chapter":
            good = e.evidence in headings
        else:
            good = bool(e.evidence) and e.src in texts and e.evidence in texts[e.src]
        if not good:
            bad.append(e)
    return bad


def _gold(gold: dict, kind: str) -> set[Key]:
    return {(g["type"], g["src"], g["dst"]) for g in gold["edges"] if g["kind"] == kind}


def evaluate(reg: Regulation, edges: list[Edge], gold: dict) -> dict:
    chapters_gold = {
        (("same_chapter", f"a{n}", ch))
        for ch, (lo, hi) in gold["expected_structure"]["chapters"].items()
        for n in range(lo, hi + 1)
    }
    pred_chapter = {(e.type, e.src, e.dst) for e in edges if e.type == "same_chapter"}
    pred_term = {(e.type, e.src, e.dst) for e in edges if e.type == "uses_term"}
    pred: set[Key] = {(e.type, e.src, e.dst) for e in edges if e.type not in {"same_chapter", "uses_term"}}
    explicit, term, other = _gold(gold, "explicit"), _gold(gold, "implicit_term"), _gold(gold, "implicit_other")
    non_term_gold = explicit | other
    everything = non_term_gold | term

    precision = len(pred & non_term_gold) / len(pred) if pred else 0.0
    recall_explicit = len(pred & explicit) / len(explicit)
    term_precision = len(pred_term & term) / len(pred_term) if pred_term else 0.0
    term_recall = len(pred_term & term) / len(term)
    recall_other = len(pred & other) / len(other)
    predicted_all = pred | pred_term
    precision_all = len(predicted_all & everything) / len(predicted_all) if predicted_all else 0.0
    recall_all = len(predicted_all & everything) / len(everything)
    pairs = {(s, d): t for t, s, d in everything}
    wrong_type = sorted((t, s, d, pairs[(s, d)]) for t, s, d in predicted_all - everything if (s, d) in pairs)
    bad_evidence = evidence_ok(reg, edges)

    structure = check_structure(reg, gold)
    verdict = {
        "split": structure["ok"],
        "explicit_recall >= 0.9": recall_explicit >= 0.9,
        "precision >= 0.9": precision >= 0.9,
        "term_recall >= 0.9": term_recall >= 0.9,
        "term_precision >= 0.9": term_precision >= 0.9,
        "wrong_type == 0": not wrong_type,
        "same_chapter exact": pred_chapter == chapters_gold,
        "evidence 100%": not bad_evidence,
    }
    return {
        "structure": structure,
        "counts": {
            "explicit": len(explicit),
            "implicit_term": len(term),
            "implicit_other": len(other),
            "predicted_non_structural": len(pred),
            "predicted_uses_term": len(pred_term),
            "predicted_same_chapter": len(pred_chapter),
        },
        "precision": precision,
        "recall_explicit": recall_explicit,
        "term_precision": term_precision,
        "term_recall": term_recall,
        "recall_implicit_other": recall_other,
        "precision_all": precision_all,
        "recall_all": recall_all,
        "f1_all": _f1(precision_all, recall_all),
        "missed_explicit": sorted(explicit - pred),
        "missed_term": sorted(term - pred_term),
        "missed_other": sorted(other - pred),
        "false_positives": sorted(pred - non_term_gold),
        "false_positive_terms": sorted(pred_term - term),
        "wrong_type": wrong_type,
        "same_chapter_missing": sorted(chapters_gold - pred_chapter),
        "same_chapter_extra": sorted(pred_chapter - chapters_gold),
        "bad_evidence": [(e.type, e.src, e.dst, e.evidence) for e in bad_evidence],
        "verdict": verdict,
        "passed": all(verdict.values()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help=f"{RESULT.relative_to(ROOT)} にも書く")
    args = ap.parse_args()

    gold = yaml.safe_load(GOLD.read_text(encoding="utf-8"))
    reg = parse_regulation(extract_pages(PDF))
    terms = yaml.safe_load(TERMS.read_text(encoding="utf-8"))
    edges = extract_edges(reg, terms=terms)
    result = evaluate(reg, edges, gold)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.write:
        RESULT.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
