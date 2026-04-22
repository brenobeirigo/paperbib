from __future__ import annotations

import json
import re
from pathlib import Path

import fitz
import responses

from paperbib.fetch import PaperBibClient


def _write_pdf(path: Path, text: str, title: str = "") -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.set_metadata({"title": title})
    document.save(path)
    document.close()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@responses.activate
def test_save_doi_writes_bib_and_report_sidecar(tmp_path: Path) -> None:
    responses.get(
        "https://api.crossref.org/works/10.1234/example.2024.001",
        json={
            "message": {
                "title": ["Source Paper"],
                "reference": [
                    {"DOI": "10.1111/ref.2020.001"},
                    {"unstructured": "Jones, B. (2019). Storage transitions. Energy Journal."},
                ],
            }
        },
        status=200,
    )
    responses.get(
        "https://api.crossref.org/works/10.1111/ref.2020.001/transform/application/x-bibtex",
        body='@article{RefOne,title="Reference One",doi="10.1111/ref.2020.001",year="2020",author="Ref, Alice"}',
        status=200,
    )
    responses.get(
        re.compile(r"https://api\.crossref\.org/works\?.*query\.bibliographic=.*Storage.*"),
        json={
            "message": {
                "items": [
                    {
                        "DOI": "10.2222/ref.2019.002",
                        "score": 78.0,
                        "issued": {"date-parts": [[2019]]},
                    }
                ]
            }
        },
        status=200,
    )
    responses.get(
        "https://api.crossref.org/works/10.2222/ref.2019.002/transform/application/x-bibtex",
        body='@article{RefTwo,title="Storage transitions",doi="10.2222/ref.2019.002",year="2019",author="Jones, Bob"}',
        status=200,
    )

    result = PaperBibClient().save(
        "10.1234/example.2024.001",
        to=tmp_path,
        name="resolved-paper",
    )

    report_path = tmp_path / "resolved-paper.report.json"
    assert result.output_path == tmp_path / "resolved-paper.bib"
    assert result.report_path == report_path

    saved = result.output_path.read_text(encoding="utf-8")
    assert "@article{ref2020reference," in saved
    assert "@article{jones2019storage," in saved

    report = _read_json(report_path)
    assert report["command"] == "save"
    assert report["output_bib"] == str(result.output_path)
    assert report["counts"] == {
        "entry_count": 2,
        "included_count": 2,
        "skipped_count": 0,
        "candidate_count": 2,
        "backward_count": 0,
        "forward_count": 0,
    }
    assert report["included_entries"][0]["citekey"] == "ref2020reference"
    assert report["seed_resolutions"][0]["doi"] == "10.1234/example.2024.001"


@responses.activate
def test_save_excludes_heuristic_entries_by_default(tmp_path: Path) -> None:
    pdf_path = tmp_path / "local-paper.pdf"
    _write_pdf(
        pdf_path,
        "\n".join(
            [
                "Interesting Local Paper About Storage Planning",
                "Some intro text.",
                "References",
                "[1] Ref, Alice. (2020). Reference One. doi:10.1111/ref.2020.001",
                "[2] Heuristic, Helen. (2018). Unresolved Background Study. Regional Grid Notes.",
            ]
        ),
        title="Interesting Local Paper About Storage Planning",
    )
    responses.get(
        "https://api.crossref.org/works/10.1111/ref.2020.001/transform/application/x-bibtex",
        body="@article{RefOne,\n  title = {Reference One},\n  author = {Ref, Alice},\n  year = {2020},\n  doi = {10.1111/ref.2020.001},\n}",
        status=200,
    )
    responses.get(
        re.compile(r"https://api\.crossref\.org/works\?.*query\.bibliographic=.*Unresolved.*"),
        json={"message": {"items": []}},
        status=200,
    )
    responses.get(
        re.compile(r"https://api\.crossref\.org/works\?.*query\.title=.*Interesting.*"),
        json={"message": {"items": []}},
        status=200,
    )

    result = PaperBibClient().save(str(pdf_path), to=tmp_path, name="precision-default")

    saved = result.output_path.read_text(encoding="utf-8")
    report = _read_json(tmp_path / "precision-default.report.json")

    assert result.entry_count == 1
    assert len(result.included_entries) == 1
    assert len(result.skipped_entries) == 1
    assert "@article{ref2020reference," in saved
    assert "Unresolved Background Study" not in saved
    assert report["included_entries"][0]["resolution_method"] == "crossref-bibtex"
    assert report["skipped_entries"][0]["title"] == "Unresolved Background Study"
    assert (
        report["skipped_entries"][0]["reason"]
        == "heuristic-only entry excluded by default"
    )


@responses.activate
def test_save_include_heuristic_restores_fallback_entries(tmp_path: Path) -> None:
    pdf_path = tmp_path / "local-paper.pdf"
    _write_pdf(
        pdf_path,
        "\n".join(
            [
                "Interesting Local Paper About Storage Planning",
                "Some intro text.",
                "References",
                "[1] Ref, Alice. (2020). Reference One. doi:10.1111/ref.2020.001",
                "[2] Heuristic, Helen. (2018). Unresolved Background Study. Regional Grid Notes.",
            ]
        ),
        title="Interesting Local Paper About Storage Planning",
    )
    responses.get(
        "https://api.crossref.org/works/10.1111/ref.2020.001/transform/application/x-bibtex",
        body="@article{RefOne,\n  title = {Reference One},\n  author = {Ref, Alice},\n  year = {2020},\n  doi = {10.1111/ref.2020.001},\n}",
        status=200,
    )
    responses.get(
        re.compile(r"https://api\.crossref\.org/works\?.*query\.bibliographic=.*Unresolved.*"),
        json={"message": {"items": []}},
        status=200,
    )
    responses.get(
        re.compile(r"https://api\.crossref\.org/works\?.*query\.title=.*Interesting.*"),
        json={"message": {"items": []}},
        status=200,
    )

    result = PaperBibClient().save(
        str(pdf_path),
        to=tmp_path,
        name="precision-heuristic",
        include_heuristic=True,
    )

    saved = result.output_path.read_text(encoding="utf-8")
    report = _read_json(tmp_path / "precision-heuristic.report.json")

    assert result.entry_count == 2
    assert len(result.skipped_entries) == 0
    assert "@article{ref2020reference," in saved
    assert "@misc{heuristic2018unresolved," in saved
    assert report["counts"]["included_count"] == 2
    assert report["included_entries"][1]["resolution_method"] == "heuristic"
