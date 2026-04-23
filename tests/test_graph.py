from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import fitz
import responses
from typer.testing import CliRunner

from paperbib.cli import app
from paperbib.fetch import PaperBibClient

_RUNNER = CliRunner()


def _write_pdf(path: Path, text: str, title: str = "") -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.set_metadata({"title": title})
    document.save(path)
    document.close()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _openalex_work(
    work_id: str,
    *,
    doi: str | None,
    title: str,
    year: int,
    authors: list[str],
    referenced: list[str] | None = None,
    venue: str = "Journal of Tests",
    work_type: str = "article",
    raw_type: str = "journal-article",
) -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/{work_id}",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "title": title,
        "display_name": title,
        "publication_year": year,
        "authorships": [{"author": {"display_name": author}} for author in authors],
        "type": work_type,
        "primary_location": {
            "source": {"display_name": venue},
            "raw_type": raw_type,
        },
        "referenced_works": [
            f"https://openalex.org/{reference}"
            for reference in (referenced or [])
        ],
    }


@responses.activate
def test_graph_resolves_doi_url_pdf_and_text_seed_sources(tmp_path: Path) -> None:
    pdf_path = tmp_path / "seed-paper.pdf"
    text_path = tmp_path / "seed-paper.txt"
    explicit_bib_path = tmp_path / "seed-graph.bib"

    _write_pdf(
        pdf_path,
        "\n".join(
            [
                "PDF Seed Paper For Storage Planning",
                "doi:10.3000/pdf.2021.003",
                "Some intro text.",
            ]
        ),
        title="PDF Seed Paper For Storage Planning",
    )
    text_path.write_text(
        "\n".join(
            [
                "Text Seed Paper For Storage Planning Networks",
                "Notes.",
            ]
        ),
        encoding="utf-8",
    )

    responses.get(
        "https://example.org/paper",
        body="""
        <html>
          <head>
            <meta name="citation_title" content="URL Seed Paper For Storage Planning" />
            <meta name="citation_doi" content="10.2000/url.2022.002" />
          </head>
        </html>
        """,
        headers={"content-type": "text/html; charset=utf-8"},
        status=200,
    )
    responses.get(
        "https://api.crossref.org/works/10.2000/url.2022.002",
        json={"message": {"title": ["URL Seed Paper For Storage Planning"], "reference": []}},
        status=200,
    )
    responses.get(
        re.compile(r"https://api\.crossref\.org/works\?.*query\.title=.*Text.*"),
        json={
            "message": {
                "items": [
                    {
                        "DOI": "10.4000/text.2020.004",
                        "score": 97.0,
                        "title": ["Text Seed Paper For Storage Planning Networks"],
                    }
                ]
            }
        },
        status=200,
    )

    for doi, work_id, title, ref_id, ref_doi in [
        ("10.1000/doi.2024.001", "W10", "DOI Seed", "W11", "10.1111/ref.2020.001"),
        ("10.2000/url.2022.002", "W20", "URL Seed", "W21", "10.2222/ref.2021.002"),
        ("10.3000/pdf.2021.003", "W30", "PDF Seed", "W31", "10.3333/ref.2022.003"),
        ("10.4000/text.2020.004", "W40", "Text Seed", "W41", "10.4444/ref.2023.004"),
    ]:
        responses.get(
            f"https://api.openalex.org/works/https://doi.org/{doi}",
            json=_openalex_work(
                work_id,
                doi=doi,
                title=title,
                year=2024,
                authors=["Seed, Alice"],
                referenced=[ref_id],
            ),
            status=200,
        )
        responses.get(
            f"https://api.openalex.org/works/{ref_id}",
            json=_openalex_work(
                ref_id,
                doi=ref_doi,
                title=f"Reference {ref_id[-1]}",
                year=2020 + int(ref_id[-1]) - 1,
                authors=[f"Author, {ref_id[-1]}"],
            ),
            status=200,
        )
        responses.get(
            f"https://api.crossref.org/works/{ref_doi}/transform/application/x-bibtex",
            body=(
                f"@article{{Crossref{ref_id[-1]},"  # noqa: E501
                f"title=\"Reference {ref_id[-1]}\",doi=\"{ref_doi}\",year=\"{2020 + int(ref_id[-1]) - 1}\",author=\"Author, {ref_id[-1]}\"}}"
            ),
            status=200,
        )

    result = PaperBibClient().save_graph(
        [
            "10.1000/doi.2024.001",
            "https://example.org/paper",
            str(pdf_path),
            str(text_path),
        ],
        to=explicit_bib_path,
        forward_levels=0,
        max_backward=1,
    )

    report = _read_json(tmp_path / "seed-graph.report.json")
    strategies = {item["source"]: item["strategy"] for item in report["seed_resolutions"]}

    assert result.output_path == explicit_bib_path
    assert result.report_path == tmp_path / "seed-graph.report.json"
    assert result.entry_count == 4
    assert result.backward_count == 4
    assert result.forward_count == 0
    assert strategies["10.1000/doi.2024.001"] == "doi"
    assert strategies["https://example.org/paper"] == "url-meta-doi"
    assert strategies[str(pdf_path)] == "pdf"
    assert strategies[str(text_path)] == "text-title"
    assert report["counts"]["included_count"] == 4
    assert report["counts"]["skipped_count"] == 0


