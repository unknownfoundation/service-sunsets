// @ts-check
// Calendar arithmetic for the register page: plain functions that never touch the page, so
// tests/dates.test.mjs runs them in Node. app.js reads them from globalThis.RegisterDates.
//
// Dates in the data are calendar days, written YYYY-MM-DD, with no time and no time zone. Two rules
// follow, and build.py keeps the same ones:
// - an entry counts as shut down ON its end date, not the day after (state_on in build.py);
// - "today" is the reader's local calendar day, because that is the day they live in.
"use strict";

{
  const MILLISECONDS_PER_DAY = 86_400_000;

  /**
   * @param {Date} [now]
   * @returns {string} the local calendar day as YYYY-MM-DD, comparable as text with the data's dates
   */
  function localIsoDate(now = new Date()) {
    const month = String(now.getMonth() + 1).padStart(2, "0");
    const day = String(now.getDate()).padStart(2, "0");
    return `${now.getFullYear()}-${month}-${day}`;
  }

  /**
   * Whole days from one calendar day to another. Both parse as UTC midnight, so a daylight-saving
   * change between them cannot add or lose an hour and round the count the wrong way.
   * @param {string} fromIsoDate
   * @param {string} toIsoDate
   */
  function daysBetween(fromIsoDate, toIsoDate) {
    return Math.round((Date.parse(toIsoDate) - Date.parse(fromIsoDate)) / MILLISECONDS_PER_DAY);
  }

  /** @param {string} endDate @param {string} today */
  function hasEnded(endDate, today) {
    return endDate <= today;
  }

  /** @param {number} days  1 or more: an entry whose end date is today has already ended */
  function describeDaysLeft(days) {
    return days === 1 ? "tomorrow" : `in ${days} days`;
  }

  const registerDates = Object.freeze({ localIsoDate, daysBetween, hasEnded, describeDaysLeft });
  /** @type {any} */ (globalThis).RegisterDates = registerDates;
}
