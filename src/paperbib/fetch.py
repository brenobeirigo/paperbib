"""Resolve paper sources into precision-first bibliography artifacts."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import fitz
import requests

from . import __version__
from .graph import GraphWork, OpenAlexClient
from .refs import (
    EntryReport,
    RefExtractor,
    ResolvedEntry,
    format_bibtex_entry,
    make_stable_citekey,
    normalize_title_key,
    resolution_quality,
    update_bibtex_citekey,
)

_DOI_PATTERN = re.compile(
    r"\b(10\.\d{4,}(?:\.\d+)*/(?:[^\s\"<>{}|\\^`\[\]#%?;,])+)",
    re.IGNORECASE,
)
_CROSSREF_WORKS_URL = "https://api.crossref.org/works/{doi}"
_CROSSREF_SEARCH_URL = "https://api.crossref.org/works"
_REQUEST_TIMEOUT = 20
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".html", ".htm"}
_META_TITLE_KEYS = ("citation_title", "dc.title", "og:title", "twitter:title")
_META_DOI_KEYS = ("citation_doi", "dc.identifier", "dc.identifier.doi", "doi")
_META_PDF_KEYS = ("citation_pdf_url", "pdf_url")
_MIN_TITLE_SEARCH_SCORE = 40.0
_DEFAULT_RELATION_TYPE = "reference"
_INCLUDABLE_METHODS = {
    "crossref-bibtex",
    "crossref-json",
    "openalex-structured",
}


@dataclass
class SeedResolution:
    source: str
    strategy: str
    doi: str | None = None
    title: str = ""
    resolved: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "strategy": self.strategy,
            "doi": self.doi,
            "title": self.title,
            "resolved": self.resolved,
            "reason": self.reason,
        }


@dataclass
class BibResult:
    command: str
    source: str
    strategy: str
    bibtex: str
    title: str = ""
    doi: str | None = None
    output_path: Path | None = None
    report_path: Path | None = None
    default_basename: str = "paper"
    entry_count: int = 0
    citekey: str = ""
    sources: list[str] = field(default_factory=list)
    backward_count: int = 0
    forward_count: int = 0
    failed_sources: list[str] = field(default_factory=list)
    included_entries: list[EntryReport] = field(default_factory=list)
    skipped_entries: list[EntryReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seed_resolutions: list[SeedResolution] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: _utc_now())

    def summary_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "doi": self.doi,
            "title": self.title,
            "entry_count": self.entry_count,
            "included_count": len(self.included_entries),
            "skipped_count": len(self.skipped_entries),
            "strategy": self.strategy,
            "output_path": str(self.output_path) if self.output_path else None,
            "report_path": str(self.report_path) if self.report_path else None,
            "failed_sources": self.failed_sources,
            "warnings": self.warnings,
        }

    def report_payload(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "version": __version__,
            "created_at": self.created_at,
            "strategy": self.strategy,
            "sources": self.sources or [self.source],
            "seed_resolutions": [seed.to_dict() for seed in self.seed_resolutions],
            "output_bib": str(self.output_path) if self.output_path else None,
            "counts": {
                "entry_count": self.entry_count,
                "included_count": len(self.included_entries),
                "skipped_count": len(self.skipped_entries),
                "candidate_count": len(self.included_entries) + len(self.skipped_entries),
                "backward_count": self.backward_count,
                "forward_count": self.forward_count,
            },
            "included_entries": [entry.to_dict() for entry in self.included_entries],
            "skipped_entries": [entry.to_dict() for entry in self.skipped_entries],
            "failed_sources": self.failed_sources,
            "warnings": self.warnings,
        }


@dataclass
class SourceDocument:
    source: str
    strategy: str
    default_basename: str
    title: str = ""
    doi: str | None = None
    full_text: str = ""
    crossref_references: list[dict[str, Any]] | None = None


@dataclass
class _CandidateRecord:
    entry: ResolvedEntry
    relation_types: set[str] = field(default_factory=set)
    seed_sources: set[str] = field(default_factory=set)
    connection_keys: set[str] = field(default_factory=set)

    @property
    def connection_count(self) -> int:
        return len(self.connection_keys) or 1


class PaperBibError(RuntimeError):
    """Raised when paperbib cannot resolve the requested source."""


class _MetaCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: list[dict[str, str]] = []
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "meta":
            lowered = {
                key.lower(): value
                for key, value in attrs
                if key and value is not None
            }
            self.meta.append(lowered)
            return
        if tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        self._text_parts.append(data)

    @property
    def title(self) -> str:
        return _normalize_space("".join(self._title_parts))

    @property
    def text(self) -> str:
        return _normalize_space(" ".join(self._text_parts))


class PaperBibClient:
    """Resolve paper sources into bibliography artifacts for AI agents."""

    def __init__(
        self,
        mailto: str = "paperbib@example.com",
        session: requests.Session | None = None,
    ) -> None:
        self.mailto = mailto
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": f"paperbib/0.1 (mailto:{mailto})"})
        self._ref_extractor = RefExtractor(mailto=mailto, session=self._session)
        self._graph_client = OpenAlexClient(mailto=mailto, session=self._session)

    def resolve(
        self,
        source: str | Path,
        *,
        include_heuristic: bool = False,
    ) -> BibResult:
        source_text = str(source).strip()
        if not source_text:
            raise PaperBibError("Source is empty.")

        document = self._load_source_document(source_text)
        result = self._extract_bibliography(
            document,
            include_heuristic=include_heuristic,
            sources=[source_text],
        )
        result.seed_resolutions = [
            SeedResolution(
                source=source_text,
                strategy=document.strategy,
                doi=result.doi,
                title=result.title or document.title,
                resolved=True,
            )
        ]
        return result

    def save(
        self,
        source: str | Path,
        to: str | Path | None = None,
        name: str | None = None,
        *,
        report: bool = True,
        include_heuristic: bool = False,
    ) -> BibResult:
        result = self.resolve(source, include_heuristic=include_heuristic)
        source_path = Path(str(source))
        artifact_bib_path = self._determine_output_path(
            original_source=source_path if source_path.exists() else None,
            result=result,
            to=Path(to) if to is not None else None,
            name=name,
        )
        self._write_artifacts(result, artifact_bib_path, report=report)
        return result

    def resolve_graph(
        self,
        sources: list[str],
        *,
        backward_levels: int = 1,
        forward_levels: int = 1,
        max_backward: int | None = None,
        max_forward: int | None = 100,
        include_heuristic: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        entry_types: list[str] | None = None,
    ) -> BibResult:
        if backward_levels < 0 or forward_levels < 0:
            raise PaperBibError("Forward and backward levels must be zero or greater.")

        if backward_levels == 0 and forward_levels == 0:
            raise PaperBibError(
                "At least one of backward or forward citation expansion levels must be greater than zero."
            )

        normalized_sources = [str(source).strip() for source in sources if str(source).strip()]
        seed_resolutions: list[SeedResolution] = []
        failed_sources: list[str] = []
        doi_aliases: dict[str, set[str]] = {}
        candidate_dois: list[str] = []
        seen_candidate_dois: set[str] = set()

        for source in normalized_sources:
            seed_resolution = self._resolve_seed_source(source)
            seed_resolutions.append(seed_resolution)
            if not seed_resolution.resolved or not seed_resolution.doi:
                failed_sources.append(source)
                continue

            doi_key = seed_resolution.doi.lower()
            doi_aliases.setdefault(doi_key, set()).add(source)
            if doi_key in seen_candidate_dois:
                continue
            seen_candidate_dois.add(doi_key)
            candidate_dois.append(seed_resolution.doi)

        if not candidate_dois:
            raise PaperBibError("Provide at least one valid DOI seed.")

        seed_works: list[GraphWork] = []
        seed_keys: set[str] = set()

        for doi in candidate_dois:
            seed_work = self._graph_client.get_work_by_doi(doi)
            if not seed_work:
                for alias in sorted(doi_aliases.get(doi.lower(), {doi})):
                    failed_sources.append(alias)
                for seed_resolution in seed_resolutions:
                    if seed_resolution.doi and seed_resolution.doi.lower() == doi.lower():
                        seed_resolution.resolved = False
                        seed_resolution.reason = f"OpenAlex could not resolve DOI {doi}."
                continue

            seed_works.append(seed_work)
            seed_keys.add(seed_work.work_id.lower())
            if seed_work.doi:
                seed_keys.add(seed_work.doi.lower())
            for seed_resolution in seed_resolutions:
                if seed_resolution.doi and seed_resolution.doi.lower() == doi.lower():
                    if not seed_resolution.title:
                        seed_resolution.title = seed_work.title

        if not seed_works:
            raise PaperBibError("Could not resolve any seed DOI in the citation graph.")

        records: dict[str, _CandidateRecord] = {}
        by_doi: dict[str, str] = {}
        by_title_year: dict[str, str] = {}
        backward_keys: set[str] = set()
        forward_keys: set[str] = set()

        entry_cache: dict[str, ResolvedEntry | None] = {}
        neighbor_cache: dict[tuple[str, str, int | None], list[GraphWork]] = {}

        self._expand_graph_direction(
            seed_works=seed_works,
            seed_keys=seed_keys,
            doi_aliases=doi_aliases,
            direction="backward",
            levels=backward_levels,
            limit=max_backward,
            records=records,
            by_doi=by_doi,
            by_title_year=by_title_year,
            discovered_keys=backward_keys,
            entry_cache=entry_cache,
            neighbor_cache=neighbor_cache,
        )
        self._expand_graph_direction(
            seed_works=seed_works,
            seed_keys=seed_keys,
            doi_aliases=doi_aliases,
            direction="forward",
            levels=forward_levels,
            limit=max_forward,
            records=records,
            by_doi=by_doi,
            by_title_year=by_title_year,
            discovered_keys=forward_keys,
            entry_cache=entry_cache,
            neighbor_cache=neighbor_cache,
        )

        if not records:
            raise PaperBibError("Could not build bibliography entries from the citation graph.")

        title = seed_works[0].title if len(seed_works) == 1 else ""
        doi = seed_works[0].doi if len(seed_works) == 1 else None
        default_basename = _slugify(seed_works[0].title) if len(seed_works) == 1 else "citation-graph"
        if not default_basename:
            default_basename = "citation-graph"

        warnings: list[str] = []
        result = self._finalize_records(
            command="graph",
            source="; ".join(normalized_sources),
            sources=normalized_sources,
            strategy="openalex-citation-graph",
            title=title,
            doi=doi,
            default_basename=default_basename,
            records=records,
            include_heuristic=include_heuristic,
            warnings=warnings,
            seed_resolutions=seed_resolutions,
            failed_sources=_unique_preserve_order(failed_sources),
            backward_count=len(backward_keys),
            forward_count=len(forward_keys),
            year_from=year_from,
            year_to=year_to,
            entry_types=entry_types,
        )
        return result

    def save_graph(
        self,
        sources: list[str],
        to: str | Path | None = None,
        name: str | None = None,
        *,
        backward_levels: int = 1,
        forward_levels: int = 1,
        max_backward: int | None = None,
        max_forward: int | None = 100,
        report: bool = True,
        include_heuristic: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        entry_types: list[str] | None = None,
        ) -> BibResult:
        result = self.resolve_graph(
            sources,
            backward_levels=backward_levels,
            forward_levels=forward_levels,
            max_backward=max_backward,
            max_forward=max_forward,
            include_heuristic=include_heuristic,
            year_from=year_from,
            year_to=year_to,
            entry_types=entry_types,
        )
        artifact_bib_path = self._determine_output_path(
            original_source=None,
            result=result,
            to=Path(to) if to is not None else None,
            name=name,
        )
        self._write_artifacts(result, artifact_bib_path, report=report)
        return result

    def _expand_graph_direction(
        self,
        *,
        seed_works: list[GraphWork],
        seed_keys: set[str],
        doi_aliases: dict[str, set[str]],
        direction: str,
        levels: int,
        limit: int | None,
        records: dict[str, _CandidateRecord],
        by_doi: dict[str, str],
        by_title_year: dict[str, str],
        discovered_keys: set[str],
        entry_cache: dict[str, ResolvedEntry | None],
        neighbor_cache: dict[tuple[str, str, int | None], list[GraphWork]],
    ) -> None:
        if levels <= 0:
            return

        for seed_work in seed_works:
            seed_source_aliases = doi_aliases.get((seed_work.doi or "").lower(), {seed_work.work_id})
            frontier = [seed_work]
            seen_frontier_keys = {self._graph_work_key(seed_work)}

            for level in range(1, levels + 1):
                next_frontier: list[GraphWork] = []
                next_frontier_keys: set[str] = set()

                for frontier_work in frontier:
                    neighbors = self._get_graph_neighbors(
                        frontier_work,
                        direction=direction,
                        limit=limit,
                        neighbor_cache=neighbor_cache,
                    )
                    for neighbor in neighbors:
                        graph_key = self._graph_work_key(neighbor)
                        if graph_key in seed_keys:
                            continue

                        discovered_keys.add(graph_key)
                        entry = self._resolve_cached_graph_entry(neighbor, entry_cache)
                        if entry:
                            self._merge_candidate_record(
                                records,
                                by_doi,
                                by_title_year,
                                entry=entry,
                                relation_type=direction,
                                seed_sources=seed_source_aliases,
                                connection_key=self._graph_connection_key(
                                    seed_work=seed_work,
                                    frontier_work=frontier_work,
                                    neighbor=neighbor,
                                    direction=direction,
                                    level=level,
                                ),
                            )

                        if graph_key in seen_frontier_keys or graph_key in next_frontier_keys:
                            continue

                        next_frontier.append(neighbor)
                        next_frontier_keys.add(graph_key)

                if not next_frontier:
                    break

                frontier = next_frontier
                seen_frontier_keys.update(next_frontier_keys)

    def _get_graph_neighbors(
        self,
        work: GraphWork,
        *,
        direction: str,
        limit: int | None,
        neighbor_cache: dict[tuple[str, str, int | None], list[GraphWork]],
    ) -> list[GraphWork]:
        cache_key = (direction, work.work_id.lower(), limit)
        if cache_key in neighbor_cache:
            return neighbor_cache[cache_key]

        if direction == "backward":
            neighbors = self._graph_client.get_referenced_works(work, limit=limit)
        else:
            neighbors = self._graph_client.get_citing_works(work, limit=limit)

        neighbor_cache[cache_key] = neighbors
        return neighbors

    def _resolve_cached_graph_entry(
        self,
        work: GraphWork,
        entry_cache: dict[str, ResolvedEntry | None],
    ) -> ResolvedEntry | None:
        graph_key = self._graph_work_key(work)
        if graph_key not in entry_cache:
            entry_cache[graph_key] = self._ref_extractor.resolve_graph_work(work)
        return entry_cache[graph_key]

    @staticmethod
    def _graph_connection_key(
        *,
        seed_work: GraphWork,
        frontier_work: GraphWork,
        neighbor: GraphWork,
        direction: str,
        level: int,
    ) -> str:
        return (
            f"{seed_work.work_id.lower()}:{direction}:{level}:"
            f"{frontier_work.work_id.lower()}->{neighbor.work_id.lower()}"
        )

    def _extract_bibliography(
        self,
        document: SourceDocument,
        *,
        include_heuristic: bool,
        sources: list[str],
    ) -> BibResult:
        records: dict[str, _CandidateRecord] = {}
        by_doi: dict[str, str] = {}
        by_title_year: dict[str, str] = {}
        strategies: list[str] = []
        warnings: list[str] = []

        if document.full_text:
            text_result = self._ref_extractor.extract_from_text(document.full_text)
            if text_result.entries:
                strategies.append(f"{document.strategy}-references")
                self._add_reference_entries(
                    records,
                    by_doi,
                    by_title_year,
                    text_result.entries,
                    seed_source=document.source,
                )

        crossref_document = self._load_reference_document(document)
        if crossref_document and crossref_document.crossref_references:
            crossref_result = self._ref_extractor.extract_from_crossref_references(
                crossref_document.crossref_references
            )
            if crossref_result.entries:
                strategies.append(f"{document.strategy}-crossref-references")
                self._add_reference_entries(
                    records,
                    by_doi,
                    by_title_year,
                    crossref_result.entries,
                    seed_source=document.source,
                )
                if not document.title:
                    document.title = crossref_document.title
                if document.default_basename == "paper":
                    document.default_basename = crossref_document.default_basename

        if not records:
            raise PaperBibError(
                f"Could not extract cited bibliography from {document.source}. "
                "A readable reference section or Crossref reference list was not available."
            )

        strategy = "+".join(dict.fromkeys(strategies)) if strategies else document.strategy
        result = self._finalize_records(
            command="save",
            source=document.source,
            sources=sources,
            strategy=strategy,
            title=document.title,
            doi=document.doi,
            default_basename=document.default_basename,
            records=records,
            include_heuristic=include_heuristic,
            warnings=warnings,
            seed_resolutions=[],
            failed_sources=[],
        )
        return result

    def _load_reference_document(self, document: SourceDocument) -> SourceDocument | None:
        if document.crossref_references is not None:
            return document

        if document.strategy not in {"pdf", "text"} and document.doi:
            return self._load_doi_document(document.doi, document.source, f"{document.strategy}-doi")

        if document.title:
            found_doi = self._search_source_doi_by_title(document.title)
            if found_doi:
                document.doi = found_doi
                document.strategy = f"{document.strategy}-title"
                return self._load_doi_document(found_doi, document.source, document.strategy)
        return None

    def _add_reference_entries(
        self,
        records: dict[str, _CandidateRecord],
        by_doi: dict[str, str],
        by_title_year: dict[str, str],
        entries: list[ResolvedEntry],
        *,
        seed_source: str,
    ) -> None:
        for entry in entries:
            self._merge_candidate_record(
                records,
                by_doi,
                by_title_year,
                entry=entry,
                relation_type=_DEFAULT_RELATION_TYPE,
                seed_sources={seed_source},
                connection_key=f"{seed_source}:{_DEFAULT_RELATION_TYPE}",
            )

    def _finalize_records(
        self,
        *,
        command: str,
        source: str,
        sources: list[str],
        strategy: str,
        title: str,
        doi: str | None,
        default_basename: str,
        records: dict[str, _CandidateRecord],
        include_heuristic: bool,
        warnings: list[str],
        seed_resolutions: list[SeedResolution],
        failed_sources: list[str],
        backward_count: int = 0,
        forward_count: int = 0,
        year_from: int | None = None,
        year_to: int | None = None,
        entry_types: list[str] | None = None,
    ) -> BibResult:
        ordered_records = sorted(records.values(), key=self._record_sort_key)
        self._assign_unique_citekeys(ordered_records)

        normalized_entry_types = {entry_type.lower() for entry_type in entry_types or []}
        included_reports: list[EntryReport] = []
        skipped_reports: list[EntryReport] = []
        included_entries: list[ResolvedEntry] = []

        for record in ordered_records:
            included, reason = self._inclusion_decision(
                record.entry,
                include_heuristic=include_heuristic,
                year_from=year_from,
                year_to=year_to,
                entry_types=normalized_entry_types,
            )
            report = self._ref_extractor.build_report(
                record.entry,
                relation_types=record.relation_types,
                seed_sources=record.seed_sources,
                included=included,
                reason=reason,
                connection_count=record.connection_count,
            )
            if included:
                included_reports.append(report)
                included_entries.append(record.entry)
            else:
                skipped_reports.append(report)

        bibtex = "\n\n".join(entry.bibtex.strip() for entry in included_entries if entry.bibtex.strip())
        if records and not included_entries:
            warnings.append("No entries survived filtering and precision rules.")

        return BibResult(
            command=command,
            source=source,
            strategy=strategy,
            bibtex=bibtex,
            title=title,
            doi=doi,
            default_basename=default_basename,
            entry_count=len(included_entries),
            citekey=included_entries[0].citekey if included_entries else "",
            sources=sources,
            backward_count=backward_count,
            forward_count=forward_count,
            failed_sources=failed_sources,
            included_entries=included_reports,
            skipped_entries=skipped_reports,
            warnings=_unique_preserve_order(warnings),
            seed_resolutions=seed_resolutions,
        )

    def _merge_candidate_record(
        self,
        records: dict[str, _CandidateRecord],
        by_doi: dict[str, str],
        by_title_year: dict[str, str],
        *,
        entry: ResolvedEntry,
        relation_type: str,
        seed_sources: set[str],
        connection_key: str,
    ) -> None:
        doi_key = entry.doi.lower() if entry.doi else ""
        title_year_key = self._title_year_key(entry)

        record_id = ""
        if doi_key and doi_key in by_doi:
            record_id = by_doi[doi_key]
        elif title_year_key and title_year_key in by_title_year:
            candidate_record_id = by_title_year[title_year_key]
            existing = records[candidate_record_id]
            existing_doi = existing.entry.doi.lower() if existing.entry.doi else ""
            if not doi_key or not existing_doi or existing_doi == doi_key:
                record_id = candidate_record_id

        if not record_id:
            record_id = doi_key or title_year_key or entry.citekey
            records[record_id] = _CandidateRecord(entry=entry)
        else:
            existing = records[record_id]
            existing.entry = self._prefer_entry(existing.entry, entry)

        record = records[record_id]
        record.relation_types.add(relation_type)
        record.seed_sources.update(seed_sources)
        record.connection_keys.add(connection_key)

        current_doi_key = record.entry.doi.lower() if record.entry.doi else ""
        current_title_year_key = self._title_year_key(record.entry)
        if current_doi_key:
            by_doi[current_doi_key] = record_id
        if current_title_year_key:
            by_title_year[current_title_year_key] = record_id

    def _prefer_entry(self, current: ResolvedEntry, candidate: ResolvedEntry) -> ResolvedEntry:
        current_quality = resolution_quality(current.resolution_method)
        candidate_quality = resolution_quality(candidate.resolution_method)
        if candidate_quality != current_quality:
            return candidate if candidate_quality > current_quality else current

        current_completeness = self._entry_completeness(current)
        candidate_completeness = self._entry_completeness(candidate)
        if candidate_completeness != current_completeness:
            return candidate if candidate_completeness > current_completeness else current

        return candidate if len(candidate.bibtex) > len(current.bibtex) else current

    @staticmethod
    def _entry_completeness(entry: ResolvedEntry) -> tuple[int, int, int]:
        filled = sum(
            1
            for value in (entry.doi, entry.title, entry.authors, entry.year)
            if value
        )
        return (
            filled,
            len(entry.authors.split(" and ")) if entry.authors else 0,
            len(entry.bibtex),
        )

    @staticmethod
    def _record_sort_key(record: _CandidateRecord) -> tuple[int, int, int, str]:
        year = int(record.entry.year) if record.entry.year.isdigit() else -1
        return (
            -record.connection_count,
            -resolution_quality(record.entry.resolution_method),
            -year,
            record.entry.title.lower(),
        )

    def _assign_unique_citekeys(self, records: list[_CandidateRecord]) -> None:
        used: set[str] = set()
        for record in records:
            base = make_stable_citekey(
                record.entry.authors,
                record.entry.year,
                record.entry.title,
                record.entry.doi,
            )
            candidate_key = base
            suffix = 1
            while candidate_key in used:
                suffix += 1
                suffix_text = str(suffix)
                candidate_key = f"{base[: max(1, 40 - len(suffix_text))]}{suffix_text}"

            used.add(candidate_key)
            if record.entry.citekey != candidate_key:
                record.entry.citekey = candidate_key
                record.entry.bibtex = update_bibtex_citekey(record.entry.bibtex, candidate_key)
                record.entry.bibtex = format_bibtex_entry(
                    record.entry.bibtex,
                    citekey=candidate_key,
                    entry_type=record.entry.entry_type,
                )

    def _inclusion_decision(
        self,
        entry: ResolvedEntry,
        *,
        include_heuristic: bool,
        year_from: int | None,
        year_to: int | None,
        entry_types: set[str],
    ) -> tuple[bool, str]:
        if year_from is not None:
            if not entry.year.isdigit():
                return False, f"missing year for year-from filter ({year_from})"
            if int(entry.year) < year_from:
                return False, f"filtered by year-from {year_from}"

        if year_to is not None:
            if not entry.year.isdigit():
                return False, f"missing year for year-to filter ({year_to})"
            if int(entry.year) > year_to:
                return False, f"filtered by year-to {year_to}"

        if entry_types and entry.entry_type.lower() not in entry_types:
            return False, f"filtered by type ({entry.entry_type})"

        if entry.resolution_method not in _INCLUDABLE_METHODS and not include_heuristic:
            return False, "heuristic-only entry excluded by default"

        return True, "included"

    def _write_artifacts(self, result: BibResult, artifact_bib_path: Path, *, report: bool) -> None:
        artifact_bib_path.parent.mkdir(parents=True, exist_ok=True)
        if result.entry_count:
            artifact_bib_path.write_text(result.bibtex.rstrip() + "\n", encoding="utf-8")
            result.output_path = artifact_bib_path

        if report:
            report_path = artifact_bib_path.with_name(f"{artifact_bib_path.stem}.report.json")
            report_path.write_text(
                json.dumps(result.report_payload(), indent=2),
                encoding="utf-8",
            )
            result.report_path = report_path

    def _resolve_seed_source(self, source_text: str) -> SeedResolution:
        local_path = Path(source_text)
        if not local_path.exists():
            doi = self._extract_doi(source_text)
            if doi and not self._is_url(source_text):
                return SeedResolution(
                    source=source_text,
                    strategy="doi",
                    doi=doi,
                    resolved=True,
                )

        try:
            document = self._load_source_document(source_text)
        except PaperBibError as exc:
            return SeedResolution(
                source=source_text,
                strategy="unresolved",
                resolved=False,
                reason=str(exc),
            )

        if document.doi:
            return SeedResolution(
                source=source_text,
                strategy=document.strategy,
                doi=document.doi,
                title=document.title,
                resolved=True,
            )

        if document.title:
            found_doi = self._search_source_doi_by_title(document.title)
            if found_doi:
                return SeedResolution(
                    source=source_text,
                    strategy=f"{document.strategy}-title",
                    doi=found_doi,
                    title=document.title,
                    resolved=True,
                )
            return SeedResolution(
                source=source_text,
                strategy=document.strategy,
                title=document.title,
                resolved=False,
                reason="Could not resolve DOI from source title.",
            )

        return SeedResolution(
            source=source_text,
            strategy=document.strategy,
            resolved=False,
            reason="Could not resolve DOI from source.",
        )

    def _load_source_document(self, source_text: str) -> SourceDocument:
        local_path = Path(source_text)
        if local_path.exists():
            return self._load_local_document(local_path)

        doi = self._extract_doi(source_text)
        if doi:
            return self._load_doi_document(doi, source_text, "doi")

        if self._is_url(source_text):
            return self._load_url_document(source_text)

        raise PaperBibError(
            "Unsupported source. Provide a local paper path, DOI, or HTTP(S) URL."
        )

    def _load_local_document(self, path: Path) -> SourceDocument:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return self._load_pdf_document(path)
        if suffix in _TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="ignore")
            title = _guess_title(text)
            doi = self._extract_doi(text)
            return SourceDocument(
                source=str(path),
                strategy="text",
                default_basename=_slugify(path.stem) or "paper",
                title=title,
                doi=doi,
                full_text=text,
            )
        raise PaperBibError(f"Unsupported local file type: {path.suffix or '<none>'}")

    def _load_pdf_document(self, path: Path) -> SourceDocument:
        try:
            with fitz.open(path) as document:
                metadata_title = _normalize_space(document.metadata.get("title") or "")
                full_text = "\n".join(
                    document.load_page(index).get_text("text")
                    for index in range(document.page_count)
                )
        except RuntimeError as exc:
            raise PaperBibError(f"Could not read PDF: {path}") from exc

        doi = self._extract_doi(full_text)
        title = metadata_title if _looks_like_title(metadata_title) else _guess_title(full_text)
        return SourceDocument(
            source=str(path),
            strategy="pdf",
            default_basename=_slugify(path.stem) or "paper",
            title=title,
            doi=doi,
            full_text=full_text,
        )

    def _load_url_document(self, url: str) -> SourceDocument:
        try:
            response = self._session.get(url, timeout=_REQUEST_TIMEOUT, allow_redirects=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise PaperBibError(f"Could not fetch URL: {url}") from exc

        content_type = response.headers.get("content-type", "").lower()
        final_url = response.url
        if "application/pdf" in content_type or urlparse(final_url).path.lower().endswith(".pdf"):
            return self._load_pdf_bytes_document(response.content, final_url, "url-pdf")

        parser = _MetaCollector()
        parser.feed(response.text)

        pdf_url = _first_meta_content(parser.meta, _META_PDF_KEYS)
        if pdf_url:
            return self._load_url_document(urljoin(final_url, pdf_url))

        meta_doi = self._extract_doi(_first_meta_content(parser.meta, _META_DOI_KEYS) or "")
        if meta_doi:
            return self._load_doi_document(meta_doi, final_url, "url-meta-doi")

        body_doi = self._extract_doi(parser.text)
        if body_doi:
            return self._load_doi_document(body_doi, final_url, "url-body-doi")

        title = _first_meta_content(parser.meta, _META_TITLE_KEYS) or parser.title
        if _looks_like_title(title):
            found_doi = self._search_source_doi_by_title(title)
            if found_doi:
                return self._load_doi_document(found_doi, final_url, "url-title")
            return SourceDocument(
                source=final_url,
                strategy="url-html",
                default_basename=_slugify(title) or "paper",
                title=title,
                full_text=parser.text,
            )

        raise PaperBibError(f"No DOI or usable metadata found at {final_url}.")

    def _load_pdf_bytes_document(
        self,
        pdf_bytes: bytes,
        source_label: str,
        strategy: str,
    ) -> SourceDocument:
        try:
            with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
                metadata_title = _normalize_space(document.metadata.get("title") or "")
                full_text = "\n".join(
                    document.load_page(index).get_text("text")
                    for index in range(document.page_count)
                )
        except RuntimeError as exc:
            raise PaperBibError(f"Could not read PDF content from {source_label}") from exc

        title = metadata_title if _looks_like_title(metadata_title) else _guess_title(full_text)
        basename = _slugify(Path(unquote(urlparse(source_label).path)).stem) or "paper"
        return SourceDocument(
            source=source_label,
            strategy=strategy,
            default_basename=basename,
            title=title,
            doi=self._extract_doi(full_text),
            full_text=full_text,
        )

    def _load_doi_document(
        self,
        doi: str,
        source_label: str,
        strategy: str,
    ) -> SourceDocument:
        normalized_doi = doi.rstrip(".,;)").strip()
        message = self._fetch_crossref_message(normalized_doi)
        if message is None:
            raise PaperBibError(f"Crossref could not resolve DOI {normalized_doi}.")

        title_list = message.get("title", [])
        title = title_list[0] if title_list else ""
        return SourceDocument(
            source=source_label,
            strategy=strategy,
            default_basename=_slugify(title) or self._doi_to_key(normalized_doi),
            title=title,
            doi=normalized_doi,
            crossref_references=message.get("reference", []) or [],
        )

    def _fetch_crossref_message(self, doi: str) -> dict[str, Any] | None:
        url = _CROSSREF_WORKS_URL.format(doi=doi)
        try:
            response = self._session.get(url, timeout=_REQUEST_TIMEOUT)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None

        try:
            return response.json().get("message", {})
        except ValueError:
            return None

    def _search_source_doi_by_title(self, title: str) -> str | None:
        params = {
            "query.title": title,
            "rows": 5,
            "mailto": self.mailto,
        }
        try:
            response = self._session.get(
                _CROSSREF_SEARCH_URL,
                params=params,
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None

        try:
            items = response.json().get("message", {}).get("items", [])
        except ValueError:
            return None

        normalized_target = normalize_title_key(title)
        for item in items:
            candidate_title = item.get("title", [""])
            candidate = candidate_title[0] if candidate_title else ""
            score = float(item.get("score", 0))
            doi = item.get("DOI", "").strip()
            if not doi:
                continue
            if normalize_title_key(candidate) == normalized_target:
                return doi
            if score >= _MIN_TITLE_SEARCH_SCORE:
                return doi
        return None

    def _determine_output_path(
        self,
        original_source: Path | None,
        result: BibResult,
        to: Path | None,
        name: str | None,
    ) -> Path:
        filename = (
            f"{_slugify(name) or result.default_basename or self._doi_to_key(result.doi or '') or 'paper'}.bib"
        )
        if original_source is not None and to is None:
            return original_source.with_suffix(".bib")
        if to is None:
            return Path.cwd() / filename
        if to.suffix.lower() == ".bib":
            return to
        return to / filename

    @staticmethod
    def _graph_work_key(work: GraphWork) -> str:
        if work.doi:
            return work.doi.lower()
        return work.work_id.lower()

    @staticmethod
    def _title_year_key(entry: ResolvedEntry) -> str:
        if not entry.title or not entry.year:
            return ""
        normalized_title = normalize_title_key(entry.title)
        if not normalized_title:
            return ""
        return f"{normalized_title}:{entry.year}"

    @staticmethod
    def _extract_doi(text: str) -> str | None:
        match = _DOI_PATTERN.search(unquote(text or ""))
        if not match:
            return None
        return match.group(1).rstrip(".,;)")

    @staticmethod
    def _is_url(value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _doi_to_key(doi: str) -> str:
        key = re.sub(r"[^a-z0-9]", "", doi.lower().replace("10.", "doi"))
        return key[:40] or "paper"


def _first_meta_content(meta_tags: list[dict[str, str]], keys: tuple[str, ...]) -> str:
    for tag in meta_tags:
        name = (tag.get("name") or tag.get("property") or "").lower()
        if name in keys:
            return _normalize_space(tag.get("content", ""))
    return ""


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _slugify(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    return slug[:120] or ""


def _looks_like_title(value: str) -> bool:
    normalized = _normalize_space(value)
    if len(normalized) < 20 or len(normalized) > 240:
        return False
    if normalized.count(" ") < 3:
        return False
    lower = normalized.lower()
    if lower.startswith(("abstract", "keywords", "doi", "http://", "https://")):
        return False
    if lower in {"references", "bibliography"}:
        return False
    if sum(character.isalpha() for character in normalized) < 12:
        return False
    return True


def _guess_title(text: str) -> str:
    candidates: list[str] = []
    for raw_line in text.splitlines():
        line = _normalize_space(raw_line)
        if not _looks_like_title(line):
            continue
        candidates.append(line)
        if len(candidates) >= 8:
            break
    if not candidates:
        return ""
    return max(candidates[:3], key=lambda candidate: (len(candidate.split()), -len(candidate)))


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