@responses.activate
def test_graph_dedupes_filters_and_orders_entries_deterministically(tmp_path: Path) -> None:
    seed_one = "10.1234/example.2024.001"
    seed_two = "10.5678/example.2024.010"

    responses.get(
        "https://api.openalex.org/works/https://doi.org/10.1234/example.2024.001",
        json=_openalex_work(
            "W1",
            doi=seed_one,
            title="Seed One",
            year=2024,
            authors=["Seed, Alice"],
            referenced=["W2", "W3", "W4"],
        ),
        status=200,
    )
    responses.get(
        "https://api.openalex.org/works/https://doi.org/10.5678/example.2024.010",
        json=_openalex_work(
            "W10",
            doi=seed_two,
            title="Seed Two",
            year=2024,
            authors=["Seed, Bob"],
            referenced=["W2", "W3"],
        ),
        status=200,
    )
    responses.get(
        "https://api.openalex.org/works/W2",
        json=_openalex_work(
            "W2",
            doi="10.1111/ref.2020.001",
            title="Reference One",
            year=2020,
            authors=["Ref, Alice"],
        ),
        status=200,
    )
    responses.get(
        "https://api.openalex.org/works/W3",
        json=_openalex_work(
            "W3",
            doi=None,
            title="OpenAlex Structured Systems",
            year=2019,
            authors=["Graph, Gina"],
            venue="Energy Journal",
        ),
        status=200,
    )
    responses.get(
        "https://api.openalex.org/works/W4",
        json=_openalex_work(
            "W4",
            doi="10.2222/book.2017.004",
            title="Archive Handbook",
            year=2017,
            authors=["Archive, Amy"],
            work_type="book",
            raw_type="book",
            venue="Archive Press",
        ),
        status=200,
    )
    responses.get(
        re.compile(r"https://api\.openalex\.org/works\?.*filter=cites(?::|%3A)W1.*page=1.*per-page=2"),
        json={
            "meta": {"count": 2, "page": 1, "per_page": 2},
            "results": [
                _openalex_work(
                    "W2",
                    doi="10.1111/ref.2020.001",
                    title="Reference One",
                    year=2020,
                    authors=["Ref, Alice"],
                ),
                _openalex_work(
                    "W5",
                    doi="10.3333/tool.2023.005",
                    title="Dataset Appendix",
                    year=2023,
                    authors=["Data, Dana"],
                    work_type="dataset",
                    raw_type="dataset",
                    venue="Data Archive",
                ),
            ],
        },
        status=200,
    )
    responses.get(
        re.compile(r"https://api\.openalex\.org/works\?.*filter=cites(?::|%3A)W10.*page=1.*per-page=2"),
        json={"meta": {"count": 0, "page": 1, "per_page": 2}, "results": []},
        status=200,
    )
    responses.get(
        "https://api.crossref.org/works/10.1111/ref.2020.001/transform/application/x-bibtex",
        body='@article{RefOne,title="Reference One",doi="10.1111/ref.2020.001",year="2020",author="Ref, Alice"}',
        status=200,
    )
    responses.get(
        "https://api.crossref.org/works/10.2222/book.2017.004/transform/application/x-bibtex",
        body='@book{ArchiveBook,title="Archive Handbook",doi="10.2222/book.2017.004",year="2017",author="Archive, Amy"}',
        status=200,
    )
    responses.get(
        "https://api.crossref.org/works/10.3333/tool.2023.005/transform/application/x-bibtex",
        body='@misc{DatasetAppendix,title="Dataset Appendix",doi="10.3333/tool.2023.005",year="2023",author="Data, Dana"}',
        status=200,
    )

    result = PaperBibClient().save_graph(
        [seed_one, seed_two],
        to=tmp_path,
        name="filtered-graph",
        year_from=2018,
        entry_types=["article"],
        max_forward=2,
    )

    saved = result.output_path.read_text(encoding="utf-8")
    report = _read_json(tmp_path / "filtered-graph.report.json")

    assert result.entry_count == 2
    assert [entry.title for entry in result.included_entries] == [
        "Reference One",
        "OpenAlex Structured Systems",
    ]
    assert result.included_entries[0].citekey == "ref2020reference"
    assert result.included_entries[0].connection_count == 3
    assert result.included_entries[0].relation_types == ["backward", "forward"]
    assert result.included_entries[0].seed_sources == [seed_one, seed_two]
    assert result.included_entries[1].resolution_method == "openalex-structured"
    assert "@article{ref2020reference," in saved
    assert "@article{graph2019openalex," in saved

    skipped = {entry["title"]: entry["reason"] for entry in report["skipped_entries"]}
    assert skipped["Archive Handbook"] == "filtered by year-from 2018"
    assert skipped["Dataset Appendix"] == "filtered by type (misc)"
    assert report["counts"]["included_count"] == 2
    assert report["counts"]["skipped_count"] == 2


