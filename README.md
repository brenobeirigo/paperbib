# paperbib

`paperbib` is an agent-first bibliography tool for literature review workflows.

It takes a paper source or a set of seed papers, resolves metadata through Crossref and OpenAlex, and produces:

- a clean `.bib` file for downstream review or drafting
- a structured `.report.json` sidecar that explains what was included, skipped, merged, or failed

The design goal is ResearchRabbit-style discovery with deterministic, automatable artifacts rather than a browser UI.

## What It Does

- Resolves a local PDF, local text/HTML file, DOI, or URL into a cited bibliography.
- Expands bounded citation graphs from seed sources through OpenAlex with configurable forward and backward levels.
- Dedupe entries DOI-first, then by normalized `title + year`.
- Keeps heuristic-only entries out of the final `.bib` by default.
- Preserves provenance in the report: seed sources, relation types, resolution method, skip reasons, and warnings.
- Produces stable, normalized citekeys instead of trusting provider-generated keys.

## What It Does Not Do

- It does not bypass paywalls.
- It does not do unbounded graph traversal.
- It does not implement a keyword recommender engine.
- It does not call external metadata sources beyond Crossref and OpenAlex in this version.

## Installation

Python `3.11+` is required.

```bash
pip install -e .
```

For local development:

```bash
pip install -e ".[dev]"
```

## Core Commands

### `paperbib save`

Use `save` when you have a single source paper and want the bibliography cited by that paper.

Supported inputs:

- local PDF path
- local `.txt`, `.md`, `.markdown`, `.html`, `.htm`
- DOI
- HTTP(S) URL

Example:

```bash
paperbib save ./paper.pdf --to ./artifacts/
```

### `paperbib graph`

Use `graph` when you want bounded multi-level discovery around seed papers, similar to a lightweight ResearchRabbit expansion.

Seed inputs may be:

- DOI
- URL
- local PDF path
- local text/HTML path

The graph command can expand:

- backward citations: papers cited by the seed
- forward citations: papers that cite the seed

Depth control:

- `--backward N` traverses `N` backward levels
- `--forward N` traverses `N` forward levels
- `0` disables that direction
- both default to `1`

Example:

```bash
paperbib graph 10.1234/example.2024.001 --to ./artifacts/ --backward 2 --forward 1
```

## Output Contract

By default, every successful run writes two artifacts:

- `name.bib`
- `name.report.json`

If `--to` is a directory, `paperbib` chooses a filename from `--name`, the source title, or a DOI-derived fallback.

If `--to` is an explicit `.bib` path, the report path is derived from that path:

- `out/seed-graph.bib`
- `out/seed-graph.report.json`

### Precision-First Default

The `.bib` file includes only entries that meet the default quality bar:

- `crossref-bibtex`
- `crossref-json`
- `openalex-structured`

Heuristic-only entries are still recorded in the report, but they are skipped from the `.bib` unless you opt in with:

```bash
--include-heuristic
```

### Failure Semantics

- If the run resolves candidates and some entries survive filtering, exit code is `0`.
- If candidates are discovered but zero entries survive precision rules or explicit filters, exit code is `1`, no empty `.bib` is written, and the `.report.json` is still written by default.
- If the source cannot be resolved at all, the command exits `1`.

### `--json`

`--json` prints a compact machine-readable summary to stdout. It mirrors the sidecar summary fields:

- `output_path`
- `report_path`
- `entry_count`
- `included_count`
- `skipped_count`
- `failed_sources`
- `strategy`
- `warnings`

Use it when an agent needs a small control-plane payload without parsing the full report file.

## Report Schema

The `.report.json` sidecar uses this top-level shape:

