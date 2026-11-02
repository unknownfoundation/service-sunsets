#!/usr/bin/env python3
"""Check the register in data/register.toml and render it into site/index.html and README.md.

Run from anywhere:
    python3 -I build.py write      check the data, then rewrite the generated blocks of both files
    python3 -I build.py check      check the data and fail if either file is out of date
    python3 -I build.py release    as check, and also fail while any entry still has `verify`
    python3 -I build.py links      open every link of the data, the page and the README (needs network)

Prints labelled lines (`entries: 125`, `state shut-down: 46`, `link 404: <url>`, ...). Data errors go to
stderr as `error <slug>: <what is wrong>`. Exit code: 0 ok, 1 a check failed, 2 a file is missing or malformed.

Only the text between <!-- register:NAME --> and <!-- /register:NAME --> is generated; the rest of
both files is written by hand. Output depends on the data file alone, never on the clock, so two runs
give the same bytes. The page moves an entry from "End date set" to "Shut down" in the browser once
the date passes.

One file on purpose: `python3 -I` keeps the script's folder off the import path, so a package
beside it could not be imported.

Data format. At the top: `updated` (the day the list was last checked against AWS), `repository_url`
and `site_url`. Then one [[entry]] table per service or feature:
    slug           kebab-case, unique; the row's anchor on the page
    name           the name AWS uses; "Old → New" for a rename
    status         shutdown | sunset | maintenance | renamed | reversed (the AWS lifecycle stage)
    group          one of GROUPS
    scope          service | feature | region (feature: part of a service; region: some Regions only)
    what           what it did, in our own words, at most 20 words, no final period
    announced      date AWS announced the change        shutdown and sunset need `ends`; maintenance
    closed_to_new  date new customers were turned away  needs `announced` or `closed_to_new`; renamed
    ends           end of support: the service stops    and reversed need `announced` and take no `ends`;
                                                        the dates that are set come in this order
    replacement    where to move, naming AWS products; empty when no AWS page names one
    note           a sentence or two a reader must not miss (optional)
    source         the AWS page for this change
    guide          a second AWS page, such as a migration guide (optional)
    registry       the AWS lifecycle pages that list the entry (optional)
    verify         what no AWS page has confirmed yet; `release` fails while it is set
"""
import argparse
import concurrent.futures
import html
import itertools
import json
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple, Self
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "register.toml"
PAGE_PATH = ROOT / "site" / "index.html"
README_PATH = ROOT / "README.md"

Table = dict[str, Any]  # one table as tomllib reads it


class Status(StrEnum):
    """The AWS lifecycle stage an entry is listed under."""
    SHUTDOWN = "shutdown"
    SUNSET = "sunset"
    MAINTENANCE = "maintenance"
    RENAMED = "renamed"
    REVERSED = "reversed"


class State(StrEnum):
    """What the reader sees: the status read on a given day."""
    SHUT_DOWN = "shut-down"
    END_DATE_SET = "end-date-set"
    NO_NEW_CUSTOMERS = "no-new-customers"
    RENAMED = "renamed"
    REVERSED = "reversed"


class Scope(StrEnum):
    SERVICE = "service"
    FEATURE = "feature"
    REGION = "region"


class DateKind(StrEnum):
    """Which of an entry's dates stands for it in sorting, grouping and the Date column."""
    END = "ends"
    CLOSED_TO_NEW = "closed"
    ANNOUNCED = "announced"
    RENAMED = "renamed"
    REVERSED = "reversed"


class StateText(NamedTuple):
    label: str
    meaning: str


class ScopeText(NamedTuple):
    tag: str           # shown after an entry's name; empty for a whole service
    filter_label: str  # the option in the page's Kind filter


# The one place the page's names live: the table, the filters, the legend, the README and app.js read them here.
STATE_TEXTS = {
    State.SHUT_DOWN: StateText("Shut down", "Gone. It no longer works."),
    State.END_DATE_SET: StateText("End date set", "Works until the date, then stops."),
    State.NO_NEW_CUSTOMERS: StateText("No new customers",
                                      "Runs for existing customers and gets no new features. No end date yet."),
    State.RENAMED: StateText("Renamed", "The same service under a new name."),
    State.REVERSED: StateText("Reversed", "AWS cancelled a planned retirement."),
}
SCOPE_TEXTS = {
    Scope.SERVICE: ScopeText("", "Whole services"),
    Scope.FEATURE: ScopeText("feature", "Features"),
    Scope.REGION: ScopeText("some Regions", "Some Regions"),
}
DATE_KIND_WORDS = {
    DateKind.CLOSED_TO_NEW: "No new customers since",
    DateKind.ANNOUNCED: "Announced",
    DateKind.RENAMED: "Renamed",
    DateKind.REVERSED: "Reversed",
}
END_DATE_WORDS = {State.SHUT_DOWN: "Ended", State.END_DATE_SET: "Ends"}
GROUPS = ("AI & ML", "Analytics", "Business apps", "Communication", "Compute & containers", "Databases",
          "Developer tools", "End-user computing", "IoT", "Management", "Media", "Migration & transfer",
          "Networking", "Security & identity", "Storage")

