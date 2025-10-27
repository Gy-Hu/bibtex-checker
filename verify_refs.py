#!/usr/bin/env python3
"""Verify bibliography entries against the Semantic Scholar API proxy."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import typing as t
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import bibtexparser


BASE_URL = "https://lifuai.com/api/v1/graph/v1/"
DEFAULT_FIELDS = "title,year,venue,externalIds,url,publicationTypes"


@dataclass
class BibEntry:
    kind: str
    key: str
    fields: dict[str, t.Union[str, list[str]]]


@dataclass
class VerificationResult:
    entry: BibEntry
    status: str
    message: str
    matched_title: str | None = None
    matched_year: int | None = None
    matched_id: str | None = None
    api_data: dict[str, t.Any] | None = None


class SemanticScholarClient:
    """Thin wrapper around the Semantic Scholar proxy."""

    def __init__(
        self,
        api_key: str,
        rate_limit_delay: float = 0.0,
        max_retries: int = 3,
        backoff_seconds: float = 5.0,
    ) -> None:
        self.api_key = api_key
        self.rate_limit_delay = rate_limit_delay
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds

    def _request(self, path: str, params: dict[str, t.Any] | None = None) -> t.Any:
        attempt = 0
        while True:
            url = urllib.parse.urljoin(BASE_URL, path)
            if params:
                url = f"{url}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": "refs-verifier/0.1",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:  # pragma: no cover - network edge case
                body = exc.read().decode("utf-8", errors="ignore")
                if exc.code == 429 and attempt < self.max_retries:
                    wait_for = self.backoff_seconds * (attempt + 1)
                    time.sleep(wait_for)
                    attempt += 1
                    continue
                raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
            except urllib.error.URLError as exc:  # pragma: no cover - network edge case
                if attempt < self.max_retries:
                    wait_for = self.backoff_seconds * (attempt + 1)
                    time.sleep(wait_for)
                    attempt += 1
                    continue
                raise RuntimeError(f"Network error: {exc.reason}") from exc
            if self.rate_limit_delay:
                time.sleep(self.rate_limit_delay)
            return data

    def get_paper_by_doi(self, doi: str) -> dict[str, t.Any] | None:
        try:
            return t.cast(
                dict[str, t.Any],
                self._request(f"paper/DOI:{doi}", {"fields": DEFAULT_FIELDS}),
            )
        except RuntimeError as exc:
            if "404" in str(exc):
                return None
            raise

    def search_paper_by_title(self, title: str) -> dict[str, t.Any] | None:
        data = t.cast(
            dict[str, t.Any],
            self._request(
                "paper/search",
                {
                    "query": title,
                    "fields": DEFAULT_FIELDS,
                    "limit": 1,
                },
            ),
        )
        hits = data.get("data", [])
        if not hits:
            return None
        return t.cast(dict[str, t.Any], hits[0])


def parse_bibtex(path: str) -> list[BibEntry]:
    parser = bibtexparser.bparser.BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    parser.homogenize_fields = False
    with open(path, "r", encoding="utf-8") as handle:
        database = bibtexparser.load(handle, parser=parser)

    entries: list[BibEntry] = []
    for raw in database.entries:
        entry_type = raw.get("ENTRYTYPE", "").strip()
        entry_id = raw.get("ID", "").strip()
        # Copy fields except bibtexparser metadata keys.
        fields: dict[str, t.Union[str, list[str]]] = {
            key: value for key, value in raw.items() if key not in {"ENTRYTYPE", "ID"}
        }
        entries.append(BibEntry(kind=entry_type, key=entry_id, fields=fields))
    return entries


def first_field(fields: dict[str, t.Union[str, list[str]]], name: str) -> str | None:
    value = fields.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        return value[0]
    return value


def normalize_title(text: str | None) -> str:
    if not text:
        return ""
    # Drop LaTeX braces and simple macros.
    stripped = text.replace("\n", " ")
    stripped = re.sub(r"\\[{}]", "", stripped)
    stripped = stripped.replace("{", "").replace("}", "")
    stripped = stripped.replace("~", " ")
    stripped = stripped.replace("\\&", "&")
    stripped = stripped.replace("\\%", "%")
    stripped = stripped.replace("\\_", "_")
    stripped = stripped.replace("\\#", "#")
    stripped = stripped.replace("\\ss", "ss")
    stripped = re.sub(r"\\[a-zA-Z]+", "", stripped)
    stripped = stripped.replace("  ", " ")
    stripped = stripped.strip()
    return re.sub(r"\s+", " ", stripped).lower()


def compare_titles(bib_title: str | None, api_title: str | None) -> float:
    import difflib

    norm_a = normalize_title(bib_title)
    norm_b = normalize_title(api_title)
    if not norm_a or not norm_b:
        return 0.0
    return difflib.SequenceMatcher(None, norm_a, norm_b).ratio()


def verify_entry(entry: BibEntry, client: SemanticScholarClient) -> VerificationResult:
    doi = first_field(entry.fields, "doi")
    title = first_field(entry.fields, "title")
    year_raw = first_field(entry.fields, "year")
    expected_year = None
    if year_raw:
        try:
            expected_year = int(re.sub(r"\D", "", year_raw))
        except ValueError:
            expected_year = None

    try:
        if doi:
            response = client.get_paper_by_doi(doi)
            if not response:
                return VerificationResult(entry, "not_found", "DOI not found in API")
        else:
            if not title:
                return VerificationResult(
                    entry, "skipped", "No title or DOI available for lookup"
                )
            response = client.search_paper_by_title(normalize_title(title))
            if not response:
                return VerificationResult(entry, "not_found", "No search hits for title")
    except RuntimeError as exc:
        return VerificationResult(entry, "error", f"API error: {exc}")

    api_title = response.get("title")
    similarity = compare_titles(title, api_title)
    match_year = response.get("year")
    matched_id = response.get("paperId") or response.get("externalIds", {}).get("DOI")

    if similarity < 0.75:
        return VerificationResult(
            entry,
            "mismatch",
            f"Title similarity {similarity:.2f} below threshold; API title: {api_title!r}",
            matched_title=api_title,
            matched_year=match_year,
            matched_id=matched_id,
            api_data=t.cast(dict[str, t.Any], response),
        )

    if expected_year and match_year and expected_year != match_year:
        return VerificationResult(
            entry,
            "mismatch",
            f"Year differs (bib {expected_year} vs API {match_year})",
            matched_title=api_title,
            matched_year=match_year,
            matched_id=matched_id,
            api_data=t.cast(dict[str, t.Any], response),
        )

    return VerificationResult(
        entry,
        "verified",
        "Title and year match within tolerance",
        matched_title=api_title,
        matched_year=match_year,
        matched_id=matched_id,
        api_data=t.cast(dict[str, t.Any], response),
    )


def run(args: argparse.Namespace) -> int:
    entries = parse_bibtex(args.bib_path)
    if not entries:
        print("No bibliography entries found.", file=sys.stderr)
        return 1

    client = SemanticScholarClient(
        args.api_key,
        rate_limit_delay=args.delay,
        max_retries=args.max_retries,
        backoff_seconds=args.backoff,
    )
    counters: dict[str, int] = {"verified": 0, "mismatch": 0, "not_found": 0, "error": 0, "skipped": 0}
    failed_logs: list[str] = []

    for entry in entries:
        result = verify_entry(entry, client)
        counters[result.status] = counters.get(result.status, 0) + 1
        prefix = {
            "verified": "[OK]",
            "mismatch": "[!!]",
            "not_found": "[??]",
            "error": "[XX]",
            "skipped": "[--]",
        }.get(result.status, "[..]")
        details = f"{entry.key}"
        title = first_field(entry.fields, "title")
        if title:
            details += f" – {title}"
        print(f"{prefix} {details}")
        print(f"     {result.message}")
        if result.matched_title and normalize_title(result.matched_title) != normalize_title(title):
            print(f"     API title: {result.matched_title}")
        if result.matched_year:
            print(f"     API year: {result.matched_year}")
        if result.matched_id:
            print(f"     API id: {result.matched_id}")
        if args.failure_log and result.status in {"mismatch", "not_found", "error"}:
            failed_logs.append(_format_failure_log_entry(result))
        if result.status in {"mismatch", "not_found", "error"} and args.stop_on_failure:
            print("Stopping early due to failure.", file=sys.stderr)
            break

    if args.failure_log and failed_logs:
        _write_failure_log(args.failure_log, failed_logs)

    print("\nSummary:")
    for status in ("verified", "mismatch", "not_found", "error", "skipped"):
        count = counters.get(status, 0)
        print(f"  {status:9s}: {count:3d}")
    return 0


def _write_failure_log(path: str, blocks: list[str]) -> None:
    target = Path(path)
    if not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    content = "\n\n".join(blocks).rstrip() + "\n"
    target.write_text(content, encoding="utf-8")


def _format_failure_log_entry(result: VerificationResult) -> str:
    entry = result.entry
    kind = entry.kind or "misc"
    key = entry.key or "unnamed"
    header = f"% Failure: {key} [{result.status}] - {result.message}"
    original = _disable_bibtex_entry(_format_bibtex_entry(kind, key, entry.fields))
    corrected_fields = _build_corrected_fields(result)
    if corrected_fields:
        corrected = _format_bibtex_entry(kind, key, corrected_fields)
    else:
        corrected = "% No corrected BibTeX suggestion available (missing API data)."
    return "\n".join([header, original, corrected])


def _build_corrected_fields(result: VerificationResult) -> dict[str, str] | None:
    if not result.api_data:
        return None
    fields: dict[str, str] = {
        name: _coerce_field_value(value) for name, value in result.entry.fields.items()
    }

    def set_field(name: str, value: t.Any | None) -> None:
        if value is None:
            return
        text = str(value)
        if fields.get(name) != text:
            fields[name] = text

    set_field("title", result.matched_title)
    if result.matched_year:
        set_field("year", result.matched_year)
    external_ids = result.api_data.get("externalIds") or {}
    doi = external_ids.get("DOI")
    set_field("doi", doi)
    set_field("url", result.api_data.get("url"))
    venue = result.api_data.get("venue")
    if venue:
        for candidate in ("journal", "booktitle", "venue"):
            if candidate in fields:
                set_field(candidate, venue)
                break
        else:
            set_field("venue", venue)
    return fields


def _format_bibtex_entry(
    kind: str, key: str, fields: dict[str, t.Union[str, list[str]]]
) -> str:
    items = []
    for name, value in fields.items():
        if isinstance(value, list):
            formatted = " and ".join(str(item) for item in value)
        else:
            formatted = str(value)
        items.append((name, formatted))
    lines = [f"@{kind}{{{key},"]
    for index, (name, value) in enumerate(items):
        suffix = "," if index < len(items) - 1 else ""
        lines.append(f"  {name} = {{{value}}}{suffix}")
    lines.append("}")
    return "\n".join(lines)


def _disable_bibtex_entry(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("@"):
            leading = line[: len(line) - len(stripped)]
            lines[index] = f"{leading}{stripped[1:]}"
            break
    return "\n".join(lines)


def _coerce_field_value(value: t.Union[str, list[str]]) -> str:
    if isinstance(value, list):
        return " and ".join(str(item) for item in value)
    return str(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify TACAS references against the Semantic Scholar proxy API.",
    )
    parser.add_argument(
        "--bib-path",
        default="refs.bib",
        help="Path to the BibTeX file to verify (default: refs.bib).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for the Semantic Scholar proxy. "
        "If omitted, the LIFUAI_API_KEY environment variable is used.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Optional delay (in seconds) between API calls.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Number of times to retry on HTTP 429 (Too Many Requests) errors.",
    )
    parser.add_argument(
        "--backoff",
        type=float,
        default=5.0,
        help="Base backoff (seconds) applied between retries when HTTP 429 is received.",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop processing after the first mismatch/not_found/error.",
    )
    parser.add_argument(
        "--failure-log",
        default="corrected.bib",
        help="Path to write corrected BibTeX suggestions for failed entries "
        "(default: corrected.bib). Use an empty string to disable.",
    )
    args = parser.parse_args(argv)
    if args.api_key is None:
        args.api_key = _env_api_key()
    if not args.api_key:
        parser.error(
            "No API key provided. Use --api-key or set the LIFUAI_API_KEY environment variable."
        )
    if args.failure_log == "":
        args.failure_log = None
    return args


def _env_api_key() -> str | None:
    return os.environ.get("LIFUAI_API_KEY")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
