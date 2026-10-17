"""2 冊の Markdown から評価用の PDF を作る（評価用の PDF の生成。アプリの実行には使わない）。

組み方の関数は、evals/corpora/guides の `make_pdfs.py`（`build_single`）を読み込んで使う（同じ依存・同じ形。reportlab と、埋め込める
日本語の TrueType フォントが要る。アプリの依存には入れていない）。一時的な仮想環境で実行する:

    python3 -m venv /tmp/pdfenv && /tmp/pdfenv/bin/pip install reportlab
    # フォント例: Debian の fonts-ipaexfont-gothic（IPA フォント。ipaexg.ttf）
    /tmp/pdfenv/bin/python evals/fixtures/gym/source/make_pdfs.py --font /path/to/ipaexg.ttf

2 冊とも 1 段（Word や Google Docs の書き出しで見かける形）:

| 資料 | 組み方 | 確かめたいこと |
|---|---|---|
| gym_guide | 1 段。表と脚注 | 店舗一覧の表の読み取り、設備の絞り込み |
| gym_plans | 1 段。**全ページに同じ見出しと、ページ番号**。表と段落が混ざる | 繰り返しの文が混ざらないか、条件・例外を含む文 |

Markdown は、見出し（# ##）・段落・表（| 区切り）・脚注（[^n] と [^n]: 本文）だけを扱う。
出力はバイト単位で再現できる（作成日時を固定している）。質問・正解は直さない（承認前は、作成者の出力のまま）。
"""

import argparse
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUIDES_BUILDER = HERE.parents[2] / "corpora" / "guides" / "source" / "make_pdfs.py"
PLANS_HEADER = "ベイフィット 料金プランのご案内"


def _load_builder():
    spec = importlib.util.spec_from_file_location("guides_make_pdfs", GUIDES_BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description="Markdown から評価用の PDF を作る")
    parser.add_argument("--font", type=Path, required=True, help="埋め込む日本語の TrueType フォント（.ttf）")
    parser.add_argument("--out-dir", type=Path, default=HERE.parent, help="PDF の出力先（既定は evals/fixtures/gym/）")
    args = parser.parse_args()

    builder = _load_builder()
    builder.pdfmetrics.registerFont(builder.TTFont(builder.FONT_NAME, str(args.font)))
    # gym_guide は 7 列の店舗一覧があり、表の文字を小さくしないとセルが折り返して抽出が崩れる（質問を作る前に確かめて決めた）
    settings = {"gym_guide": (None, 7.5), "gym_plans": (PLANS_HEADER, 10)}
    for name, (header, cell) in settings.items():
        out = args.out_dir / f"{name}.pdf"
        builder.build_single(HERE / f"{name}.md", out, header=header, size=11.5, leading=21, cell=cell)
        print("->", out)


if __name__ == "__main__":
    main()
