"""テスト用の架空資料の生成（evals/synth/）: コマンドラインと、PDF 化・点検。外部に接続しない。

reportlab はアプリの依存に入れていない。入っていない環境では、PDF を実際に作るテストだけをスキップする。
"""

import json
from pathlib import Path

import pytest

from evals.synth import batch, gpu_worker
from evals.synth.cli import main
from evals.synth.render import BANNER_LINES, MARKER, NOTICE, RenderUnavailable, render_pdf
from evals.synth.verify import verify_output


def test_cli_batch_flow_with_the_fake_engine(tmp_path, capsys):
    out = str(tmp_path / "out")
    assert main(["plan", "--out-dir", out]) == 0
    assert "計 74 件" in capsys.readouterr().out

    sample = ["--sample-per-target", "2", "--out-dir", out]
    assert main(["prepare", *sample, "--max-requests", "8", "--usd-per-hour", "3", "--max-total-cost-usd", "50"]) == 0
    capsys.readouterr()
    requests, results, meta = (f(Path(out), 1) for f in (batch.requests_path, batch.results_path, batch.meta_path))
    assert (
        gpu_worker.main(
            [
                "--engine",
                "fake",
                "--requests",
                str(requests),
                "--out",
                str(results),
                "--meta",
                str(meta),
                "--max-requests",
                "8",
            ]
        )
        == 0
    )
    assert main(["ingest", *sample, "--round", "1", "--usd-per-hour", "3", "--job-seconds", "600"]) == 0
    assert "合格 8" in capsys.readouterr().out
    assert main(["status", "--out-dir", out]) == 0
    assert main(["report", "--out-dir", out, "--usd-per-hour", "3", "--gemini-in", "0.15", "--gemini-out", "1.25"]) == 0
    assert "合格 PDF あたり" in capsys.readouterr().out


def test_cli_bench_flow(tmp_path, capsys):
    out = str(tmp_path / "out")
    sample = ["--sample-per-target", "4", "--out-dir", out]
    assert main(["bench-prepare", *sample]) == 0
    for label, c in (("c04", "4"), ("c08", "8")):
        args = ["--engine", "fake", "--requests", str(Path(out) / "bench" / "requests.jsonl"), "--out", str(Path(out) / "bench" / f"results.{label}.jsonl"),
                "--meta", str(Path(out) / "bench" / f"results.{label}.meta.json"), "--max-num-seqs", c]  # fmt: skip
        assert gpu_worker.main(args) == 0
        assert main(["bench-ingest", *sample, "--label", label, "--usd-per-hour", "3", "--job-seconds", "600"]) == 0
    capsys.readouterr()
    assert main(["bench-report", "--out-dir", out]) == 0
    assert "推奨:" in capsys.readouterr().out


def test_cli_refuses_unsafe_out_dir_and_unbounded_paid_runs(tmp_path, capsys):
    repo = Path(__file__).resolve().parents[1]
    assert main(["plan", "--out-dir", str(repo / "storage" / "pdf" / "synthetic")]) == 2
    assert "本番の検索に混ざる" in capsys.readouterr().err
    # fake 以外のバックエンドは、件数・費用・トークンの上限を全部指定しないと始まらない
    assert (
        main(
            [
                "run",
                "--backend",
                "openai",
                "--base-url",
                "http://127.0.0.1:1",
                "--model",
                "m",
                "--out-dir",
                str(tmp_path),
            ]
        )
        == 2
    )


def test_cli_run_returns_3_when_a_limit_stops_it(tmp_path):
    assert main(["run", "--sample-per-target", "2", "--out-dir", str(tmp_path / "o"), "--max-docs", "3"]) == 3


def test_render_without_reportlab_fails_clearly(tmp_path):
    pytest.importorskip("pypdf")
    try:
        import reportlab  # noqa: F401
    except ImportError:
        with pytest.raises(RenderUnavailable):
            render_pdf(
                "# 題\n\n## 一\n\n本文です。",
                tmp_path / "x.pdf",
                font_path=Path("/nonexistent.ttf"),
                doc_id="x",
                title="題",
            )
    else:
        pytest.skip("reportlab が入っている（実物のテストが走る）")


def test_real_pdf_has_the_notice_on_every_page_and_in_the_metadata(tmp_path):
    pytest.importorskip("reportlab")
    pypdf = pytest.importorskip("pypdf")
    from evals.synth.render import DEFAULT_FONT

    if not DEFAULT_FONT.exists():
        pytest.skip("日本語フォントがない")
    paragraphs = "\n\n".join(
        f"洗濯機の運転が止まったときは、エラーコードを確かめてください。{i} 番目の手順です。" * 6 for i in range(40)
    )
    md = f"# 題名です\n\n## 一\n\n{paragraphs}\n\n## 二\n\n| 項目 | 値 |\n|---|---|\n| 容量 | 8kg |\n\n以上です。"
    path = tmp_path / "x.pdf"
    render_pdf(md, path, font_path=DEFAULT_FONT, doc_id="demo-01", title="題名です")
    reader = pypdf.PdfReader(str(path))
    assert len(reader.pages) >= 2
    assert all(NOTICE in (p.extract_text() or "") for p in reader.pages)
    assert MARKER in (reader.metadata.subject or "")
    assert BANNER_LINES[0] in reader.pages[0].extract_text()
    again = tmp_path / "y.pdf"
    render_pdf(md, again, font_path=DEFAULT_FONT, doc_id="demo-01", title="題名です")
    assert again.read_bytes() == path.read_bytes()  # 同じ入力から同じバイト列


