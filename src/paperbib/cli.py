"""CLI for paperbib."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import __version__
from .fetch import BibResult, PaperBibClient, PaperBibError

app = typer.Typer(
    add_completion=False,
    help="Produce agent-friendly bibliography artifacts from paper sources and citation graphs.",
    no_args_is_help=True,
)


@app.command()
def save(
    source: str = typer.Argument(..., help="Local paper path, DOI, or URL."),
    to: Path | None = typer.Option(
        None,
        "--to",
        help="Destination directory or explicit .bib file path.",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Optional output filename stem when --to points to a directory.",
    ),
    report: bool = typer.Option(
        True,
        "--report/--no-report",
        help="Write a structured .report.json sidecar next to the .bib artifact.",
    ),
    include_heuristic: bool = typer.Option(
        False,
        "--include-heuristic/--no-include-heuristic",
        help="Include heuristic-only entries in the final .bib output.",
    ),
    mailto: str = typer.Option(
        "paperbib@example.com",
        "--mailto",
        help="Email sent to Crossref for polite-pool requests.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of plain text.",
    ),
) -> None:
    """Save the bibliography cited by the requested paper source."""
    client = PaperBibClient(mailto=mailto)
    try:
        result = client.save(
            source,
            to=to,
            name=name,
            report=report,
            include_heuristic=include_heuristic,
        )
    except PaperBibError as exc:
        _emit_error(exc, json_output=json_output)
        raise typer.Exit(code=1) from exc

    _emit_result(result, json_output=json_output)
    _exit_if_empty(result)


@app.command()
def graph(
    sources: list[str] = typer.Argument(
        None,
        help="One or more seed sources: DOI, local path, or URL.",
    ),
    from_file: Path | None = typer.Option(
        None,
        "--from-file",
        help="Optional text file with one seed source per line.",
    ),
    to: Path | None = typer.Option(
        None,
        "--to",
        help="Destination directory or explicit .bib file path.",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Optional output filename stem when --to points to a directory.",
    ),
    backward_levels: int = typer.Option(
        1,
        "--backward",
        min=0,
        help="Number of backward citation levels to traverse. Use 0 to disable backward expansion.",
    ),
    forward_levels: int = typer.Option(
        1,
        "--forward",
        min=0,
        help="Number of forward citation levels to traverse. Use 0 to disable forward expansion.",
    ),
    max_backward: int | None = typer.Option(
        None,
        "--max-backward",
        min=1,
        help="Optional cap per expanded paper at each backward level.",
    ),
    max_forward: int = typer.Option(
        100,
        "--max-forward",
        min=1,
        help="Cap per expanded paper at each forward level.",
    ),
    year_from: int | None = typer.Option(
        None,
        "--year-from",
        help="Keep only entries published in or after this year.",
    ),
    year_to: int | None = typer.Option(
        None,
        "--year-to",
        help="Keep only entries published in or before this year.",
    ),
    entry_types: list[str] = typer.Option(
        None,
        "--type",
        help="Repeatable BibTeX entry type filter applied after resolution.",
    ),
    report: bool = typer.Option(
        True,
        "--report/--no-report",
        help="Write a structured .report.json sidecar next to the .bib artifact.",
    ),
    include_heuristic: bool = typer.Option(
        False,
        "--include-heuristic/--no-include-heuristic",
        help="Include heuristic-only entries in the final .bib output.",
    ),
    mailto: str = typer.Option(
        "paperbib@example.com",
        "--mailto",
        help="Email sent to APIs for polite-pool requests.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of plain text.",
    ),
) -> None:
    """Save a bounded citation graph bibliography expanded from seed sources."""
    seed_sources = _collect_seed_sources(sources, from_file)
    if not seed_sources:
        typer.echo("Provide at least one seed source or --from-file input.", err=True)
        raise typer.Exit(code=1)

    client = PaperBibClient(mailto=mailto)
    try:
        result = client.save_graph(
            seed_sources,
            to=to,
            name=name,
            backward_levels=backward_levels,
            forward_levels=forward_levels,
            max_backward=max_backward,
            max_forward=max_forward,
            report=report,
            include_heuristic=include_heuristic,
            year_from=year_from,
            year_to=year_to,
            entry_types=entry_types,
        )
    except PaperBibError as exc:
        _emit_error(exc, json_output=json_output)
        raise typer.Exit(code=1) from exc

    _emit_result(result, json_output=json_output)
    _exit_if_empty(result)


@app.command()
def status() -> None:
    """Show the installed paperbib runtime."""
    typer.echo(f"paperbib {__version__}")
    typer.echo("Inputs: local PDF/text path, DOI, or HTTP(S) URL")
    typer.echo("Artifacts: .bib plus .report.json by default")
    typer.echo("Graph mode: bounded forward/backward OpenAlex expansion from resolved seed sources")
    typer.echo("Default policy: precision-first, heuristic entries excluded unless requested")


def _collect_seed_sources(sources: list[str], from_file: Path | None) -> list[str]:
    collected = [source.strip() for source in sources if source.strip()]
    if from_file is None:
        return collected

    file_sources = [
        line.strip()
        for line in from_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return [*collected, *file_sources]


def _emit_result(result: BibResult, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(result.summary_payload(), indent=2))
        return

    typer.echo(f"Saved: {result.output_path or 'n/a'}")
    typer.echo(f"Report: {result.report_path or 'disabled'}")
    typer.echo(f"DOI: {result.doi or 'n/a'}")
    typer.echo(f"Title: {result.title or 'n/a'}")
    typer.echo(f"Included: {len(result.included_entries)}")
    typer.echo(f"Skipped: {len(result.skipped_entries)}")
    if result.failed_sources:
        typer.echo(f"Failed: {', '.join(result.failed_sources)}")
    if result.warnings:
        typer.echo(f"Warnings: {'; '.join(result.warnings)}")
    typer.echo(f"Strategy: {result.strategy}")


def _emit_error(error: PaperBibError, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps({"error": str(error)}, indent=2))
        return
    typer.echo(str(error), err=True)


def _exit_if_empty(result: BibResult) -> None:
    if result.entry_count:
        return
    raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