DATE_FIELD_LABELS = {"announced": "Announced", "closed_to_new": "No new customers", "ends": "End of support"}
DATE_FIELDS = tuple(DATE_FIELD_LABELS)
LINK_FIELDS = ("source", "guide")
REQUIRED_FIELDS = ("slug", "name", "status", "group", "scope", "what", "source")
OPTIONAL_FIELDS = (*DATE_FIELDS, "replacement", "note", "guide", "registry", "verify")
VERSION_REQUIRED_FIELDS = ("slug", "name", "ends", "replacement", "source")
VERSION_OPTIONAL_FIELDS = ("note",)
VERSIONS_TITLE = "Version deadlines"
VERSIONS_INTRO = ("Not services, but versions that most AWS accounts run. After a Lambda runtime's date, AWS may stop "
                  "its security patches and support. AWS can move these dates, so check its pages before you plan.")
SETTINGS_URL_FIELDS = ("repository_url", "site_url")
SETTINGS_FIELDS = ("updated", *SETTINGS_URL_FIELDS)
SEARCHED_FIELDS = ("name", "what", "replacement", "group", "note")
CHECKED_TEXT_FIELDS = ("what", "replacement", "note")
TEXT_FIELDS = ("slug", "name", "status", "group", "scope", "what", "replacement", "note", "source", "guide", "verify")
# Fields a reader sees in a table cell: a line break would split the README's row.
ONE_LINE_FIELDS = ("name", "what", "replacement", "note")
LINE_BREAK_PATTERN = re.compile(r"[\r\n]")
DATA_ARRAYS = ("entry", "version")

ALLOWED_LINK_HOSTS = frozenset({"docs.aws.amazon.com", "aws.amazon.com"})
# Browsers read a backslash in an https URL as "/", Python does not, so "evil\@docs.aws..." would pass the host
# check. Whitespace, quotes, angle brackets and parentheses would end a Markdown link or an attribute early.
UNSAFE_URL_PATTERN = re.compile(r"""[\s\\"'<>()`]""")
WHAT_WORD_LIMIT = 20
# Twin of the four columns of `.upcoming-cards` in site/css/styles.css: change both together.
UPCOMING_CARD_LIMIT = 4
# Twin of the five <th> in the hand-written <thead> of site/index.html: change both together.
TABLE_COLUMN_COUNT = 5
# The window starts on 1 Nov 2024, so 2024 holds two months only and is titled "late 2024".
FIRST_FULL_YEAR = 2025
MONTH_ABBREVIATIONS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# A replacement that starts this way names no product ("Another tool") and tells the reader nothing.
VAGUE_REPLACEMENT_PATTERN = re.compile(r"^(?:another|other|alternative|a different|some)\b", re.IGNORECASE)
# Words that make text read as generated. Text that uses them is rewritten, not excused.
BANNED_WORDS = ("seamless", "seamlessly", "robust", "robustly", "leverage", "leverages", "comprehensive",
                "cutting-edge", "streamline", "streamlines", "empower", "empowers", "delve", "crucial",
                "pivotal", "utilize", "effortless", "effortlessly", "game-changer", "unlock", "unlocks",
                "elevate", "landscape", "tapestry", "holistic", "synergy", "best-in-class", "world-class")
NO_REPLACEMENT_TEXT = "Not named. See the AWS page"
# The class styles.css defines to hide text from sight but not from screen readers.
VISUALLY_HIDDEN_CLASS = "visually-hidden"
MOVE_TO_WORDS = "Move to: "
MARKDOWN_SPECIAL_PATTERN = re.compile(r"([\\|\[\]<>*_`])")

# Ids the generated blocks write besides the slugs; a slug equal to one of them would take its place.
LABELS_SCRIPT_ID = "register-labels"
VERSIONS_TITLE_ID = "versions-title"
GENERATED_BLOCK_PATTERN = re.compile(r"<!-- register:([\w-]+) -->.*?<!-- /register:\1 -->", re.DOTALL)
ID_PATTERN = re.compile(r"""\sid=["']([^"']+)["']""")

HTML_LINK_PATTERN = re.compile(r"""href=["'](https?://[^"']+)["']""")
MARKDOWN_LINK_PATTERN = re.compile(r"\]\((https?://[^)\s]+)\)")
LINK_TIMEOUT_SECONDS = 20
# AWS drops connections when one client opens too many at once, or throttles it with 429 or 503:
# few workers, and a retry with a pause.
LINK_WORKERS = 4
LINK_ATTEMPTS = 3
RETRIED_STATUSES = frozenset({"429", "503"})
LINK_RETRY_PAUSE_SECONDS = 2.0
LINK_USER_AGENT = "Mozilla/5.0 (compatible; register link check)"


@dataclass(frozen=True, slots=True)
class Entry:
    slug: str
    name: str
    status: Status
    group: str
    scope: Scope
    what: str
    source: str
    announced: date | None = None
    closed_to_new: date | None = None
    ends: date | None = None
    replacement: str = ""
    note: str = ""
    guide: str = ""
    registry: tuple[str, ...] = ()
    verify: str = ""

    @classmethod
    def from_table(cls, table: Table) -> Self:
        """Build an entry from a table that find_register_errors has already passed."""
        values = dict(table, status=Status(table["status"]), scope=Scope(table["scope"]))
        values["registry"] = tuple(table.get("registry", ()))
        return cls(**values)