def test_a_cover_adds_one_page_with_the_notice_the_title_and_the_image(tmp_path):
    pytest.importorskip("reportlab")
    pypdf = pytest.importorskip("pypdf")
    from PIL import Image

    from evals.synth.render import COVER_COLOPHON, DEFAULT_FONT

    if not DEFAULT_FONT.exists():
        pytest.skip("日本語フォントがない")
    md = "# 題名です\n\n## 一\n\n洗濯機の運転が止まったときは、エラーコードを確かめてください。\n\n以上です。"
    cover = tmp_path / "cover.jpg"
    Image.new("RGB", (1200, 600), (120, 60, 30)).save(cover)
    plain = tmp_path / "plain.pdf"
    covered = tmp_path / "covered.pdf"
    render_pdf(md, plain, font_path=DEFAULT_FONT, doc_id="demo-01", title="題名です（副題です）")
    render_pdf(md, covered, font_path=DEFAULT_FONT, doc_id="demo-01", title="題名です（副題です）", cover_image=cover)
    before, after = pypdf.PdfReader(str(plain)), pypdf.PdfReader(str(covered))
    assert len(after.pages) == len(before.pages) + 1
    assert all(NOTICE in (p.extract_text() or "") for p in after.pages)  # 表紙にも帯がある
    first = after.pages[0].extract_text()
    assert "題名です" in first and "副題です" in first and COVER_COLOPHON in first
    assert "1 / " not in first  # 表紙にページ番号は無い
    assert len(list(after.pages[0].images)) == 1
    assert "題名です" in after.pages[1].extract_text()  # 本文は 2 ページ目から始まる


def test_verify_flags_missing_notice_tampering_and_orphans(tmp_path):
    pytest.importorskip("pypdf")
    from evals.synth.ledger import Ledger
    from evals.synth.pipeline import sha256_bytes

    out = tmp_path / "out"
    (out / "md" / "t").mkdir(parents=True)
    (out / "pdf" / "t").mkdir(parents=True)
    md = out / "md" / "t" / "t-1.md"
    md.write_text("# 題\n", encoding="utf-8")
    pdf = out / "pdf" / "t" / "t-1.pdf"
    pdf.write_bytes(b"%PDF-1.4 not really")
    Ledger(out / "ledger.jsonl").append(
        "t-1",
        status="accepted",
        md="md/t/t-1.md",
        md_sha256=sha256_bytes(md.read_bytes()),
        pdf="pdf/t/t-1.pdf",
        pdf_sha256="0" * 64,
        target="t",
    )
    (out / "pdf" / "t" / "orphan.pdf").write_bytes(b"x")
    problems = verify_output(out)
    assert any("PDF のハッシュが台帳と違います" in p for p in problems)
    assert any("PDF を読めません" in p for p in problems)
    assert any("台帳にない PDF" in p for p in problems)
    json.dumps(problems)


def test_markdown_subset_parses_h3_bullets_and_bold():
    from evals.synth import markdown_subset

    md = "# 題\n\n### 小見出し\n\n1. 手順\n   続きの行\n*   項目\n    *   入れ子\n\n**強調**だけ\n\n| 項目 | 値 |\n|---|---|\n| **a** | 1 |"
    kinds = [k for k, _ in markdown_subset.parse(md)]
    assert kinds == ["h1", "h3", "p", "cont", "li", "li2", "p", "table"]
    plain = markdown_subset.plain_text(md)
    assert "**" not in plain and "強調だけ" in plain


def test_pdf_draws_markup_as_headings_bold_and_bullets(tmp_path):
    pytest.importorskip("reportlab")
    pypdf = pytest.importorskip("pypdf")
    from evals.synth.render import DEFAULT_BOLD_FONT, DEFAULT_FONT

    if not DEFAULT_FONT.exists():
        pytest.skip("日本語フォントがない")
    md = "# 題\n\n## 一\n\n### 小見出し\n\n*   **太字の項目**です。\n\n**単独の太字**\n\n本文です。"
    path = tmp_path / "m.pdf"
    render_pdf(md, path, font_path=DEFAULT_FONT, bold_font_path=DEFAULT_BOLD_FONT, doc_id="demo-02", title="題")
    text = pypdf.PdfReader(str(path)).pages[0].extract_text()
    assert "**" not in text and "###" not in text and "*" not in text
    assert "小見出し" in text and "太字の項目です" in text
    if DEFAULT_BOLD_FONT is not None:
        assert b"NotoSansJP-Bold" in path.read_bytes()
