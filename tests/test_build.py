"""Tests for build.py: every data check fails on a planted defect and passes the real register.

Run from the repository root:
    python3 -I -m unittest discover --start-directory tests --top-level-directory .

Exit code 0 means every test passed. The tests read the real data and page but write nothing and
open no network connection.
"""
import ast
import re
import unittest
from datetime import date, datetime
from typing import ClassVar
from unittest import mock

import build

VALID_TABLE = {
    "slug": "example-service",
    "name": "Amazon Example",
    "status": "sunset",
    "group": "Storage",
    "scope": "service",
    "what": "Stored example objects for tests",
    "announced": date(2026, 1, 1),
    "ends": date(2027, 1, 1),
    "replacement": "Amazon S3",
    "source": "https://docs.aws.amazon.com/example/latest/userguide/end-of-support.html",
}
VALID_SETTINGS = {
    "updated": date(2026, 11, 2),
    "repository_url": "https://github.com/example/register",
    "site_url": "https://example.github.io/register/",
}

# Each case: what to change in VALID_TABLE (None removes the field), and a phrase the error must hold.
PLANTED_DEFECTS = {
    "missing source": ({"source": None}, "missing source"),
    "unknown field": ({"colour": "red"}, "unknown field colour"),
    "slug not kebab-case": ({"slug": "Example_Service"}, "slug is not kebab-case"),
    "unknown status": ({"status": "retired"}, "status 'retired'"),
    "unknown group": ({"group": "Toys"}, "group 'Toys'"),
    "unknown scope": ({"scope": "planet"}, "scope 'planet'"),
    "date written as text": ({"ends": "2027-01-01"}, "ends is not a date"),
    "sunset without end": ({"ends": None}, "sunset needs ends"),
    "rename without day": ({"status": "renamed", "announced": None, "ends": None}, "renamed needs announced"),
    "maintenance without date": ({"status": "maintenance", "announced": None, "ends": None}, "maintenance needs"),
    "announced after end": ({"announced": date(2028, 1, 1)}, "announced is after ends"),
    "closed to new after end": ({"closed_to_new": date(2027, 6, 1)}, "closed_to_new is after ends"),
    "closed to new before announced": ({"closed_to_new": date(2025, 6, 1)}, "announced is after closed_to_new"),
    "renamed with an end": ({"status": "renamed"}, "renamed takes no ends"),
    "reversed with an end": ({"status": "reversed"}, "reversed takes no ends"),
    "what too long": ({"what": "word " * 21}, "the limit is 20"),
    "what ends with period": ({"what": "Stored example objects."}, "ends with a period"),
    "line break in what": ({"what": "Stored example\nobjects"}, "what has a line break"),
    "line break in name": ({"name": "Amazon\r\nExample"}, "name has a line break"),
    "vague replacement": ({"replacement": "Another storage tool"}, "names no product"),
    "banned word": ({"what": "Seamless storage for example objects"}, "banned word 'seamless'"),
    "link outside AWS": ({"source": "https://example.com/page"}, "not a plain https AWS page"),
    "plain http link": ({"guide": "http://docs.aws.amazon.com/guide"}, "not a plain https AWS page"),
    "registry outside AWS": ({"registry": ["https://example.com/x"]}, "registry is not a plain https AWS page"),
    "backslash before the host": ({"source": "https://evil.example\\@docs.aws.amazon.com/x.html"}, "source is not"),
    "user part before the host": ({"source": "https://user@docs.aws.amazon.com/x.html"}, "source is not"),
    "parenthesis in a link": ({"guide": "https://docs.aws.amazon.com/a) [x](https://evil.example"}, "guide is not"),
    "space in a link": ({"source": "https://docs.aws.amazon.com/a b.html"}, "source is not"),
    "unclosed bracket in a host": ({"source": "https://[docs.aws.amazon.com/x.html"}, "source is not"),
    "slug as a number": ({"slug": 5}, "slug is not text"),
    "what as a number": ({"what": 7}, "what is not text"),
    "name as a list": ({"name": ["Amazon Example"]}, "name is not text"),
    "note as a boolean": ({"note": True}, "note is not text"),
    "status as a list": ({"status": ["sunset"]}, "status is not text"),
    "registry as text": ({"registry": "https://docs.aws.amazon.com/x"}, "registry is not a list"),
    "registry holding a number": ({"registry": [5]}, "registry is not a list"),
    "end as a date-time": ({"ends": datetime(2027, 1, 1, 12, 0)}, "ends is not a date"),
}