@responses.activate
def test_cli_graph_supports_multi_level_backward_traversal(tmp_path: Path) -> None:
    seed = "10.7000/seed.2024.001"

    responses.get(
        "https://api.openalex.org/works/https://doi.org/10.7000/seed.2024.001",
        json=_openalex_work(
            "W100",
            doi=seed,
            title="Seed Paper",
            year=2024,
            authors=["Seed, Alice"],
            referenced=["W101"],
        ),
        status=200,
    )
    responses.get(
        "https://api.openalex.org/works/W101",
        json=_openalex_work(
            "W101",
            doi="10.7000/ref.2023.002",
            title="First Hop",
            year=2023,
            authors=["Hop, One"],
            referenced=["W102"],
        ),
        status=200,
    )
    responses.get(
        "https://api.openalex.org/works/W102",
        json=_openalex_work(
            "W102",
            doi="10.7000/ref.2022.003",
            title="Second Hop",
            year=2022,
            authors=["Hop, Two"],
            referenced=[],
        ),
        status=200,
    )
    responses.get(
        "https://api.crossref.org/works/10.7000/ref.2023.002/transform/application/x-bibtex",
        body='@article{FirstHop,title="First Hop",doi="10.7000/ref.2023.002",year="2023",author="Hop, One"}',
        status=200,
    )
    responses.get(
        "https://api.crossref.org/works/10.7000/ref.2022.003/transform/application/x-bibtex",
        body='@article{SecondHop,title="Second Hop",doi="10.7000/ref.2022.003",year="2022",author="Hop, Two"}',
        status=200,
    )

    cli_result = _RUNNER.invoke(
        app,
        [
            "graph",
            seed,
            "--to",
            str(tmp_path),
            "--name",
            "multi-backward",
            "--backward",
            "2",
            "--forward",
            "0",
            "--max-backward",
            "1",
            "--json",
        ],
    )

    payload = json.loads(cli_result.output)
    saved = (tmp_path / "multi-backward.bib").read_text(encoding="utf-8")
    report = _read_json(tmp_path / "multi-backward.report.json")

    assert cli_result.exit_code == 0
    assert payload["entry_count"] == 2
    assert "@article{hop2023first," in saved
    assert "@article{hop2022second," in saved
    assert report["counts"]["backward_count"] == 2
    assert report["counts"]["forward_count"] == 0