```json
{
  "command": "save|graph",
  "version": "0.1.0",
  "created_at": "2026-04-22T00:00:00Z",
  "strategy": "resolution strategy string",
  "sources": ["original inputs"],
  "seed_resolutions": [],
  "output_bib": "path or null",
  "counts": {
    "entry_count": 0,
    "included_count": 0,
    "skipped_count": 0,
    "candidate_count": 0,
    "backward_count": 0,
    "forward_count": 0
  },
  "included_entries": [],
  "skipped_entries": [],
  "failed_sources": [],
  "warnings": []
}
```

Each entry report includes:

- `citekey`
- `doi`
- `title`
- `authors`
- `year`
- `entry_type`
- `resolution_method`
- `relation_types`
- `seed_sources`
- `included`
- `reason`
- `connection_count`
- `resolution_quality`

`seed_resolutions` records how each input seed was interpreted, for example:

- raw DOI
- URL metadata DOI
- PDF body DOI
- title-to-DOI resolution for local text

## Agent Playbooks

### 1. Local PDF to `.bib`

```bash
paperbib save ./papers/source-paper.pdf --to ./out/
```

Use when the source paper is already on disk and you want its cited bibliography.

### 2. DOI to cited bibliography

```bash
paperbib save 10.1234/example.2024.001 --to ./out/ --json
```

Use when the agent already knows the DOI and wants the fastest path to clean references.

### 3. Multi-seed graph expansion from a file

Create `seeds.txt` with one seed per line. Each line may be a DOI, URL, or local path.

```text
10.1234/example.2024.001
https://example.org/paper
./papers/local-seed.pdf
```

Run:

```bash
paperbib graph --from-file ./seeds.txt --to ./out/graph.bib --backward 2 --forward 1
```

### 4. Precision-first graph with filters

```bash
paperbib graph \
  --from-file ./seeds.txt \
  --to ./out/graph.bib \
  --year-from 2019 \
  --type article \
  --type inproceedings
```

Use this when the agent wants a review-ready bibliography and is willing to drop weak or out-of-scope entries.

### 5. Heuristic opt-in recovery

```bash
paperbib save ./papers/source-paper.pdf --to ./out/ --include-heuristic
```

Use this only when recall matters more than cleanliness and the agent is prepared to review lower-confidence entries.

## Resolution and Filtering Rules

### Resolution methods

- `crossref-bibtex`: Crossref returned BibTeX directly.
- `crossref-json`: Crossref metadata was converted into normalized BibTeX.
- `openalex-structured`: OpenAlex metadata was converted into normalized BibTeX.
- `heuristic`: A fallback entry was inferred from raw reference text.

### Graph traversal and filters

`paperbib graph` supports bounded traversal controls plus post-resolution filters:

- `--backward`
- `--forward`
- `--year-from`
- `--year-to`
- repeatable `--type`

Traversal depth is applied during graph expansion. Year and type filters are applied after metadata resolution and before writing the final `.bib`. Skipped records remain visible in the report with explicit reasons.

### Ordering

Final entries are ordered deterministically by:

1. `connection_count` descending
2. `resolution_quality` descending
3. `year` descending
4. `title` ascending

That ordering is used for both `.bib` output and report entries.

## Library Usage

Basic save flow:

```python
from paperbib.fetch import PaperBibClient

client = PaperBibClient(mailto="agent@example.com")
result = client.save("10.1234/example.2024.001", to="out/")

print(result.output_path)
print(result.report_path)
print(result.entry_count)
```

Basic graph flow:

```python
from paperbib.fetch import PaperBibClient

client = PaperBibClient(mailto="agent@example.com")
result = client.save_graph(
    ["10.1234/example.2024.001", "./papers/seed.pdf"],
    to="out/graph.bib",
    year_from=2019,
    entry_types=["article"],
)
```

## Operational Notes

- Set a real `--mailto` value in production or automated agent runs.
- Crossref and OpenAlex availability directly affects result quality.
- Local PDF/title extraction is heuristic; when source metadata is weak, the report is the source of truth for what happened.
- The report is intended to be the audit trail. Agents should read it before trusting low-yield runs.