def planted(change: dict) -> dict:
    table = dict(VALID_TABLE)
    for field, value in change.items():
        if value is None:
            table.pop(field, None)
        else:
            table[field] = value
    return table


def example_register(*tables: dict) -> build.Register:
    entries = tuple(build.Entry.from_table(table) for table in tables)
    return build.Register(VALID_SETTINGS["updated"], VALID_SETTINGS["repository_url"], VALID_SETTINGS["site_url"],
                          entries)


class DataChecksTest(unittest.TestCase):
    def test_valid_table_has_no_errors(self) -> None:
        self.assertEqual(build.find_register_errors([dict(VALID_TABLE)]), [])

    def test_each_planted_defect_is_caught(self) -> None:
        for case, (change, expected_phrase) in PLANTED_DEFECTS.items():
            with self.subTest(case=case):
                errors = build.find_register_errors([planted(change)])
                self.assertTrue(any(expected_phrase in error for error in errors), errors)

    def test_wrong_type_skips_the_checks_that_would_crash(self) -> None:
        errors = build.find_register_errors([planted({"slug": 5, "what": 7})])
        self.assertEqual(errors, ["entry 1: slug is not text", "entry 1: what is not text"])

    def test_duplicates_are_caught_once(self) -> None:
        errors = build.find_register_errors([dict(VALID_TABLE), dict(VALID_TABLE)])
        self.assertIn("example-service: duplicate slug", errors)
        self.assertIn("Amazon Example: duplicate name", errors)
        self.assertFalse(any(error.startswith("None") for error in build.find_duplicate_errors([{}, {}])))
        self.assertEqual(build.find_duplicate_errors([{"name": ["a"]}, {"name": ["a"]}]), [])

    def test_register_needs_an_entry(self) -> None:
        self.assertEqual(build.find_register_errors([]), ["data: add at least one [[entry]] table"])

    def test_each_settings_defect_is_caught(self) -> None:
        self.assertEqual(build.find_settings_errors(VALID_SETTINGS), [])
        cases = {"updated": ({"updated": "2026-11-02"}, "set `updated"),
                 "repository_url": ({"repository_url": "http://github.com/x"}, "repository_url must be"),
                 "site_url": ({"site_url": "https://evil.example\\@github.io/"}, "site_url must be"),
                 "url as a number": ({"site_url": 5}, "site_url must be"),
                 "unknown key": ({"versions": [{}]}, "settings: unknown key versions")}
        for case, (change, phrase) in cases.items():
            with self.subTest(case=case):
                errors = build.find_settings_errors(dict(VALID_SETTINGS, **change))
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(phrase, errors[0])

    def test_entries_must_be_an_array_of_tables(self) -> None:
        data = dict(VALID_SETTINGS, entry=[dict(VALID_TABLE)])
        self.assertEqual(build.split_data(data, "register.toml"), (VALID_SETTINGS, [VALID_TABLE], []))
        with self.assertRaises(build.DataFileError):
            build.split_data(dict(VALID_SETTINGS, entry=dict(VALID_TABLE)), "register.toml")

    def test_slug_may_not_be_an_id_of_the_page(self) -> None:
        page = ('<div id="toolbar"></div>\n<!-- register:rows --><tr id="generated-row"></tr>'
                "<!-- /register:rows -->")
        tables = [planted({"slug": slug}) for slug in ("toolbar", "generated-row", build.LABELS_SCRIPT_ID)]
        self.assertEqual(build.find_reserved_slug_errors(tables, page),
                         ["register-labels: slug is an id the page already uses",
                          "toolbar: slug is an id the page already uses"])

    def test_real_register_has_no_errors(self) -> None:
        settings, tables, versions = build.read_data(build.DATA_PATH)
        self.assertEqual(build.find_data_errors(settings, tables, versions, build.read_text(build.PAGE_PATH)), [])

    def test_every_state_and_scope_has_its_text(self) -> None:
        self.assertEqual(set(build.STATE_TEXTS), set(build.State))
        self.assertEqual(set(build.SCOPE_TEXTS), set(build.Scope))


