"""2 冊の Markdown から評価用の PDF を作る（評価用の PDF の生成。アプリの実行には使わない）。

組み方の関数は、evals/corpora/guides の `make_pdfs.py`（`build_single`）を読み込んで使う（同じ依存・同じ形。reportlab と、埋め込める
日本語の TrueType フォントが要る。アプリの依存には入れていない）。一時的な仮想環境で実行する:

    python3 -m venv /tmp/pdfenv && /tmp/pdfenv/bin/pip install reportlab
    # フォント例: Debian の fonts-ipaexfont-gothic（IPA フォント。ipaexg.ttf）
    /tmp/pdfenv/bin/python evals/fixtures/menu/source/make_pdfs.py --font /path/to/ipaexg.ttf

2 冊とも 1 段（Word や Google Docs の書き出しで見かける形）:

| 資料 | 組み方 | 確かめたいこと |
|---|---|---|
| cafe_menu | 1 段。表が複数。脚注。FAQ | 価格表・アレルギー表の読み取り、表の集計・絞り込み |
| cafe_campaign | 1 段。**全ページに同じ見出しと、ページ番号**。表と段落が混ざる | 繰り返しの文が混ざらないか、条件・例外を含む文 |

Markdown は、見出し（# ##）・段落・表（| 区切り）・脚注（[^n] と [^n]: 本文）だけを扱う。
出力はバイト単位で再現できる（作成日時を固定している）。質問・正解は直さない（承認前は、作成者の出力のまま）。
"""

import argparse
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUIDES_BUILDER = HERE.parents[2] / "corpora" / "guides" / "source" / "make_pdfs.py"
CAMPAIGN_HEADER = "みなとコーヒー 期間限定キャンペーンのご案内"


def _load_builder():
    spec = importlib.util.spec_from_file_location("guides_make_pdfs", GUIDES_BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description="Markdown から評価用の PDF を作る")
    parser.add_argument("--font", type=Path, required=True, help="埋め込む日本語の TrueType フォント（.ttf）")
    parser.add_argument("--out-dir", type=Path, default=HERE.parent, help="PDF の出力先（既定は evals/fixtures/menu/）")
    args = parser.parse_args()

    builder = _load_builder()
    builder.pdfmetrics.registerFont(builder.TTFont(builder.FONT_NAME, str(args.font)))
    settings = {"cafe_menu": None, "cafe_campaign": CAMPAIGN_HEADER}
    for name, header in settings.items():
        out = args.out_dir / f"{name}.pdf"
        builder.build_single(HERE / f"{name}.md", out, header=header, size=11.5, leading=21, cell=10)
        print("->", out)


if __name__ == "__main__":
    main()
