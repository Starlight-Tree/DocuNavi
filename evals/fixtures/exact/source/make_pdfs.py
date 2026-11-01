"""3 冊の Markdown から評価用の PDF を作る（評価用の PDF の生成。アプリの実行には使わない）。

組み方は corpora/guides/source/make_pdfs.py の 1 段組み（aoba_aw820_manual と同じ）で、関数もそれを使う。
reportlab と、埋め込める日本語の TrueType フォントが要る。アプリの依存（requirements*.txt）には入れていない:

    python3 -m pip install --target /tmp/pylib reportlab
    PYTHONPATH=/tmp/pylib python3 evals/fixtures/exact/source/make_pdfs.py \
        --font /usr/share/fonts/opentype/ipafont-gothic/ipag.ttf

出力はバイト単位で再現できる（作成日時を固定している）。質問・正解は直さない。
"""

import argparse
import sys
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "corpora" / "guides" / "source"))

from make_pdfs import FONT_NAME, build_single  # noqa: E402  guides の組み立てを使い回す

BOOKS = ("ap_air_purifier_spec", "rv_robot_vacuum_spec", "notememo_release_notes")


def main() -> None:
    parser = argparse.ArgumentParser(description="Markdown から評価用の PDF を作る")
    parser.add_argument("--font", type=Path, required=True, help="埋め込む日本語の TrueType フォント（.ttf）")
    parser.add_argument("--out-dir", type=Path, default=HERE.parent, help="PDF の出力先")
    args = parser.parse_args()
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(args.font)))

    for name in BOOKS:
        out = args.out_dir / f"{name}.pdf"
        build_single(HERE / f"{name}.md", out, header=None, size=11.5, leading=21, cell=10)
        print("->", out)


if __name__ == "__main__":
    main()