class VersionChecksTest(unittest.TestCase):
    VALID_VERSION: ClassVar[dict[str, object]] = {
        "slug": "example-runtime", "name": "Example runtime 1", "ends": date(2027, 1, 1),
        "replacement": "Example runtime 2", "source": "https://docs.aws.amazon.com/example/runtimes.html",
    }

    def test_valid_version_has_no_errors(self) -> None:
        self.assertEqual(build.find_version_errors([dict(self.VALID_VERSION)]), [])

    def test_planted_version_defects_are_caught(self) -> None:
        cases = {"missing ends": ({"ends": None}, "missing ends"),
                 "date as text": ({"ends": "2027-01-01"}, "ends is not a date"),
                 "link outside AWS": ({"source": "https://example.com/x"}, "not a plain https AWS page"),
                 "unknown field": ({"status": "sunset"}, "unknown field status"),
                 "slug not kebab-case": ({"slug": "Example_Runtime"}, "slug is not kebab-case"),
                 "vague replacement": ({"replacement": "Another runtime"}, "names no product"),
                 "banned word": ({"note": "A robust runtime"}, "banned word 'robust'"),
                 "line break": ({"name": "Example\nruntime 1"}, "name has a line break"),
                 "name as a number": ({"name": 1}, "name is not text")}
        for case, (change, phrase) in cases.items():
            with self.subTest(case=case):
                version = {key: value for key, value in {**self.VALID_VERSION, **change}.items() if value is not None}
                errors = build.find_version_errors([version])
                self.assertTrue(any(phrase in error for error in errors), errors)

    def test_duplicate_versions_are_caught(self) -> None:
        errors = build.find_version_errors([dict(self.VALID_VERSION), dict(self.VALID_VERSION)])
        self.assertEqual(errors, ["example-runtime: duplicate slug", "Example runtime 1: duplicate name"])

    def test_entry_and_version_may_not_share_a_slug(self) -> None:
        errors = build.find_shared_slug_errors([dict(VALID_TABLE)], [dict(self.VALID_VERSION, slug="example-service")])
        self.assertEqual(errors, ["example-service: slug used by an entry and a version"])
        version = build.Version(**self.VALID_VERSION)
        self.assertIn('id="example-runtime"', build.render_version_item(version, VALID_SETTINGS["updated"]))

    def test_version_section_shows_ended_and_upcoming(self) -> None:
        ended = build.Version("old-runtime", "Old runtime", date(2026, 1, 1), "New runtime",
                              self.VALID_VERSION["source"])
        ahead = build.Version("next-runtime", "Next runtime", date(2027, 1, 1), "New runtime",
                              self.VALID_VERSION["source"])
        register = build.Register(VALID_SETTINGS["updated"], VALID_SETTINGS["repository_url"],
                                  VALID_SETTINGS["site_url"], (), (ahead, ended))
        section = build.render_versions_section(register)
        self.assertLess(section.index("Old runtime"), section.index("Next runtime"))
        self.assertIn('<span class="date-label">Ended</span>', section)
        self.assertIn('<span class="date-label">Ends</span>', section)
        empty_register = build.Register(date(2026, 1, 1), "https://a.example", "https://b.example", ())
        self.assertEqual(build.render_versions_section(empty_register), "")


class StatesAndOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.entry = build.Entry.from_table(VALID_TABLE)

    def test_state_turns_on_the_end_date(self) -> None:
        # The same rule as hasEnded() in site/js/dates.js: shut down ON the end date.
        self.assertIs(build.state_on(self.entry, date(2026, 12, 31)), build.State.END_DATE_SET)
        self.assertIs(build.state_on(self.entry, date(2027, 1, 1)), build.State.SHUT_DOWN)

    def test_key_date_prefers_the_end(self) -> None:
        self.assertEqual(build.key_date_of(self.entry), build.KeyDate(date(2027, 1, 1), build.DateKind.END))

    def test_same_date_reads_a_to_z(self) -> None:
        later = build.Entry.from_table(dict(VALID_TABLE, slug="zeta", name="Zeta"))
        earlier = build.Entry.from_table(dict(VALID_TABLE, slug="alpha", name="Alpha"))
        names = [entry.name for entry in build.oldest_first((later, earlier))]
        self.assertEqual(names, ["Alpha", "Zeta"])
        upcoming_names = [entry.name for entry in build.upcoming((later, earlier), date(2026, 6, 1))]
        self.assertEqual(upcoming_names, ["Alpha", "Zeta"])