@responses.activate
def test_graph_supports_multi_level_forward_traversal(tmp_path: Path) -> None:
    seed = "10.8000/seed.2024.001"

    responses.get(
        "https://api.openalex.org/works/https://doi.org/10.8000/seed.2024.001",
        json=_openalex_work(
            "W200",
            doi=seed,
            title="Seed Paper",
            year=2024,
            authors=["Seed, Alice"],
            referenced=[],
        ),
        status=200,
    )
    responses.get(
        re.compile(r"https://api\.openalex\.org/works\?.*filter=cites(?::|%3A)W200.*page=1.*per-page=1"),
        json={
            "meta": {"count": 1, "page": 1, "per_page": 1},
            "results": [
                _openalex_work(
                    "W201",
                    doi="10.8000/citing.2023.002",
                    title="First Forward Hop",
                    year=2023,
                    authors=["Forward, Fran"],
                ),
            ],
        },
        status=200,
    )
    responses.get(
        re.compile(r"https://api\.openalex\.org/works\?.*filter=cites(?::|%3A)W201.*page=1.*per-page=1"),
        json={
            "meta": {"count": 1, "page": 1, "per_page": 1},
            "results": [
                _openalex_work(
                    "W202",
                    doi="10.8000/citing.2022.003",
                    title="Second Forward Hop",
                    year=2022,
                    authors=["Forward, Finn"],
                ),
            ],
        },
        status=200,
    )
    responses.get(
        "https://api.crossref.org/works/10.8000/citing.2023.002/transform/application/x-bibtex",
        body='@article{FirstForwardHop,title="First Forward Hop",doi="10.8000/citing.2023.002",year="2023",author="Forward, Fran"}',
        status=200,
    )
    responses.get(
        "https://api.crossref.org/works/10.8000/citing.2022.003/transform/application/x-bibtex",
        body='@article{SecondForwardHop,title="Second Forward Hop",doi="10.8000/citing.2022.003",year="2022",author="Forward, Finn"}',
        status=200,
    )

    result = PaperBibClient().save_graph(
        [seed],
        to=tmp_path,
        name="multi-forward",
        backward_levels=0,
        forward_levels=2,
        max_forward=1,
    )

    saved = result.output_path.read_text(encoding="utf-8")
    report = _read_json(tmp_path / "multi-forward.report.json")

    assert result.entry_count == 2
    assert result.backward_count == 0
    assert result.forward_count == 2
    assert "@article{forward2023first," in saved
    assert "@article{forward2022second," in saved
    assert report["counts"]["forward_count"] == 2


@responses.activate
def test_cli_json_matches_report_summary_when_no_entries_survive(tmp_path: Path) -> None:
    pdf_path = tmp_path / "only-heuristic.pdf"
    _write_pdf(
        pdf_path,
        "\n".join(
            [
                "Only Heuristic Local Paper For Agents",
                "Some intro text.",
                "References",
                "[1] Heuristic, Helen. (2018). Unresolved Background Study. Regional Grid Notes.",
            ]
        ),
        title="Only Heuristic Local Paper For Agents",
    )
    responses.get(
        re.compile(r"https://api\.crossref\.org/works\?.*query\.bibliographic=.*Unresolved.*"),
        json={"message": {"items": []}},
        status=200,
    )
    responses.get(
        re.compile(r"https://api\.crossref\.org/works\?.*query\.title=.*Only.*"),
        json={"message": {"items": []}},
        status=200,
    )

    command = [
        "save",
        str(pdf_path),
        "--to",
        str(tmp_path),
        "--name",
        "only-heuristic",
        "--json",
    ]
    cli_result = _RUNNER.invoke(app, command)

    payload = json.loads(cli_result.output)
    report_path = tmp_path / "only-heuristic.report.json"
    report = _read_json(report_path)

    assert cli_result.exit_code == 1
    assert payload["entry_count"] == 0
    assert payload["included_count"] == 0
    assert payload["skipped_count"] == 1
    assert payload["output_path"] is None
    assert payload["report_path"] == str(report_path)
    assert payload["warnings"] == report["warnings"]
    assert report["counts"]["entry_count"] == 0
    assert report["counts"]["skipped_count"] == 1
