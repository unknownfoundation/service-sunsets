// Tests for site/js/dates.js, the calendar arithmetic behind "in N days" and "End date set" -> "Shut down".
//
// Run from the repository root:
//     node --test tests/
//
// dates.js is a browser script; it runs here in a fresh VM context, whose globalThis receives RegisterDates.
// The checks that depend on the time zone run in child processes, one per zone in ZONES, because a process
// reads TZ once: in UTC alone, the usual zone of a build machine, both planted defects below would pass.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { isDeepStrictEqual } from "node:util";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../site/js/dates.js", import.meta.url), "utf8");

// No shift, a European and a US daylight-saving change, and a zone ahead of UTC without one.
const ZONES = ["UTC", "Europe/Berlin", "America/Los_Angeles", "Asia/Tokyo"];
// Each pair is two calendar days apart.
const TWO_DAY_SPANS = [
  ["2026-03-28", "2026-03-30"], // Europe moves clocks forward on 29 Mar 2026
  ["2026-10-24", "2026-10-26"], // and back on 25 Oct 2026
  ["2026-03-07", "2026-03-09"], // the US moves clocks forward on 8 Mar 2026
  ["2026-10-31", "2026-11-02"], // and back on 1 Nov 2026
  ["2028-02-28", "2028-03-01"], // 2028 is a leap year
];
// Local times a minute from midnight, as [year, month index, day, hour, minute], and the day each falls on.
const LOCAL_TIMES = [[2026, 0, 5, 23, 59], [2026, 11, 31, 0, 0]];
const EXPECTED = { daysAcrossSpans: TWO_DAY_SPANS.map(() => 2), localDays: ["2026-01-05", "2026-12-31"] };

// Runs in the child: reads dates.js from stdin, prints what it computes for the spans and local times.
const CHILD_SCRIPT = `
const { runInNewContext } = require("node:vm");
const context = {};
runInNewContext(require("node:fs").readFileSync(0, "utf8"), context);
const { daysBetween, localIsoDate } = context.RegisterDates;
const { spans, localTimes } = JSON.parse(process.argv[1]);
console.log(JSON.stringify({
  daysAcrossSpans: spans.map(([from, to]) => daysBetween(from, to)),
  localDays: localTimes.map((parts) => localIsoDate(new Date(...parts))),
}));
`;

/** @param {string} zone @param {string} source */
function computeInZone(zone, source) {
  const cases = JSON.stringify({ spans: TWO_DAY_SPANS, localTimes: LOCAL_TIMES });
  const output = execFileSync(process.execPath, ["-e", CHILD_SCRIPT, cases], {
    input: source,
    env: { ...process.env, TZ: zone },
    encoding: "utf8",
  });
  return JSON.parse(output);
}

/** @param {string} source @returns {string[]} the zones where the source computes a wrong answer */
function zonesGettingItWrong(source) {
  return ZONES.filter((zone) => !isDeepStrictEqual(computeInZone(zone, source), EXPECTED));
}

/** @param {string} original @param {string} replacement @returns {string} dates.js with one line changed */
function plantDefect(original, replacement) {
  assert.equal(SOURCE.split(original).length, 2, `the line to change must occur once: ${original}`);
  return SOURCE.replace(original, replacement);
}

/** @param {string} source */
function loadDates(source) {
  const context = {};
  vm.runInNewContext(source, context);
  return context.RegisterDates;
}

const dates = loadDates(SOURCE);

test("counts whole days, as the page shows them on its build day", () => {
  assert.equal(dates.daysBetween("2026-11-02", "2026-12-31"), 59);
  assert.equal(dates.daysBetween("2026-11-02", "2027-03-31"), 149);
  assert.equal(dates.daysBetween("2026-11-02", "2027-09-29"), 331);
});

for (const zone of ZONES) {
  test(`in ${zone}, a clock change or a leap day does not shift the count, and today is the local day`, () => {
    assert.deepEqual(computeInZone(zone, SOURCE), EXPECTED);
  });
}

test("planted defect: days counted from local midnight and rounded down are caught", () => {
  const mutant = plantDefect(
    "return Math.round((Date.parse(toIsoDate) - Date.parse(fromIsoDate)) / MILLISECONDS_PER_DAY);",
    "return Math.floor((Date.parse(`${toIsoDate}T00:00`) - Date.parse(`${fromIsoDate}T00:00`))"
      + " / MILLISECONDS_PER_DAY);",
  );
  assert.deepEqual(zonesGettingItWrong(mutant), ["Europe/Berlin", "America/Los_Angeles"]);
});

test("planted defect: today taken as the UTC day is caught near local midnight", () => {
  const mutant = plantDefect(
    "function localIsoDate(now = new Date()) {",
    "function localIsoDate(now = new Date()) {\n    return now.toISOString().slice(0, 10);",
  );
  // Los Angeles at 23:59 is already the next day in UTC; Berlin and Tokyo at 00:00 are still the day before.
  assert.deepEqual(zonesGettingItWrong(mutant), ["Europe/Berlin", "America/Los_Angeles", "Asia/Tokyo"]);
});

test("an entry is shut down on its end date, not the day after, as in build.py", () => {
  assert.equal(dates.hasEnded("2026-12-31", "2026-12-30"), false);
  assert.equal(dates.hasEnded("2026-12-31", "2026-12-31"), true);
  assert.equal(dates.hasEnded("2026-12-31", "2027-01-01"), true);
});

test("days left read as people say them", () => {
  assert.equal(dates.describeDaysLeft(1), "tomorrow");
  assert.equal(dates.describeDaysLeft(2), "in 2 days");
  assert.equal(dates.describeDaysLeft(331), "in 331 days");
});

test("planted defect: moving the end-date rule by one day is caught", () => {
  const mutant = loadDates(plantDefect("return endDate <= today;", "return endDate < today;"));
  assert.notEqual(mutant.hasEnded("2026-12-31", "2026-12-31"), dates.hasEnded("2026-12-31", "2026-12-31"));
});
