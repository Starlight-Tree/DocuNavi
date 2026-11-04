"""PDF に埋め込む Noto Sans JP の Regular / Bold（静的な TrueType）を、fonts/ に作る。

reportlab は CFF の OpenType（fonts-noto-cjk の .otf/.ttc）を埋め込めないので、Google Fonts の可変フォント
（TrueType 輪郭。SIL Open Font License 1.1）から、fontTools で wght=400 / 700 の静的な TTF を切り出す。
フォントは約 6MB ずつで、Git に入れない（`evals/synth/fonts/` は管理外）。外部へ接続するのは、取得の 1 回だけ。

    python3 -m venv /tmp/synth-env && /tmp/synth-env/bin/pip install fonttools
    /tmp/synth-env/bin/python -m evals.synth.make_fonts
"""

from __future__ import annotations

import hashlib
import tempfile
import urllib.request
from pathlib import Path

from evals.synth.render import FONT_DIR

BASE = "https://github.com/google/fonts/raw/main/ofl/notosansjp/"
VARIABLE = "NotoSansJP%5Bwght%5D.ttf"
LICENSE = "OFL.txt"
WEIGHTS = {"Regular": 400, "Bold": 700}


def _fetch(name: str, dest: Path) -> None:
    with urllib.request.urlopen(BASE + name, timeout=120) as r:  # noqa: S310 - 固定の https の URL
        dest.write_bytes(r.read())


def main() -> int:
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    FONT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        variable = Path(tmp) / "vf.ttf"
        _fetch(VARIABLE, variable)
        _fetch(LICENSE, FONT_DIR / "OFL.txt")
        for style, weight in WEIGHTS.items():
            font = instantiateVariableFont(TTFont(variable), {"wght": weight}, updateFontNames=False)
            ps = f"NotoSansJP-{style}"
            for rec in font["name"].names:  # 埋め込まれる字体名を、可変フォントの名前（Thin など）から直す
                if rec.nameID in (1, 16):
                    rec.string = "Noto Sans JP"
                elif rec.nameID in (2, 17):
                    rec.string = style
                elif rec.nameID == 4:
                    rec.string = f"Noto Sans JP {style}"
                elif rec.nameID == 6:
                    rec.string = ps
            out = FONT_DIR / f"{ps}.ttf"
            font.save(out)
            print(f"{out}  sha256={hashlib.sha256(out.read_bytes()).hexdigest()[:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