@dataclass(frozen=True, slots=True)
class Version:
    """A version or runtime that most accounts run, with the day AWS ends its support."""
    slug: str
    name: str
    ends: date
    replacement: str
    source: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class Register:
    updated: date
    repository_url: str
    site_url: str
    entries: tuple[Entry, ...]
    versions: tuple[Version, ...] = ()


class KeyDate(NamedTuple):
    day: date
    kind: DateKind


class RenderedFiles(NamedTuple):
    page: str
    readme: str


class LinkResult(NamedTuple):
    url: str
    status: str  # the HTTP status code, or why the link could not be opened
    final_url: str


class DataFileError(Exception):
    """A file is missing or cannot be read as this script expects; main() exits with 2."""


# ---------- reading and checking the data ----------

def read_data(path: Path) -> tuple[Table, list[Table], list[Table]]:
    """The top-level settings, the entry tables and the version tables of the data file."""
    try:
        with path.open("rb") as data_file:
            data = tomllib.load(data_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DataFileError(f"{path.name}: {error}") from error
    return split_data(data, path.name)


def split_data(data: Table, file_name: str) -> tuple[Table, list[Table], list[Table]]:
    """Take the [[entry]] and [[version]] arrays out of the data; the other keys are the settings."""
    entries, versions = (data.get(key, []) for key in DATA_ARRAYS)
    for key, tables in zip(DATA_ARRAYS, (entries, versions)):
        # `[entry]` with single brackets reads as one table, not an array of them.
        if not isinstance(tables, list) or not all(isinstance(table, dict) for table in tables):
            raise DataFileError(f"{file_name}: write each {key} as a [[{key}]] table")
    settings = {key: value for key, value in data.items() if key not in DATA_ARRAYS}
    return settings, entries, versions


def is_day(value: object) -> bool:
    """A TOML date. A TOML date-time reads as a datetime, which a date cannot be compared with."""
    return type(value) is date


def is_safe_url(value: object) -> bool:
    """An https address with a host, no user or password part, and no character a link would end on."""
    if not isinstance(value, str) or UNSAFE_URL_PATTERN.search(value):
        return False
    try:
        parsed = urlparse(value)
        return parsed.scheme == "https" and bool(parsed.hostname) and "@" not in parsed.netloc
    except ValueError:  # an unclosed "[" in the host
        return False


def is_aws_page(url: object) -> bool:
    return isinstance(url, str) and is_safe_url(url) and urlparse(url).hostname in ALLOWED_LINK_HOSTS


def find_settings_errors(settings: Table) -> list[str]:
    errors = [] if is_day(settings.get("updated")) else ["settings: set `updated = YYYY-MM-DD` at the top"]
    errors += [f"settings: {field} must be a plain https address" for field in SETTINGS_URL_FIELDS
               if not is_safe_url(settings.get(field))]
    return errors + [f"settings: unknown key {key}" for key in settings if key not in SETTINGS_FIELDS]


def find_field_errors(table: Table, required: tuple[str, ...], optional: tuple[str, ...]) -> list[str]:
    missing = [f"missing {field}" for field in required if not table.get(field)]
    known_fields = {*required, *optional}
    unknown = [f"unknown field {field}" for field in table if field not in known_fields]
    return missing + unknown


def find_type_errors(table: Table) -> list[str]:
    """A wrong type would crash the other checks, so this runs first and they are skipped when it fails."""
    errors = [f"{field} is not text" for field in TEXT_FIELDS if field in table and not isinstance(table[field], str)]
    errors += [f"{field} is not a date" for field in DATE_FIELDS if field in table and not is_day(table[field])]
    registry = table.get("registry", [])
    if not isinstance(registry, list) or not all(isinstance(url, str) for url in registry):
        errors.append("registry is not a list of addresses")
    return errors


def find_slug_errors(table: Table) -> list[str]:
    slug = table.get("slug")
    return ["slug is not kebab-case"] if slug and not SLUG_PATTERN.fullmatch(slug) else []


def find_value_errors(table: Table) -> list[str]:
    errors = []
    if table.get("status") not in {status.value for status in Status}:
        errors.append(f"status {table.get('status')!r} is not one of {[status.value for status in Status]}")
    if table.get("group") not in GROUPS:
        errors.append(f"group {table.get('group')!r} is not in GROUPS")
    if table.get("scope") not in {scope.value for scope in Scope}:
        errors.append(f"scope {table.get('scope')!r} is not one of {[scope.value for scope in Scope]}")
    return errors


def find_date_errors(table: Table) -> list[str]:
    errors = []
    status = table.get("status")
    if status in (Status.SHUTDOWN, Status.SUNSET) and not table.get("ends"):
        errors.append(f"{status} needs ends")
    if status == Status.MAINTENANCE and not (table.get("announced") or table.get("closed_to_new")):
        errors.append("maintenance needs announced or closed_to_new")
    if status in (Status.RENAMED, Status.REVERSED) and not table.get("announced"):
        errors.append(f"{status} needs announced, the day of the change")
    # The page would show the entry as renamed or reversed but date, sort and list it by its end.
    if status in (Status.RENAMED, Status.REVERSED) and table.get("ends"):
        errors.append(f"{status} takes no ends")
    return errors + find_date_order_errors(table)


def find_date_order_errors(table: Table) -> list[str]:
    """Announced, closed to new customers, ends: in that order, over the dates that are set."""
    days = [(field, table[field]) for field in DATE_FIELDS if table.get(field)]
    return [f"{first_field} is after {second_field}"
            for (first_field, first_day), (second_field, second_day) in itertools.combinations(days, 2)
            if first_day > second_day]


def find_text_errors(table: Table) -> list[str]:
    errors = [f"{field} has a line break" for field in ONE_LINE_FIELDS
              if LINE_BREAK_PATTERN.search(table.get(field, ""))]
    what = table.get("what", "")
    if len(what.split()) > WHAT_WORD_LIMIT:
        errors.append(f"what has {len(what.split())} words, the limit is {WHAT_WORD_LIMIT}")
    if what.endswith("."):
        errors.append("what ends with a period")
    if VAGUE_REPLACEMENT_PATTERN.match(table.get("replacement", "")):
        errors.append(f"replacement names no product: {table['replacement']!r}")
    checked_text = " ".join(table.get(field, "") for field in CHECKED_TEXT_FIELDS).lower()
    errors += [f"banned word {word!r}" for word in BANNED_WORDS if re.search(rf"\b{re.escape(word)}\b", checked_text)]
    return errors


def find_link_errors(table: Table) -> list[str]:
    links = [(field, table[field]) for field in LINK_FIELDS if table.get(field)]
    links += [("registry", url) for url in table.get("registry", [])]
    return [f"{field} is not a plain https AWS page: {url}" for field, url in links if not is_aws_page(url)]


def text_values(tables: list[Table], field: str) -> list[str]:
    """The field's values that are non-empty text; a wrong type is reported by find_type_errors."""
    return [table[field] for table in tables if isinstance(table.get(field), str) and table[field]]


def label_of(table: Table, kind: str, position: int) -> str:
    slug = table.get("slug")
    return slug if isinstance(slug, str) and slug else f"{kind} {position}"


def find_duplicate_errors(tables: list[Table]) -> list[str]:
    errors = []
    for field in ("slug", "name"):
        seen: set[str] = set()
        for value in text_values(tables, field):
            if value in seen:
                errors.append(f"{value}: duplicate {field}")
            seen.add(value)
    return errors


def find_tables_errors(tables: list[Table], kind: str, fields: tuple[tuple[str, ...], tuple[str, ...]],
                       value_checks: tuple[Callable[[Table], list[str]], ...]) -> list[str]:
    errors = []
    for position, table in enumerate(tables, start=1):
        problems = find_field_errors(table, *fields)
        type_errors = find_type_errors(table)
        problems += type_errors if type_errors else [problem for check in value_checks for problem in check(table)]
        errors += [f"{label_of(table, kind, position)}: {problem}" for problem in problems]
    return errors + find_duplicate_errors(tables)


def find_register_errors(tables: list[Table]) -> list[str]:
    if not tables:
        return ["data: add at least one [[entry]] table"]
    checks = (find_slug_errors, find_value_errors, find_date_errors, find_text_errors, find_link_errors)
    return find_tables_errors(tables, "entry", (REQUIRED_FIELDS, OPTIONAL_FIELDS), checks)


def find_version_errors(tables: list[Table]) -> list[str]:
    checks = (find_slug_errors, find_text_errors, find_link_errors)
    return find_tables_errors(tables, "version", (VERSION_REQUIRED_FIELDS, VERSION_OPTIONAL_FIELDS), checks)


def find_shared_slug_errors(entry_tables: list[Table], version_tables: list[Table]) -> list[str]:
    """Slugs become ids on the page, so an entry and a version may not share one."""
    shared = set(text_values(entry_tables, "slug")) & set(text_values(version_tables, "slug"))
    return [f"{slug}: slug used by an entry and a version" for slug in sorted(shared)]


def find_page_ids(page: str) -> set[str]:
    """The ids of the page's hand-written part, and the fixed ids its generated blocks write."""
    hand_written = GENERATED_BLOCK_PATTERN.sub("", page)
    return set(ID_PATTERN.findall(hand_written)) | {LABELS_SCRIPT_ID, VERSIONS_TITLE_ID}


def find_reserved_slug_errors(tables: list[Table], page: str) -> list[str]:
    """app.js finds its elements by id, and the first element with an id wins: a row would take its place."""
    reserved = set(text_values(tables, "slug")) & find_page_ids(page)
    return [f"{slug}: slug is an id the page already uses" for slug in sorted(reserved)]


def find_data_errors(settings: Table, tables: list[Table], version_tables: list[Table], page: str) -> list[str]:
    return (find_settings_errors(settings) + find_register_errors(tables) + find_version_errors(version_tables)
            + find_shared_slug_errors(tables, version_tables)
            + find_reserved_slug_errors(tables + version_tables, page))


# ---------- dates and states ----------

def state_of_end(ends: date, day: date) -> State:
    """Shut down ON the end date, as hasEnded() in site/js/dates.js."""
    return State.SHUT_DOWN if ends <= day else State.END_DATE_SET


def state_on(entry: Entry, day: date) -> State:
    if entry.status is Status.RENAMED:
        return State.RENAMED
    if entry.status is Status.REVERSED:
        return State.REVERSED
    if entry.ends:
        return state_of_end(entry.ends, day)
    return State.NO_NEW_CUSTOMERS


def known(day: date | None) -> date:
    """A date the data checks already require. None here is a bug in this script, not bad data."""
    if day is None:
        raise AssertionError("a date that the data checks require is missing")
    return day


def key_date_of(entry: Entry) -> KeyDate:
    if entry.ends:
        return KeyDate(entry.ends, DateKind.END)
    if entry.status is Status.RENAMED:
        return KeyDate(known(entry.announced), DateKind.RENAMED)
    if entry.status is Status.REVERSED:
        return KeyDate(known(entry.announced), DateKind.REVERSED)
    if entry.closed_to_new:
        return KeyDate(entry.closed_to_new, DateKind.CLOSED_TO_NEW)
    return KeyDate(known(entry.announced), DateKind.ANNOUNCED)


def date_words(key_date: KeyDate, state: State) -> str:
    if key_date.kind is DateKind.END:
        return END_DATE_WORDS[state]
    return DATE_KIND_WORDS[key_date.kind]


def format_day(day: date) -> str:
    return f"{day.day} {MONTH_ABBREVIATIONS[day.month - 1]} {day.year}"


def newest_first(entries: tuple[Entry, ...]) -> list[Entry]:
    """The order of every table, page and README: the latest date at the top; names A to Z inside one date."""
    by_name = sorted(entries, key=lambda entry: entry.name.lower())
    return sorted(by_name, key=lambda entry: key_date_of(entry).day, reverse=True)


def upcoming(entries: tuple[Entry, ...], day: date) -> list[Entry]:
    ahead = [entry for entry in entries if entry.ends and entry.ends > day]
    by_name = sorted(ahead, key=lambda entry: entry.name.lower())
    return sorted(by_name, key=lambda entry: known(entry.ends))


# ---------- HTML ----------

def escape(text: str) -> str:
    return html.escape(text, quote=True)


def render_time(day: date) -> str:
    return f'<time datetime="{day.isoformat()}">{format_day(day)}</time>'


def render_timeline(entry: Entry) -> str:
    items = [(DATE_FIELD_LABELS[field], getattr(entry, field)) for field in DATE_FIELDS if getattr(entry, field)]
    return ''.join(f'<li><span class="timeline-label">{label}</span> {render_time(day)}</li>' for label, day in items)


def render_hidden_text(text: str) -> str:
    return f'<span class="{VISUALLY_HIDDEN_CLASS}">{escape(text)}</span>'


def render_page_link(url: str, words: str, name: str) -> str:
    """Every row's link has the same visible words; the hidden name tells them apart in a screen reader's list."""
    return f'<a href="{escape(url)}">{words}{render_hidden_text(f" for {name}")}</a>'


def render_replacement(replacement: str) -> str:
    return escape(replacement) if replacement else f'<span class="text-muted">{NO_REPLACEMENT_TEXT}</span>'


def render_move_to(replacement: str) -> str:
    # The arrow is decoration that a screen reader would announce as "right arrow"; it hears "Move to:" instead.
    return f'<span aria-hidden="true">→</span>{render_hidden_text(MOVE_TO_WORDS)} {render_replacement(replacement)}'


def render_service_cell(entry: Entry) -> str:
    """The row's header cell, so table navigation names the service with every other cell of the row."""
    tag = SCOPE_TEXTS[entry.scope].tag
    scope_tag = f'<span class="scope-tag">{tag}</span>' if tag else ''
    note = f'<p class="entry-note">{escape(entry.note)}</p>' if entry.note else ''
    # A <td>, not <th scope="row">: <th> may not hold <details>. Each link names its service instead.
    return (f'<td class="cell-service" data-label="Service"><details><summary>'
            f'<strong class="entry-name">{escape(entry.name)}</strong>{scope_tag}'
            f'<span class="entry-what">{escape(entry.what)}</span></summary>'
            f'<div class="entry-details"><ul class="entry-timeline">{render_timeline(entry)}</ul>{note}</div>'
            f"</details></td>")


def render_source_links(entry: Entry) -> str:
    links = [render_page_link(entry.source, "AWS page", entry.name)]
    if entry.guide:
        links.append(render_page_link(entry.guide, "guide", entry.name))
    return " · ".join(links)


def render_row_attributes(entry: Entry, state: State, key_date: KeyDate) -> str:
    """The attributes app.js filters and sorts by; an entry with an end date shows "Ends"/"Ended"."""
    search_text = " ".join(getattr(entry, field) for field in SEARCHED_FIELDS).lower()
    end_date = entry.ends.isoformat() if entry.ends else ""
    return (f'id="{escape(entry.slug)}" class="entry-row" data-state="{state}"'
            f' data-group="{escape(entry.group)}" data-scope="{entry.scope}"'
            f' data-key-date="{key_date.day.isoformat()}" data-end-date="{end_date}"'
            f' data-sort-name="{escape(entry.name.lower())}" data-search-text="{escape(search_text)}"')


def render_state_badge(state: State) -> str:
    return f'<span class="state-badge {state}">{STATE_TEXTS[state].label}</span>'


def render_entry_row(entry: Entry, day: date) -> str:
    state, key_date = state_on(entry, day), key_date_of(entry)
    replacement = render_replacement(entry.replacement)
    return (f"<tr {render_row_attributes(entry, state, key_date)}>{render_service_cell(entry)}"
            f'<td class="cell-status" data-label="Status">{render_state_badge(state)}</td>'
            f'<td class="cell-date" data-label="Date"><span class="date-label">{date_words(key_date, state)}</span>'
            f"{render_time(key_date.day)}</td>"
            f'<td class="cell-replacement" data-label="Move to">{replacement}</td>'
            f'<td class="cell-source" data-label="Source">{render_source_links(entry)}</td></tr>')


def render_year_row(year: int) -> str:
    return f'<tr class="year-row"><th colspan="{TABLE_COLUMN_COUNT}" scope="rowgroup">{year}</th></tr>'


def render_rows(register: Register) -> str:
    '''One <tbody> per year, headed by its year, so `scope="rowgroup"` names the right rows.'''
    rows_by_year: dict[int, list[str]] = {}
    for entry in newest_first(register.entries):
        rows_by_year.setdefault(key_date_of(entry).day.year, []).append(render_entry_row(entry, register.updated))
    return "\n".join(f"<tbody>\n{render_year_row(year)}\n" + "\n".join(rows) + "\n</tbody>"
                     for year, rows in rows_by_year.items())


def render_upcoming_card(entry: Entry, is_hidden: bool) -> str:
    hidden, ends = " hidden" if is_hidden else "", known(entry.ends)
    return (f'<li class="upcoming-card" data-end-date="{ends.isoformat()}"{hidden}>{render_time(ends)}'
            f'<a href="#{escape(entry.slug)}">{escape(entry.name)}</a>'
            f'<span class="card-replacement">{render_move_to(entry.replacement)}</span></li>')


def render_upcoming_cards(register: Register) -> str:
    """Every upcoming entry; those past the limit start hidden and app.js shows them as dates pass."""
    entries = upcoming(register.entries, register.updated)
    return "\n".join(render_upcoming_card(entry, is_hidden=position >= UPCOMING_CARD_LIMIT)
                     for position, entry in enumerate(entries))


def render_state_legend() -> str:
    return "\n".join(f"<dt>{render_state_badge(state)}</dt><dd>{escape(text.meaning)}</dd>"
                     for state, text in STATE_TEXTS.items())


def render_labels_script() -> str:
    """Labels app.js reads: state and scope names, and the words for an entry whose end date passes."""
    labels = {
        "stateLabels": {state.value: text.label for state, text in STATE_TEXTS.items()},
        "scopeLabels": {scope.value: text.filter_label for scope, text in SCOPE_TEXTS.items()},
        "endStates": {"passed": State.SHUT_DOWN.value, "ahead": State.END_DATE_SET.value},
        "endDateWords": {"passed": END_DATE_WORDS[State.SHUT_DOWN], "ahead": END_DATE_WORDS[State.END_DATE_SET]},
    }
    # "</" inside a script element would end it early; JSON allows the escaped form.
    labels_json = json.dumps(labels, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="{LABELS_SCRIPT_ID}">{labels_json}</script>'


def render_version_item(version: Version, day: date) -> str:
    note = f'<span class="version-note">{escape(version.note)}</span>' if version.note else ""
    return (f'<li class="version-item" id="{escape(version.slug)}" data-end-date="{version.ends.isoformat()}">'
            f'<span class="cell-date"><span class="date-label">{END_DATE_WORDS[state_of_end(version.ends, day)]}</span>'
            f'{render_time(version.ends)}</span>'
            f'<span class="version-name">{escape(version.name)}{note}</span>'
            f'<span class="version-replacement">{render_move_to(version.replacement)}</span>'
            f'{render_page_link(version.source, "AWS page", version.name)}</li>')


def versions_by_date(register: Register) -> list[Version]:
    """Newest end date first, like the tables; names A to Z inside one date."""
    by_name = sorted(register.versions, key=lambda version: version.name.lower())
    return sorted(by_name, key=lambda version: version.ends, reverse=True)


def render_versions_section(register: Register) -> str:
    """The whole section, or nothing when the data holds no versions."""
    if not register.versions:
        return ""
    items = "\n".join(render_version_item(version, register.updated) for version in versions_by_date(register))
    return (f'<section class="versions" aria-labelledby="{VERSIONS_TITLE_ID}">\n'
            f'<h2 id="{VERSIONS_TITLE_ID}">{VERSIONS_TITLE}</h2>\n'
            f'<p class="section-intro">{escape(VERSIONS_INTRO)}</p>\n'
            f'<ol class="version-list">\n{items}\n</ol>\n</section>')


def render_page_meta(register: Register) -> str:
    return f"Updated {render_time(register.updated)} · {len(register.entries)} entries"


def render_license_link(register: Register) -> str:
    return f'<a href="{escape(register.repository_url)}/blob/main/LICENSE">MIT license</a>'


def render_issue_link(register: Register) -> str:
    return f'<a href="{escape(register.repository_url)}/issues">Open an issue</a>'


# ---------- Markdown ----------

def escape_markdown_cell(text: str) -> str:
    """Text shown as is: one line, and no character Markdown or HTML would read as a link, tag or emphasis."""
    return MARKDOWN_SPECIAL_PATTERN.sub(r"\\\1", " ".join(text.split()))


def render_markdown_row(entry: Entry, day: date) -> str:
    key_date = key_date_of(entry)
    tag = SCOPE_TEXTS[entry.scope].tag
    scope_tag = f" *({tag})*" if tag else ""
    cells = [escape_markdown_cell(entry.name) + scope_tag, escape_markdown_cell(entry.what),
             f"{date_words(key_date, state_on(entry, day))} {format_day(key_date.day)}",
             escape_markdown_cell(entry.replacement or NO_REPLACEMENT_TEXT), f"[AWS]({entry.source})"]
    return "| " + " | ".join(cells) + " |"


def render_markdown_table(entries: list[Entry], day: date) -> str:
    header = ["| Service | What it did | Date | Move to | Source |", "|---|---|---|---|---|"]
    return "\n".join(header + [render_markdown_row(entry, day) for entry in entries])


def render_markdown_section(title: str, entries: list[Entry], day: date, intro: str = "") -> list[str]:
    if not entries:
        return []
    return [title, "", *([intro, ""] if intro else []), render_markdown_table(entries, day), ""]


def render_readme_body(register: Register) -> str:
    day, ordered = register.updated, newest_first(register.entries)
    by_state = {state: [entry for entry in ordered if state_on(entry, day) is state] for state in State}
    labels = {state: text.label for state, text in STATE_TEXTS.items()}
    parts = render_markdown_section(f"## {labels[State.END_DATE_SET]}", by_state[State.END_DATE_SET], day)
    shut_down = by_state[State.SHUT_DOWN]
    for year in sorted({known(entry.ends).year for entry in shut_down}, reverse=True):
        period = str(year) if year >= FIRST_FULL_YEAR else f"late {year}"
        parts += render_markdown_section(f"## {labels[State.SHUT_DOWN]} in {period}",
                                         [entry for entry in shut_down if known(entry.ends).year == year], day)
    parts += render_markdown_section(f"## {labels[State.NO_NEW_CUSTOMERS]}", by_state[State.NO_NEW_CUSTOMERS], day,
                                     intro=STATE_TEXTS[State.NO_NEW_CUSTOMERS].meaning)
    changed = [entry for entry in ordered if state_on(entry, day) in (State.RENAMED, State.REVERSED)]
    changed_titles = [labels[state] for state in (State.RENAMED, State.REVERSED) if by_state[state]]
    changed_title = " or ".join([changed_titles[0], *(title.lower() for title in changed_titles[1:])]
                                if changed_titles else [])
    parts += render_markdown_section(f"## {changed_title}", changed, day)
    return "\n".join(parts).rstrip() + "\n"


def render_readme_versions(register: Register) -> str:
    """The section after a blank line, or nothing when the data holds no versions."""
    if not register.versions:
        return ""
    lines = ["", f"## {VERSIONS_TITLE}", "", VERSIONS_INTRO, "",
             "| Version | Date | Move to | Source |", "|---|---|---|---|"]
    for version in versions_by_date(register):
        words = END_DATE_WORDS[state_of_end(version.ends, register.updated)]
        lines.append(f"| {escape_markdown_cell(version.name)} | {words} {format_day(version.ends)} | "
                     f"{escape_markdown_cell(version.replacement)} | [AWS]({version.source}) |")
    return "\n".join(lines) + "\n"


def render_readme_meta(register: Register) -> str:
    return f"**Updated {format_day(register.updated)}** · {len(register.entries)} entries"


def render_site_link(register: Register) -> str:
    # The address itself is the link text, so a reader sees and can copy it; the [text](url) form keeps it in
    # the link check, which a bare <url> would escape.
    return f"Search and filter it on the web: [{register.site_url}]({register.site_url})"


# ---------- files ----------

def replace_block(text: str, block_name: str, content: str, file_name: str) -> str:
    pattern = re.compile(rf"(<!-- register:{block_name} -->)(.*?)(<!-- /register:{block_name} -->)", re.DOTALL)
    if len(pattern.findall(text)) != 1:
        raise DataFileError(f"{file_name}: needs exactly one <!-- register:{block_name} --> block")
    return pattern.sub(lambda match: match.group(1) + content + match.group(3), text)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise DataFileError(f"{path.name}: {error}") from error


def render_page(register: Register) -> str:
    blocks = {
        "labels": render_labels_script(),
        "meta": render_page_meta(register),
        "upcoming": "\n" + render_upcoming_cards(register) + "\n",
        "rows": "\n" + render_rows(register) + "\n",
        "legend": "\n" + render_state_legend() + "\n",
        "issue-link": render_issue_link(register),
        "license-link": render_license_link(register),
        "versions": render_versions_section(register),
    }
    page = read_text(PAGE_PATH)
    for block_name, content in blocks.items():
        page = replace_block(page, block_name, content, PAGE_PATH.name)
    return page


def render_readme(register: Register) -> str:
    blocks = {
        # Markers sit on lines of their own: a line that starts with "<!--" is raw HTML in Markdown, so
        # anything after the marker on that line would show its asterisks and brackets literally.
        "summary": f"\n{render_readme_meta(register)} · {render_site_link(register)}\n",
        "body": "\n" + render_readme_body(register) + "\n",
        "versions": render_readme_versions(register),
    }
    readme = read_text(README_PATH)
    for block_name, content in blocks.items():
        readme = replace_block(readme, block_name, content, README_PATH.name)
    return readme


def render_files(register: Register) -> RenderedFiles:
    return RenderedFiles(render_page(register), render_readme(register))


def find_stale_files(rendered: RenderedFiles) -> list[str]:
    targets = ((PAGE_PATH, rendered.page), (README_PATH, rendered.readme))
    return [str(path.relative_to(ROOT)) for path, text in targets if read_text(path) != text]


# ---------- links ----------

def collect_links(register: Register, rendered: RenderedFiles) -> list[str]:
    """Every http(s) link a reader can follow: the data's, and the hand-written ones in both files."""
    urls = {url for entry in register.entries for url in (entry.source, entry.guide, *entry.registry) if url}
    urls |= {version.source for version in register.versions}
    urls |= {html.unescape(url) for url in HTML_LINK_PATTERN.findall(rendered.page)}
    urls |= set(MARKDOWN_LINK_PATTERN.findall(rendered.readme))
    return sorted(urls)


def open_link_once(url: str) -> LinkResult:
    request = urllib.request.Request(url, headers={"User-Agent": LINK_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=LINK_TIMEOUT_SECONDS) as response:
            return LinkResult(url, str(response.status), response.geturl())
    except urllib.error.HTTPError as error:
        return LinkResult(url, str(error.code), url)
    except (urllib.error.URLError, OSError) as error:
        return LinkResult(url, f"unreachable ({getattr(error, 'reason', error)})", url)


def is_worth_retrying(result: LinkResult) -> bool:
    return result.status.startswith("unreachable") or result.status in RETRIED_STATUSES


def open_link(url: str) -> LinkResult:
    """Open the link, retrying a dropped connection or throttling; the last attempt's result stands."""
    for attempt in range(1, LINK_ATTEMPTS):
        result = open_link_once(url)
        if not is_worth_retrying(result):
            return result
        time.sleep(LINK_RETRY_PAUSE_SECONDS * attempt)
    return open_link_once(url)


def is_moved_to_index(result: LinkResult) -> bool:
    """AWS answers a removed page with 200 after redirecting it to the guide's front page.

    A path that ends in ".html" and one that ends in "/" always differ, so no third condition is needed.
    """
    original_path, final_path = urlparse(result.url).path, urlparse(result.final_url).path
    return original_path.endswith(".html") and final_path.endswith("/")


def check_links(urls: list[str]) -> int:
    with concurrent.futures.ThreadPoolExecutor(max_workers=LINK_WORKERS) as pool:
        results = list(pool.map(open_link, urls))
    failed = [result for result in results if result.status != "200" or is_moved_to_index(result)]
    for result in results:
        if result.status != "200":
            print(f"link {result.status}: {result.url}")
        elif is_moved_to_index(result):
            print(f"link removed, redirected to the guide index: {result.url} -> {result.final_url}")
        elif result.final_url != result.url:
            print(f"link moved: {result.url} -> {result.final_url}")
    print(f"links: {len(results)} checked, {len(failed)} failed")
    return 1 if failed else 0


# ---------- commands ----------

def print_summary(register: Register) -> None:
    print(f"entries: {len(register.entries)}")
    for state in State:
        count = sum(1 for entry in register.entries if state_on(entry, register.updated) is state)
        print(f"state {state}: {count}")
    print(f"no-replacement: {sum(1 for entry in register.entries if not entry.replacement)}")
    print(f"to-verify: {sum(1 for entry in register.entries if entry.verify)}")


def load_register() -> Register | None:
    """Read and check the data; print errors and return None when the data is not fit to render."""
    settings, tables, version_tables = read_data(DATA_PATH)
    errors = find_data_errors(settings, tables, version_tables, read_text(PAGE_PATH))
    for error in errors:
        print(f"error {error}", file=sys.stderr)
    if errors:
        return None
    entries = tuple(Entry.from_table(table) for table in tables)
    versions = tuple(Version(**table) for table in version_tables)
    return Register(settings["updated"], settings["repository_url"], settings["site_url"], entries, versions)


def write_files(rendered: RenderedFiles) -> int:
    PAGE_PATH.write_text(rendered.page, encoding="utf-8")
    README_PATH.write_text(rendered.readme, encoding="utf-8")
    print(f"wrote: {PAGE_PATH.relative_to(ROOT)}")
    print(f"wrote: {README_PATH.relative_to(ROOT)}")
    return 0


def check_files(register: Register, rendered: RenderedFiles, is_release: bool) -> int:
    stale = find_stale_files(rendered)
    for path in stale:
        print(f"stale: {path} (run: python3 -I build.py write)")
    unverified = [entry.slug for entry in register.entries if entry.verify] if is_release else []
    if unverified:
        print(f"unverified: {', '.join(unverified)}")
    return 1 if stale or unverified else 0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the register and render it into the page and the README.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("write", help="check the data, then rewrite the generated blocks")
    commands.add_parser("check", help="check the data and fail if a generated block is out of date")
    commands.add_parser("release", help="as check, and fail while any entry has `verify`")
    commands.add_parser("links", help="open every link and fail on any that does not answer 200 (needs network)")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    try:
        register = load_register()
        if register is None:
            return 1
        print_summary(register)
        rendered = render_files(register)
        if arguments.command == "write":
            return write_files(rendered)
        if arguments.command == "links":
            return check_links(collect_links(register, rendered))
        return check_files(register, rendered, is_release=arguments.command == "release")
    except DataFileError as error:
        print(f"error {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