class RenderingTest(unittest.TestCase):
    def test_one_tbody_per_year_each_headed_by_its_year(self) -> None:
        register = example_register(VALID_TABLE, dict(VALID_TABLE, slug="older", name="Older", ends=date(2025, 5, 1),
                                                      announced=date(2024, 5, 1)))
        sections = re.findall(r'<tbody>\n<tr class="year-row"><th[^>]*>(\d{4})</th></tr>\n(.*?)\n</tbody>',
                              build.render_rows(register), re.DOTALL)
        self.assertEqual([year for year, _ in sections], ["2025", "2027"])
        self.assertTrue(all(rows.count('class="entry-row"') == 1 for _, rows in sections))

    def test_page_ids_are_unique(self) -> None:
        register = build.load_register()
        ids = re.findall(r"""\sid=["']([^"']+)["']""", build.render_files(register).page)
        self.assertEqual(len(ids), len(set(ids)), [identifier for identifier in ids if ids.count(identifier) > 1])

    def test_row_names_its_service_for_screen_readers(self) -> None:
        row = build.render_entry_row(build.Entry.from_table(dict(VALID_TABLE, guide=VALID_TABLE["source"])),
                                     VALID_SETTINGS["updated"])
        self.assertIn('<td class="cell-service"', row)
        self.assertNotIn("<th", row)  # <th> may not hold <details> (html-validate element-permitted-content)
        self.assertIn('AWS page<span class="visually-hidden"> for Amazon Example</span></a>', row)
        self.assertIn('guide<span class="visually-hidden"> for Amazon Example</span></a>', row)

    def test_cards_say_move_to_and_name_a_missing_replacement(self) -> None:
        named = build.Entry.from_table(VALID_TABLE)
        unnamed = build.Entry.from_table(dict(VALID_TABLE, slug="unnamed", name="Unnamed", replacement=""))
        cards = [build.render_upcoming_card(entry, is_hidden=False) for entry in (named, unnamed)]
        self.assertIn('<span class="visually-hidden">Move to: </span> Amazon S3', cards[0])
        self.assertIn(build.NO_REPLACEMENT_TEXT, cards[1])

    def test_cards_past_the_limit_start_hidden(self) -> None:
        tables = [dict(VALID_TABLE, slug=f"service-{number}", name=f"Service {number}")
                  for number in range(build.UPCOMING_CARD_LIMIT + 1)]
        cards = build.render_upcoming_cards(example_register(*tables)).splitlines()
        self.assertEqual([" hidden>" in card for card in cards], [False] * build.UPCOMING_CARD_LIMIT + [True])

    def test_labels_script_cannot_end_its_element(self) -> None:
        with mock.patch.dict(build.STATE_TEXTS, {build.State.RENAMED: build.StateText("</script>", "x")}):
            script = build.render_labels_script()
        self.assertEqual(script.count("</script>"), 1)

    def test_links_come_from_data_page_and_readme(self) -> None:
        register = example_register(dict(VALID_TABLE, guide="https://aws.amazon.com/guide"))
        rendered = build.RenderedFiles("<a href='https://a.example/x?y=1&amp;z=2'>a</a>", "[b](https://b.example/)")
        self.assertEqual(build.collect_links(register, rendered),
                         ["https://a.example/x?y=1&z=2", "https://aws.amazon.com/guide", "https://b.example/",
                          VALID_TABLE["source"]])


class MarkdownTest(unittest.TestCase):
    def test_cell_text_is_one_line_with_markup_escaped(self) -> None:
        text = "A | B [x](https://evil.example) <img src=x>\n*b* _c_ `d` \\"
        self.assertEqual(build.escape_markdown_cell(text),
                         r"A \| B \[x\](https://evil.example) \<img src=x\> \*b\* \_c\_ \`d\` \\")

    def test_readme_tables_read_down_in_time(self) -> None:
        """Every README table ends with its latest date; one date reads A to Z."""
        register = build.load_register()
        assert register is not None
        months = {name: number for number, name in enumerate(build.MONTH_ABBREVIATIONS, start=1)}
        for section in build.render_readme_body(register).split("\n## ")[1:]:
            cells = re.findall(r"\| [A-Z][a-z ]+ (\d{1,2}) ([A-Z][a-z]{2}) (\d{4}) \|", section)
            days = [date(int(year), months[month], int(day)) for day, month, year in cells]
            with self.subTest(section=section.split("\n")[0]):
                self.assertEqual(days, sorted(days))

    def test_readme_sections_titles_and_escaping(self) -> None:
        register = example_register(
            dict(VALID_TABLE, name="Amazon A | B", replacement=""),
            dict(VALID_TABLE, slug="ended", name="Ended", ends=date(2025, 3, 1), announced=date(2024, 1, 1)),
            dict(VALID_TABLE, slug="late", name="Late", ends=date(2024, 12, 1), announced=date(2024, 1, 1)),
            planted({"slug": "kept", "name": "Kept", "status": "maintenance", "ends": None}),
            planted({"slug": "moved", "name": "Moved", "status": "renamed", "ends": None}))
        body = build.render_readme_body(register)
        titles = [line for line in body.splitlines() if line.startswith("## ")]
        self.assertEqual(titles, ["## End date set", "## Shut down in 2025", "## Shut down in late 2024",
                                  "## No new customers", "## Renamed"])
        both = example_register(planted({"slug": "moved", "name": "Moved", "status": "renamed", "ends": None}),
                                planted({"slug": "back", "name": "Back", "status": "reversed", "ends": None}))
        self.assertIn("## Renamed or reversed", build.render_readme_body(both))
        self.assertIn(f"## No new customers\n\n{build.STATE_TEXTS[build.State.NO_NEW_CUSTOMERS].meaning}\n", body)
        self.assertIn(f"| Amazon A \\| B | Stored example objects for tests | Ends 1 Jan 2027 | "
                      f"{build.NO_REPLACEMENT_TEXT} |", body)


