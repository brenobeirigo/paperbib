"""Resolve cited references into normalized bibliography entries."""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from .graph import GraphWork

_DOI_PATTERN = re.compile(
    r"\b(10\.\d{4,}(?:\.\d+)*/(?:[^\s\"<>{}|\\^`\[\]#%?;,])+)",
    re.IGNORECASE,
)
_CROSSREF_BIB_URL = "https://api.crossref.org/works/{doi}/transform/application/x-bibtex"
_CROSSREF_WORKS_URL = "https://api.crossref.org/works/{doi}"
_CROSSREF_SEARCH_URL = "https://api.crossref.org/works"
_REQUEST_TIMEOUT = 20
_RETRY_DELAY = 0.25
_MIN_SEARCH_SCORE = 60.0
_REF_SECTION_RE = re.compile(
    r"^\s*(?:#+\s*\*{0,2}\s*)?(?:references?|bibliography|works cited|literature cited)\s*\*{0,2}\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_REFERENCE_START_RE = re.compile(
    r"^(?:\[?\d+\]?[\.\)]?\s+|[A-Z][^.;]{0,120}\b(?:19|20)\d{2}\b)"
)
_PREFERRED_FIELD_ORDER = [
    "author",
    "editor",
    "title",
    "booktitle",
    "journal",
    "year",
    "volume",
    "number",
    "pages",
    "publisher",
    "doi",
    "url",
]
_COMMON_TITLE_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "of",
    "on",
    "the",
    "to",
    "with",
}
_QUALITY_SCORES = {
    "crossref-bibtex": 4,
    "crossref-json": 3,
    "openalex-structured": 2,
    "heuristic": 1,
}


@dataclass
class EntryReport:
    citekey: str
    doi: str | None
    title: str
    authors: str
    year: str
    entry_type: str
    resolution_method: str
    relation_types: list[str] = field(default_factory=list)
    seed_sources: list[str] = field(default_factory=list)
    included: bool = False
    reason: str = ""
    connection_count: int = 0
    resolution_quality: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "citekey": self.citekey,
            "doi": self.doi,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "entry_type": self.entry_type,
            "resolution_method": self.resolution_method,
            "relation_types": self.relation_types,
            "seed_sources": self.seed_sources,
            "included": self.included,
            "reason": self.reason,
            "connection_count": self.connection_count,
            "resolution_quality": self.resolution_quality,
        }


@dataclass
class ResolvedEntry:
    citekey: str
    doi: str | None
    bibtex: str
    title: str = ""
    authors: str = ""
    year: str = ""
    entry_type: str = "misc"
    source: str = "crossref"
    resolution_method: str = "heuristic"


@dataclass
class RefExtractionResult:
    entries: list[ResolvedEntry] = field(default_factory=list)


