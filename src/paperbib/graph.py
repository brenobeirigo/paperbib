"""Traverse citation graph data for DOI seed papers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests

_OPENALEX_WORKS_URL = "https://api.openalex.org/works"
_REQUEST_TIMEOUT = 20
_FORWARD_PAGE_SIZE = 100


@dataclass
class GraphWork:
    work_id: str
    doi: str | None
    title: str
    authors: str = ""
    year: str = ""
    venue: str = ""
    work_type: str = ""
    raw_type: str = ""
    referenced_work_ids: list[str] = field(default_factory=list)


class OpenAlexClient:
    """Resolve seed DOIs and expand one-hop citation graph neighbors."""

    def __init__(
        self,
        mailto: str = "paperbib@example.com",
        session: requests.Session | None = None,
    ) -> None:
        self.mailto = mailto
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": f"paperbib/0.1 (mailto:{mailto})"})

    def get_work_by_doi(self, doi: str) -> GraphWork | None:
        normalized_doi = _normalize_doi(doi)
        if not normalized_doi:
            return None

        encoded = quote(f"https://doi.org/{normalized_doi}", safe=":/")
        data = self._get_json(f"{_OPENALEX_WORKS_URL}/{encoded}")
        if not data:
            return None
        return self._parse_work(data)

    def get_work(self, work_id: str) -> GraphWork | None:
        work_key = _openalex_work_key(work_id)
        if not work_key:
            return None

        data = self._get_json(f"{_OPENALEX_WORKS_URL}/{work_key}")
        if not data:
            return None
        return self._parse_work(data)

    def get_referenced_works(
        self,
        work: GraphWork,
        limit: int | None = None,
    ) -> list[GraphWork]:
        referenced_ids = work.referenced_work_ids
        if limit is not None:
            referenced_ids = referenced_ids[:limit]

        papers: list[GraphWork] = []
        for referenced_work_id in referenced_ids:
            referenced_work = self.get_work(referenced_work_id)
            if referenced_work:
                papers.append(referenced_work)
        return papers

    def get_citing_works(
        self,
        work: GraphWork,
        limit: int | None = None,
    ) -> list[GraphWork]:
        work_key = _openalex_work_key(work.work_id)
        if not work_key:
            return []

        papers: list[GraphWork] = []
        page = 1

        while True:
            remaining = None if limit is None else max(limit - len(papers), 0)
            if remaining == 0:
                break

            page_size = _FORWARD_PAGE_SIZE if remaining is None else min(_FORWARD_PAGE_SIZE, remaining)
            params = {
                "filter": f"cites:{work_key}",
                "page": page,
                "per-page": page_size,
            }
            data = self._get_json(_OPENALEX_WORKS_URL, params=params)
            if not data:
                break

            results = data.get("results", [])
            if not results:
                break

            for item in results:
                parsed = self._parse_work(item)
                if parsed:
                    papers.append(parsed)
                if limit is not None and len(papers) >= limit:
                    return papers[:limit]

            meta = data.get("meta", {})
            total = int(meta.get("count", 0) or 0)
            if total and len(papers) >= min(total, limit or total):
                break

            page += 1

        return papers[:limit] if limit is not None else papers

    def _get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            response = self._session.get(url, params=params, timeout=_REQUEST_TIMEOUT)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None

        try:
            return response.json()
        except ValueError:
            return None

    def _parse_work(self, data: dict[str, Any]) -> GraphWork | None:
        work_id = str(data.get("id") or data.get("ids", {}).get("openalex") or "").strip()
        title = _normalize_space(
            str(data.get("title") or data.get("display_name") or "")
        )
        if not work_id or not title:
            return None

        authorships = data.get("authorships", []) or []
        authors = " and ".join(
            _normalize_space(str(authorship.get("author", {}).get("display_name") or ""))
            for authorship in authorships
            if _normalize_space(str(authorship.get("author", {}).get("display_name") or ""))
        )

        primary_location = data.get("primary_location") or {}
        source = primary_location.get("source") or {}
        publication_year = data.get("publication_year")
        year = str(publication_year) if publication_year else ""

        return GraphWork(
            work_id=work_id,
            doi=_normalize_doi(str(data.get("doi") or data.get("ids", {}).get("doi") or "")) or None,
            title=title,
            authors=authors,
            year=year,
            venue=_normalize_space(str(source.get("display_name") or "")),
            work_type=_normalize_space(str(data.get("type") or "")),
            raw_type=_normalize_space(str(primary_location.get("raw_type") or "")),
            referenced_work_ids=[
                str(work_ref).strip()
                for work_ref in data.get("referenced_works", []) or []
                if str(work_ref).strip()
            ],
        )


def _openalex_work_key(work_id: str) -> str:
    value = (work_id or "").strip()
    if not value:
        return ""
    if value.lower().startswith("https://openalex.org/"):
        return value.rsplit("/", 1)[-1]
    return value


def _normalize_doi(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""

    lowered = text.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break

    return re.sub(r"\s+", "", text).rstrip(".,;)")


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()