class LinkCheckTest(unittest.TestCase):
    def test_each_condition_of_a_removed_page(self) -> None:
        cases = {"html page sent to its guide index": ("/sns/latest/dg/gone.html", "/sns/latest/dg/", True),
                 "page without .html sent to a folder": ("/chime/", "/chime/getting-started/", False),
                 "html page sent to another html page": ("/sns/latest/dg/old.html", "/sns/latest/dg/new.html", False),
                 "html page not redirected": ("/sns/latest/dg/page.html", "/sns/latest/dg/page.html", False)}
        for case, (original_path, final_path, is_removed) in cases.items():
            with self.subTest(case=case):
                result = build.LinkResult(f"https://docs.aws.amazon.com{original_path}", "200",
                                          f"https://docs.aws.amazon.com{final_path}")
                self.assertIs(build.is_moved_to_index(result), is_removed)

    def test_throttling_and_dropped_connections_are_retried(self) -> None:
        url = "https://docs.aws.amazon.com/x.html"
        cases = {"throttled, then ok": (["429", "503", "200"], "200", 3),
                 "unreachable every time": (["unreachable (reset)"] * 3, "unreachable (reset)", 3),
                 "not found": (["404"], "404", 1)}
        for case, (statuses, final_status, attempts) in cases.items():
            results = [build.LinkResult(url, status, url) for status in statuses]
            with self.subTest(case=case), mock.patch.object(build.time, "sleep"), \
                    mock.patch.object(build, "open_link_once", side_effect=results) as opener:
                self.assertEqual(build.open_link(url).status, final_status)
                self.assertEqual(opener.call_count, attempts)


def find_clock_reads(source: str) -> list[str]:
    """Calls that read the clock; time.sleep, the link checker's pause, is the one allowed use of `time`."""
    clock_attributes = {"today", "now", "utcnow", "fromtimestamp"}
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        if node.attr in clock_attributes or (node.value.id == "time" and node.attr != "sleep"):
            found.append(f"{node.value.id}.{node.attr} on line {node.lineno}")
    return found


class FilesTest(unittest.TestCase):
    def test_missing_marker_is_an_input_error(self) -> None:
        with self.assertRaises(build.DataFileError):
            build.replace_block("no markers here", "rows", "x", "index.html")

    def test_stale_page_is_caught(self) -> None:
        register = build.load_register()
        self.assertIsNotNone(register)
        rendered = build.render_files(register)
        changed = build.RenderedFiles(rendered.page + "<!-- planted -->", rendered.readme)
        self.assertIn("site/index.html", build.find_stale_files(changed))

    def test_rendering_twice_gives_the_same_bytes(self) -> None:
        register = build.load_register()
        self.assertEqual(build.render_files(register), build.render_files(register))

    def test_build_never_reads_the_clock(self) -> None:
        # Two renders in one run share the clock, so only reading the source can show a dependence on it.
        self.assertEqual(find_clock_reads(build.ROOT.joinpath("build.py").read_text(encoding="utf-8")), [])
        planted_source = "import time\nfrom datetime import date\nday = date.today()\nstarted = time.time()\n"
        self.assertEqual(find_clock_reads(planted_source), ["date.today on line 3", "time.time on line 4"])


if __name__ == "__main__":
    unittest.main()