class RefExtractor:
    """Resolve bibliography candidates into normalized BibTeX entries."""

    def __init__(
        self,
        mailto: str = "paperbib@example.com",
        session: requests.Session | None = None,
    ) -> None:
        self.mailto = mailto
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": f"paperbib/0.1 (mailto:{mailto})"})

    def extract_from_text(self, full_text: str) -> RefExtractionResult:
        entries: list[ResolvedEntry] = []
        seen_keys: set[str] = set()

        reference_candidates = self._split_reference_section(full_text)
        if reference_candidates:
            for candidate in reference_candidates:
                entry = self.resolve_reference_text(candidate, seen_keys)
                if entry:
                    entries.append(entry)
        else:
            for doi in self._find_dois(full_text):
                entry = self._fetch_crossref_bibtex(doi)
                if entry and entry.citekey not in seen_keys:
                    entries.append(entry)
                    seen_keys.add(entry.citekey)
                    time.sleep(_RETRY_DELAY)

        return RefExtractionResult(entries=entries)

    def extract_from_crossref_references(
        self,
        references: list[dict[str, Any]],
    ) -> RefExtractionResult:
        entries: list[ResolvedEntry] = []
        seen_keys: set[str] = set()

        for reference in references:
            entry = self.resolve_crossref_reference(reference, seen_keys)
            if entry:
                entries.append(entry)

        return RefExtractionResult(entries=entries)

    def resolve_crossref_reference(
        self,
        reference: dict[str, Any],
        seen_keys: set[str] | None = None,
    ) -> ResolvedEntry | None:
        resolved_seen_keys = seen_keys if seen_keys is not None else set()
        entry = self._resolve_crossref_reference(reference, resolved_seen_keys)
        return entry

    def resolve_reference_text(
        self,
        ref_text: str,
        seen_keys: set[str] | None = None,
    ) -> ResolvedEntry | None:
        resolved_seen_keys = seen_keys if seen_keys is not None else set()
        entry = self._resolve_reference_text(ref_text, resolved_seen_keys)
        return entry

    def resolve_graph_work(self, work: GraphWork) -> ResolvedEntry | None:
        if work.doi:
            entry = self._fetch_crossref_bibtex(work.doi)
            if entry:
                if not entry.title:
                    entry.title = _normalize_space(work.title)
                if not entry.authors:
                    entry.authors = _normalize_space(work.authors)
                if not entry.year:
                    entry.year = _normalize_space(work.year)
                if entry.entry_type == "misc":
                    entry.entry_type = self._graph_type_to_bibtex(work)
                    entry.bibtex = format_bibtex_entry(
                        entry.bibtex,
                        citekey=entry.citekey,
                        entry_type=entry.entry_type,
                    )
                return entry

        title = _normalize_space(work.title)
        if not title:
            return None

        entry_type = self._graph_type_to_bibtex(work)
        venue_field = _graph_venue_field(entry_type)
        extra_fields: dict[str, str] = {}
        if work.venue and venue_field:
            extra_fields[venue_field] = work.venue

        return self._build_structured_entry(
            title=title,
            authors=_normalize_space(work.authors),
            year=_normalize_space(work.year),
            doi=work.doi,
            entry_type=entry_type,
            source="openalex",
            resolution_method="openalex-structured",
            extra_fields=extra_fields,
        )

    def build_report(
        self,
        entry: ResolvedEntry,
        *,
        relation_types: set[str],
        seed_sources: set[str],
        included: bool,
        reason: str,
        connection_count: int,
    ) -> EntryReport:
        return EntryReport(
            citekey=entry.citekey,
            doi=entry.doi,
            title=entry.title,
            authors=entry.authors,
            year=entry.year,
            entry_type=entry.entry_type,
            resolution_method=entry.resolution_method,
            relation_types=sorted(relation_types),
            seed_sources=sorted(seed_sources),
            included=included,
            reason=reason,
            connection_count=connection_count,
            resolution_quality=resolution_quality(entry.resolution_method),
        )

    def _resolve_crossref_reference(
        self,
        reference: dict[str, Any],
        seen_keys: set[str],
    ) -> ResolvedEntry | None:
        doi = self._extract_doi(reference.get("DOI", "") or reference.get("doi", "") or "")
        if doi:
            entry = self._fetch_crossref_bibtex(doi)
            if entry and entry.citekey not in seen_keys:
                seen_keys.add(entry.citekey)
                time.sleep(_RETRY_DELAY)
                return entry

        ref_text = self._reference_dict_to_text(reference)
        expected_year = str(reference.get("year", "")).strip() or None
        if ref_text:
            entry = self._search_crossref_for_ref(ref_text, expected_year=expected_year)
            if entry and entry.citekey not in seen_keys:
                seen_keys.add(entry.citekey)
                time.sleep(_RETRY_DELAY)
                return entry

        heuristic = self._reference_dict_to_heuristic(reference, seen_keys)
        if heuristic:
            seen_keys.add(heuristic.citekey)
        return heuristic

    def _resolve_reference_text(
        self,
        ref_text: str,
        seen_keys: set[str],
    ) -> ResolvedEntry | None:
        clean = self._clean_reference_text(ref_text)
        if len(clean) < 20:
            return None

        doi = self._extract_doi(clean)
        if doi:
            entry = self._fetch_crossref_bibtex(doi)
            if entry and entry.citekey not in seen_keys:
                seen_keys.add(entry.citekey)
                time.sleep(_RETRY_DELAY)
                return entry

        year_match = re.search(r"\b((?:19|20)\d{2})\b", clean)
        expected_year = year_match.group(1) if year_match else None
        crossref_entry = self._search_crossref_for_ref(clean, expected_year=expected_year)
        if crossref_entry and crossref_entry.citekey not in seen_keys:
            seen_keys.add(crossref_entry.citekey)
            time.sleep(_RETRY_DELAY)
            return crossref_entry

        heuristic = self._parse_ref_line(clean, seen_keys)
        if heuristic:
            seen_keys.add(heuristic.citekey)
        return heuristic

    def _split_reference_section(self, text: str) -> list[str]:
        match = _REF_SECTION_RE.search(text)
        if not match:
            return []

        ref_text = text[match.end() :].replace("\r\n", "\n")
        candidates: list[str] = []
        buffer: list[str] = []

        for raw_line in ref_text.split("\n"):
            line = _normalize_space(raw_line)
            if not line:
                if buffer:
                    candidates.append(" ".join(buffer))
                    buffer = []
                continue

            if self._looks_like_page_noise(line):
                continue

            if self._is_reference_start(line) and buffer:
                candidates.append(" ".join(buffer))
                buffer = [line]
                continue

            if buffer:
                buffer.append(line)
            else:
                buffer = [line]

        if buffer:
            candidates.append(" ".join(buffer))

        return [candidate for candidate in candidates if len(candidate) >= 20]

    def _is_reference_start(self, line: str) -> bool:
        return bool(_REFERENCE_START_RE.match(line))

    def _looks_like_page_noise(self, line: str) -> bool:
        lowered = line.lower()
        if re.match(r"^\d+\s*$", line):
            return True
        if lowered.startswith("page ") and re.search(r"\d", line):
            return True
        return False

    def _find_dois(self, text: str) -> list[str]:
        seen: dict[str, None] = {}
        for match in _DOI_PATTERN.finditer(text):
            doi = match.group(1).rstrip(".,;)")
            seen[doi] = None
        return list(seen)

    def _fetch_crossref_bibtex(self, doi: str) -> ResolvedEntry | None:
        url = _CROSSREF_BIB_URL.format(doi=doi)
        try:
            response = self._session.get(url, timeout=_REQUEST_TIMEOUT)
        except requests.RequestException:
            return self._fetch_crossref_json(doi)

        if response.status_code != 200:
            return self._fetch_crossref_json(doi)

        bibtex = response.text.strip()
        parsed = _parse_bibtex_entry(bibtex)
        if not parsed:
            return self._fetch_crossref_json(doi)

        entry_type, _, fields = parsed
        field_map = {name.lower(): value for name, value in fields}
        title = field_map.get("title", "")
        authors = field_map.get("author", "")
        year = field_map.get("year", "")
        stable_key = make_stable_citekey(authors, year, title, doi)
        normalized_bibtex = format_bibtex_entry(
            bibtex,
            citekey=stable_key,
            entry_type=entry_type,
        )
        return ResolvedEntry(
            citekey=stable_key,
            doi=doi,
            bibtex=normalized_bibtex,
            title=title,
            authors=authors,
            year=year,
            entry_type=entry_type.lower(),
            source="crossref",
            resolution_method="crossref-bibtex",
        )

    def _fetch_crossref_json(self, doi: str) -> ResolvedEntry | None:
        url = _CROSSREF_WORKS_URL.format(doi=doi)
        try:
            response = self._session.get(url, timeout=_REQUEST_TIMEOUT)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None

        try:
            data: dict[str, Any] = response.json().get("message", {})
        except ValueError:
            return None

        title_list = data.get("title", [])
        title = title_list[0] if title_list else "Unknown Title"
        authors_raw = data.get("author", [])
        authors = " and ".join(
            f"{author.get('family', '')}, {author.get('given', '')}".strip(", ")
            for author in authors_raw
            if f"{author.get('family', '')}{author.get('given', '')}".strip()
        ) or "Unknown Author"
        year = ""
        issued = data.get("issued", {}).get("date-parts", [[]])
        if issued and issued[0]:
            year = str(issued[0][0])
        entry_type = self._crossref_type_to_bibtex(data.get("type", "journal-article"))

        journal = data.get("container-title", [""])[0] or data.get("publisher", "")
        extra_fields: dict[str, str] = {}
        if journal:
            journal_field = "journal" if entry_type == "article" else _graph_venue_field(entry_type)
            if journal_field:
                extra_fields[journal_field] = journal
        if data.get("volume"):
            extra_fields["volume"] = str(data["volume"])
        if data.get("issue"):
            extra_fields["number"] = str(data["issue"])
        if data.get("page"):
            extra_fields["pages"] = str(data["page"])
        if data.get("publisher") and entry_type in {"book", "techreport"}:
            extra_fields["publisher"] = str(data["publisher"])

        return self._build_structured_entry(
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            entry_type=entry_type,
            source="crossref",
            resolution_method="crossref-json",
            extra_fields=extra_fields,
        )

    def _search_crossref_for_ref(
        self,
        ref_text: str,
        expected_year: str | None = None,
    ) -> ResolvedEntry | None:
        params: dict[str, Any] = {
            "query.bibliographic": ref_text,
            "rows": 1,
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
        if not items:
            return None

        best = items[0]
        score = float(best.get("score", 0))
        if score < _MIN_SEARCH_SCORE:
            return None

        if expected_year:
            issued = best.get("issued", {}).get("date-parts", [[]])
            if issued and issued[0]:
                result_year = str(issued[0][0])
                if result_year != expected_year:
                    return None

        doi = best.get("DOI", "").strip()
        if not doi:
            return None

        return self._fetch_crossref_bibtex(doi)

    def _reference_dict_to_text(self, reference: dict[str, Any]) -> str:
        if reference.get("unstructured"):
            return _normalize_space(str(reference["unstructured"]))

        parts: list[str] = []
        for key in (
            "author",
            "year",
            "article-title",
            "volume-title",
            "journal-title",
            "volume",
            "first-page",
        ):
            value = reference.get(key)
            if value:
                parts.append(str(value))
        return _normalize_space(". ".join(parts))

    def _reference_dict_to_heuristic(
        self,
        reference: dict[str, Any],
        exclude_keys: set[str],
    ) -> ResolvedEntry | None:
        authors = _normalize_space(str(reference.get("author", "")))
        year = _normalize_space(str(reference.get("year", "")))
        title = _normalize_space(
            str(
                reference.get("article-title")
                or reference.get("volume-title")
                or reference.get("journal-title")
                or ""
            )
        )
        if len(title) < 6:
            return None

        heuristic = self._build_structured_entry(
            title=title,
            authors=authors,
            year=year,
            doi=None,
            entry_type="misc",
            source="heuristic",
            resolution_method="heuristic",
        )
        if heuristic is None or heuristic.citekey in exclude_keys:
            return None
        return heuristic

    def _clean_reference_text(self, line: str) -> str:
        clean = re.sub(r"^\[?\d+\]?[\.\)]?\s*", "", line)
        clean = re.sub(r"^[-*•]\s+", "", clean)
        return _normalize_space(clean)

    def _parse_ref_line(self, line: str, exclude_keys: set[str]) -> ResolvedEntry | None:
        if len(line) < 20:
            return None

        year_match = re.search(r"\b((?:19|20)\d{2})\b", line)
        year = year_match.group(1) if year_match else ""
        author_chunk = line[: year_match.start()].strip(" .,;") if year_match else line[:40]
        authors = _normalize_space(author_chunk)

        title = ""
        if year_match:
            after_year = line[year_match.end() :].lstrip(")]}:.,; ")
            quoted_match = re.match(r'["\u201c]([^"\u201d]{10,})["\u201d]', after_year)
            if quoted_match:
                title = quoted_match.group(1).strip()
            else:
                title_match = re.match(r"([^.]+)", after_year)
                title = title_match.group(1).strip() if title_match else after_year[:120]

        if not title:
            return None

        heuristic = self._build_structured_entry(
            title=title,
            authors=authors,
            year=year,
            doi=None,
            entry_type="misc",
            source="heuristic",
            resolution_method="heuristic",
        )
        if heuristic is None or heuristic.citekey in exclude_keys:
            return None
        return heuristic

    def _build_structured_entry(
        self,
        *,
        title: str,
        authors: str,
        year: str,
        doi: str | None,
        entry_type: str,
        source: str,
        resolution_method: str,
        extra_fields: dict[str, str] | None = None,
    ) -> ResolvedEntry | None:
        normalized_title = _normalize_space(title)
        if not normalized_title:
            return None

        normalized_authors = _normalize_space(authors)
        normalized_year = _normalize_space(year)
        normalized_type = entry_type.lower() if entry_type else "misc"
        stable_key = make_stable_citekey(
            normalized_authors,
            normalized_year,
            normalized_title,
            doi,
        )

        fields = [("title", normalized_title)]
        if normalized_authors:
            fields.insert(0, ("author", normalized_authors))
        if normalized_year:
            fields.append(("year", normalized_year))
        if extra_fields:
            for field_name, field_value in extra_fields.items():
                normalized_value = _normalize_space(field_value)
                if normalized_value:
                    fields.append((field_name.lower(), normalized_value))
        if doi:
            fields.append(("doi", doi))

        lines = [f"@{normalized_type}{{{stable_key},"]
        for index, (field_name, field_value) in enumerate(fields):
            suffix = "," if index < len(fields) - 1 else ""
            lines.append(f"  {field_name.lower()} = {{{field_value}}}{suffix}")
        lines.append("}")

        return ResolvedEntry(
            citekey=stable_key,
            doi=doi,
            bibtex=format_bibtex_entry("\n".join(lines)),
            title=normalized_title,
            authors=normalized_authors,
            year=normalized_year,
            entry_type=normalized_type,
            source=source,
            resolution_method=resolution_method,
        )

    def _extract_doi(self, text: str) -> str | None:
        match = _DOI_PATTERN.search(text or "")
        if not match:
            return None
        return match.group(1).rstrip(".,;)")

    @staticmethod
    def _crossref_type_to_bibtex(crossref_type: str) -> str:
        mapping = {
            "journal-article": "article",
            "proceedings-article": "inproceedings",
            "book": "book",
            "book-chapter": "incollection",
            "dissertation": "phdthesis",
            "report": "techreport",
            "dataset": "misc",
            "posted-content": "misc",
            "monograph": "book",
        }
        return mapping.get(crossref_type, "misc")

    def _graph_type_to_bibtex(self, work: GraphWork) -> str:
        if work.raw_type:
            entry_type = self._crossref_type_to_bibtex(work.raw_type)
            if entry_type != "misc" or work.raw_type in {"dataset", "posted-content"}:
                return entry_type

        mapping = {
            "article": "article",
            "book": "book",
            "book-chapter": "incollection",
            "dataset": "misc",
            "dissertation": "phdthesis",
            "editorial": "article",
            "letter": "article",
            "preprint": "misc",
            "report": "techreport",
            "review": "article",
        }
        return mapping.get(work.work_type, "misc")


def resolution_quality(method: str) -> int:
    return _QUALITY_SCORES.get(method, 0)


def make_stable_citekey(
    authors: str,
    year: str,
    title: str,
    doi: str | None = None,
) -> str:
    author_part = authors.split(" and ", 1)[0].strip()
    if "," in author_part:
        family = author_part.split(",", 1)[0]
    elif author_part:
        family = author_part.split()[-1]
    else:
        family = "paper"

    normalized_family = _ascii_key(family) or "paper"
    normalized_year = re.sub(r"[^0-9]", "", year or "")[:4] or "nd"

    title_word = ""
    for token in re.split(r"\W+", title.lower()):
        ascii_token = _ascii_key(token)
        if ascii_token and ascii_token not in _COMMON_TITLE_WORDS:
            title_word = ascii_token
            break
    if not title_word and doi:
        title_word = _ascii_key(doi.split("/")[-1])[:12]
    if not title_word:
        title_word = "paper"

    return f"{normalized_family}{normalized_year}{title_word}"[:40] or "paper"


def format_bibtex_entry(
    bibtex: str,
    *,
    citekey: str | None = None,
    entry_type: str | None = None,
) -> str:
    parsed = _parse_bibtex_entry(bibtex)
    if not parsed:
        if citekey is None:
            return bibtex.strip()
        return re.sub(r"(@\w+\{)[^,]+,", rf"\g<1>{citekey},", bibtex.strip(), count=1)

    parsed_type, parsed_citekey, fields = parsed
    normalized_type = (entry_type or parsed_type).lower()
    normalized_key = citekey or parsed_citekey

    order_map = {field: index for index, field in enumerate(_PREFERRED_FIELD_ORDER)}
    indexed_fields = list(enumerate(fields))
    indexed_fields.sort(
        key=lambda item: (
            order_map.get(item[1][0].lower(), len(_PREFERRED_FIELD_ORDER)),
            item[0],
        )
    )

    lines = [f"@{normalized_type}{{{normalized_key},"]
    total_fields = len(indexed_fields)
    for position, (_, (field_name, field_value)) in enumerate(indexed_fields):
        suffix = "," if position < total_fields - 1 else ""
        lines.append(f"  {field_name.lower()} = {{{field_value}}}{suffix}")
    lines.append("}")
    return "\n".join(lines)


def update_bibtex_citekey(bibtex: str, citekey: str) -> str:
    parsed = _parse_bibtex_entry(bibtex)
    if not parsed:
        return re.sub(r"(@\w+\{)[^,]+,", rf"\g<1>{citekey},", bibtex.strip(), count=1)
    entry_type, _, _ = parsed
    return format_bibtex_entry(bibtex, citekey=citekey, entry_type=entry_type)


def normalize_title_key(title: str) -> str:
    ascii_title = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", "", ascii_title.lower())


def _graph_venue_field(entry_type: str) -> str | None:
    if entry_type == "article":
        return "journal"
    if entry_type in {"inproceedings", "incollection"}:
        return "booktitle"
    return None


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _ascii_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return re.sub(r"[^a-z0-9]", "", normalized.encode("ascii", "ignore").decode("ascii").lower())


def _parse_bibtex_entry(bibtex: str) -> tuple[str, str, list[tuple[str, str]]] | None:
    text = bibtex.strip()
    header_match = re.match(r"@(?P<entry_type>\w+)\{(?P<citekey>[^,]+),", text, re.DOTALL)
    if not header_match:
        return None

    entry_type = header_match.group("entry_type").strip()
    citekey = header_match.group("citekey").strip()
    position = header_match.end()
    fields: list[tuple[str, str]] = []

    while position < len(text):
        position = _skip_bib_whitespace(text, position)
        if position >= len(text) or text[position] == "}":
            break

        field_start = position
        while position < len(text) and text[position] not in "=}":
            position += 1
        if position >= len(text) or text[position] != "=":
            break

        field_name = text[field_start:position].strip().rstrip(",")
        position += 1
        position = _skip_bib_whitespace(text, position)
        if position >= len(text):
            break

        field_value, position = _parse_bib_value(text, position)
        if field_value is None:
            break
        if field_name:
            fields.append((field_name, field_value))

        position = _skip_bib_whitespace(text, position)
        if position < len(text) and text[position] == ",":
            position += 1

    if not fields:
        return None
    return entry_type, citekey, fields


def _skip_bib_whitespace(text: str, position: int) -> int:
    while position < len(text) and text[position] in {" ", "\t", "\r", "\n", ","}:
        position += 1
    return position


def _parse_bib_value(text: str, position: int) -> tuple[str | None, int]:
    if text[position] == "{":
        return _parse_braced_bib_value(text, position)
    if text[position] == '"':
        return _parse_quoted_bib_value(text, position)

    start = position
    while position < len(text) and text[position] not in {",", "\n", "}"}:
        position += 1
    value = _normalize_bib_value(text[start:position])
    return value or None, position


def _parse_braced_bib_value(text: str, position: int) -> tuple[str | None, int]:
    depth = 0
    start = position + 1
    position += 1
    while position < len(text):
        character = text[position]
        if character == "{":
            depth += 1
        elif character == "}":
            if depth == 0:
                value = _normalize_bib_value(text[start:position])
                return value or None, position + 1
            depth -= 1
        position += 1
    return None, position


def _parse_quoted_bib_value(text: str, position: int) -> tuple[str | None, int]:
    position += 1
    start = position
    escaped = False
    while position < len(text):
        character = text[position]
        if character == '"' and not escaped:
            value = _normalize_bib_value(text[start:position])
            return value or None, position + 1
        escaped = character == "\\" and not escaped
        if character != "\\":
            escaped = False
        position += 1
    return None, position


def _normalize_bib_value(value: str) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip()
    return compact.replace("\u2013", "--")
