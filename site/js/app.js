// @ts-check
// Search, filters, sorting and live dates for the register table that build.py renders.
//
// The page reads fine without this file. It is a classic script, not an ES module, so the page also
// works when opened straight from disk: browsers refuse module scripts on file:// URLs. The block
// around everything keeps its names out of the global scope.
//
// State and scope names are not written here: build.py puts them in the #register-labels JSON, so the
// table, the filters and the legend can never disagree. Calendar arithmetic lives in dates.js.
"use strict";

{
  const DEFAULT_SORT = "date-asc";
  const FILTER_NAMES = /** @type {const} */ (["query", "state", "year", "group", "scope"]);
  const SEARCH_DELAY_MILLISECONDS = 120;
  // The same query as the sticky rules in styles.css: below it the toolbar scrolls away with the page.
  const STICKY_TOOLBAR_QUERY = "(min-width: 760px) and (min-height: 600px)";
  const TOOLBAR_READY_CLASS = "is-ready"; // styles.css keeps the toolbar invisible until this script has run
  const JUMP_TARGET_CLASS = "is-jump-target"; // :target does not follow history.replaceState, so a class marks it
  // Hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar and minus: names mix them.
  const DASHES = /[\u2010-\u2015\u2212]/g;

  /**
   * @typedef {object} RegisterDatesApi
   * @property {(now?: Date) => string} localIsoDate
   * @property {(fromIsoDate: string, toIsoDate: string) => number} daysBetween
   * @property {(endDate: string, today: string) => boolean} hasEnded
   * @property {(days: number) => string} describeDaysLeft
   */
  /** @type {RegisterDatesApi} */
  const { localIsoDate, daysBetween, hasEnded, describeDaysLeft } = /** @type {any} */ (globalThis).RegisterDates;

  /**
   * @typedef {object} Labels
   * @property {Record<string, string>} stateLabels  state value -> label, in display order
   * @property {Record<string, string>} scopeLabels  scope value -> Kind filter label, in display order
   * @property {{passed: string, ahead: string}} endStates  states of an entry with an end date
   * @property {{passed: string, ahead: string}} endDateWords  "Ended" / "Ends"
   */

  /**
   * @typedef {object} View
   * @property {string} query
   * @property {string} state
   * @property {string} year
   * @property {string} group
   * @property {string} scope
   * @property {string} sort  "<field>-<direction>", such as "date-desc"
   */

  /**
   * @template {Element} ElementType
   * @param {ParentNode} parent
   * @param {string} selector
   * @param {new () => ElementType} elementType
   * @returns {ElementType}
   */
  function requireElement(parent, selector, elementType) {
    const element = parent.querySelector(selector);
    if (!(element instanceof elementType)) {
      throw new Error(`${selector} is missing or is not a ${elementType.name}`);
    }
    return element;
  }

  /**
   * Code-point order, the same as Python's sort in build.py, so the first render keeps the built order.
   * @param {string} first @param {string} second
   */
  function compareText(first, second) {
    if (first < second) return -1;
    return first > second ? 1 : 0;
  }

  /** @param {string} text @param {string} className */
  function createSpan(text, className) {
    const span = document.createElement("span");
    span.className = className;
    span.textContent = text;
    return span;
  }

  /**
   * @param {HTMLElement[]} elements
   * @param {(element: HTMLElement) => string} pickValue
   * @returns {Map<string, number>} how many elements carry each value
   */
  function countBy(elements, pickValue) {
    const counts = new Map();
    for (const element of elements) {
      const value = pickValue(element);
      counts.set(value, (counts.get(value) ?? 0) + 1);
    }
    return counts;
  }

  /**
   * Lower case, every dash as "-": "workspaces - pcoip" finds "Amazon WorkSpaces – PCoIP".
   * @param {string} text
   */
  function normalizeSearchText(text) {
    return text.toLowerCase().replace(DASHES, "-");
  }

  /** @returns {Partial<View>} every filter set to "", which shows all rows */
  function emptyFilters() {
    return Object.fromEntries(FILTER_NAMES.map((name) => [name, ""]));
  }

  /** @param {HTMLSelectElement} select @param {string} value */
  function isOffered(select, value) {
    return [...select.options].some((option) => option.value === value);
  }

  class RegisterPage {
    constructor() {
      const labelsScript = requireElement(document, "#register-labels", HTMLScriptElement);
      /** @type {Labels} */
      this.labels = JSON.parse(labelsScript.textContent ?? "{}");
      this.stateOrder = Object.keys(this.labels.stateLabels);
      this.today = localIsoDate();

      this.table = requireElement(document, "#register-table", HTMLTableElement);
      /** @type {HTMLTableRowElement[]} */
      this.rows = [...this.table.querySelectorAll("tbody > tr.entry-row")]
        .filter((row) => row instanceof HTMLTableRowElement);
      this.yearRowTemplate = requireElement(this.table, "tbody > tr.year-row", HTMLTableRowElement).cloneNode(true);

      this.searchInput = requireElement(document, "#search-input", HTMLInputElement);
      this.stateFilter = requireElement(document, "#state-filter", HTMLElement);
      this.yearFilter = requireElement(document, "#year-filter", HTMLSelectElement);
      this.groupFilter = requireElement(document, "#group-filter", HTMLSelectElement);
      this.scopeFilter = requireElement(document, "#scope-filter", HTMLSelectElement);
      this.sortOrder = requireElement(document, "#sort-order", HTMLSelectElement);
      this.clearButton = requireElement(document, "#clear-filters", HTMLButtonElement);
      this.clearButtonInEmptyState = requireElement(document, "#clear-filters-empty", HTMLButtonElement);
      this.resultCount = requireElement(document, "#result-count", HTMLElement);
      this.noResults = requireElement(document, "#no-results", HTMLElement);
      this.upcomingList = requireElement(document, "#upcoming-cards", HTMLOListElement);
      this.toolbar = requireElement(document, "#toolbar", HTMLElement);

      /** @type {View} */
      this.view = { query: "", state: "", year: "", group: "", scope: "", sort: DEFAULT_SORT };
      /** @type {ReturnType<typeof setTimeout> | undefined} */
      this.searchTimer = undefined;
    }

    start() {
      this.refreshEntryStates();
      this.refreshVersionDates();
      this.buildStateFilter();
      this.buildSelectFilters();
      this.readViewFromUrl();
      this.bindEvents();
      this.render();
      this.refreshUpcomingCards();
      this.toolbar.classList.add(TOOLBAR_READY_CLASS);
      this.jumpToUrlHash();
    }

    // ---- dates: the HTML is built on one day and read on another ----

    /** An entry built as "End date set" turns "Shut down" once its end date has come. */
    refreshEntryStates() {
      const { endStates, stateLabels } = this.labels;
      const endStateValues = Object.values(endStates);
      for (const row of this.rows) {
        const endDate = row.dataset.endDate ?? "";
        if (!endDate || !endStateValues.includes(row.dataset.state ?? "")) continue;
        const state = this.refreshEndDate(row, endDate) ? endStates.passed : endStates.ahead;
        row.dataset.state = state;
        const badge = requireElement(row, ".state-badge", HTMLElement);
        badge.className = `state-badge ${state}`;
        badge.textContent = stateLabels[state];
      }
    }

    /** Version deadlines read "Ended" once their date has come, and count the days until then. */
    refreshVersionDates() {
      for (const item of document.querySelectorAll(".version-item")) {
        if (item instanceof HTMLElement) this.refreshEndDate(item, item.dataset.endDate ?? "");
      }
    }

    /**
     * Sets the "Ended"/"Ends" words and the days left of one element with an end date.
     * @param {HTMLElement} element @param {string} endDate @returns {boolean} whether the date has come
     */
    refreshEndDate(element, endDate) {
      const ended = hasEnded(endDate, this.today);
      const { endDateWords } = this.labels;
      const label = requireElement(element, ".date-label", HTMLElement);
      label.textContent = ended ? endDateWords.passed : endDateWords.ahead;
      if (!ended) requireElement(element, ".cell-date", HTMLElement).append(this.createDaysLeft(endDate));
      return ended;
    }

    /** Drop cards whose date has come and keep as many on show as the build showed. */
    refreshUpcomingCards() {
      const cards = [...this.upcomingList.querySelectorAll(".upcoming-card")]
        .filter((card) => card instanceof HTMLElement);
      const visibleLimit = cards.filter((card) => !card.hidden).length;
      const ahead = cards.filter((card) => !hasEnded(card.dataset.endDate ?? "", this.today));
      for (const card of cards) if (!ahead.includes(card)) card.remove();
      ahead.forEach((card, position) => {
        card.hidden = position >= visibleLimit;
        card.append(this.createDaysLeft(card.dataset.endDate ?? ""));
      });
      const section = this.upcomingList.closest("section");
      if (section && ahead.length === 0) section.hidden = true;
    }

    /** @param {string} endDate */
    createDaysLeft(endDate) {
      return createSpan(describeDaysLeft(daysBetween(this.today, endDate)), "days-left");
    }

    // ---- controls, built from the rows so they never offer a value the table lacks ----

    buildStateFilter() {
      const countsByState = countBy(this.rows, (row) => row.dataset.state ?? "");
      this.stateFilter.append(this.createStateButton("", "All", this.rows.length));
      for (const state of this.stateOrder) {
        const count = countsByState.get(state);
        if (count) this.stateFilter.append(this.createStateButton(state, this.labels.stateLabels[state], count));
      }
    }

    /** @param {string} state @param {string} label @param {number} count */
    createStateButton(state, label, count) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "control";
      button.dataset.stateValue = state;
      // A real space, so a screen reader says "Shut down 46", not "Shut down46".
      button.append(label, " ", createSpan(String(count), "chip-count"));
      button.addEventListener("click", () => this.updateView({ state: this.view.state === state ? "" : state }));
      return button;
    }

    buildSelectFilters() {
      const years = new Set(this.rows.map((row) => (row.dataset.keyDate ?? "").slice(0, 4)));
      for (const year of [...years].sort().reverse()) this.yearFilter.add(new Option(year, year));
      const groups = new Set(this.rows.map((row) => row.dataset.group ?? ""));
      for (const group of [...groups].sort()) this.groupFilter.add(new Option(group, group));
      const countsByScope = countBy(this.rows, (row) => row.dataset.scope ?? "");
      for (const [scope, label] of Object.entries(this.labels.scopeLabels)) {
        const count = countsByScope.get(scope);
        if (count) this.scopeFilter.add(new Option(`${label} (${count})`, scope));
      }
    }

    // ---- the view lives in the URL, so a filtered list can be shared ----

    /** @param {string} state  "" for the All button */
    hasStateButton(state) {
      return [...this.stateFilter.querySelectorAll("button")].some((button) => button.dataset.stateValue === state);
    }

    /** A value no control offers, from an old or hand-edited link, falls back to showing everything. */
    readViewFromUrl() {
      const parameters = new URLSearchParams(location.search);
      /** @type {(keyof View)[]} */
      const names = [...FILTER_NAMES, "sort"];
      for (const name of names) this.view[name] = parameters.get(name) ?? this.view[name];
      /** @type {[keyof View, HTMLSelectElement][]} */
      const selects = [["year", this.yearFilter], ["group", this.groupFilter], ["scope", this.scopeFilter]];
      for (const [name, select] of selects) {
        if (!isOffered(select, this.view[name])) this.view[name] = "";
      }
      if (!isOffered(this.sortOrder, this.view.sort)) this.view.sort = DEFAULT_SORT;
      if (!this.hasStateButton(this.view.state)) this.view.state = "";
    }

    writeViewToUrl() {
      const parameters = new URLSearchParams();
      for (const [name, value] of Object.entries(this.view)) {
        if (value && !(name === "sort" && value === DEFAULT_SORT)) parameters.set(name, value);
      }
      const search = parameters.toString();
      history.replaceState(null, "", search ? `?${search}` : location.pathname);
    }

    /**
     * Every update reads the search box as it is now, so a change made while a search waits keeps what
     * was typed, and the waiting search has nothing left to do.
     * @param {Partial<View>} change
     */
    updateView(change) {
      clearTimeout(this.searchTimer);
      Object.assign(this.view, { query: this.searchInput.value }, change);
      this.render();
      this.writeViewToUrl();
    }

    clearFilters() {
      this.updateView(emptyFilters());
      this.searchInput.focus();
    }

    hasFilters() {
      return FILTER_NAMES.some((name) => this.view[name].trim() !== "");
    }

    // ---- filtering and sorting ----

    /** @param {HTMLTableRowElement} row */
    matchesView(row) {
      const { query, state, year, group, scope } = this.view;
      const searchText = normalizeSearchText(row.dataset.searchText ?? "");
      const queryWords = normalizeSearchText(query).split(/\s+/).filter(Boolean);
      return (!state || row.dataset.state === state)
        && (!year || (row.dataset.keyDate ?? "").startsWith(year))
        && (!group || row.dataset.group === group)
        && (!scope || row.dataset.scope === scope)
        && queryWords.every((word) => searchText.includes(word));
    }

    /** @param {HTMLTableRowElement} first @param {HTMLTableRowElement} second */
    compareRows(first, second) {
      const [field, direction] = this.view.sort.split("-");
      const sign = direction === "asc" ? 1 : -1;
      const firstData = first.dataset;
      const secondData = second.dataset;
      let result = 0;
      if (field === "date") result = compareText(firstData.keyDate ?? "", secondData.keyDate ?? "");
      if (field === "name") result = compareText(firstData.sortName ?? "", secondData.sortName ?? "");
      if (field === "state") {
        result = this.stateOrder.indexOf(firstData.state ?? "") - this.stateOrder.indexOf(secondData.state ?? "")
          || compareText(firstData.keyDate ?? "", secondData.keyDate ?? "");
      }
      // Ties read A to Z in every order, as in build.py.
      return result * sign || compareText(firstData.sortName ?? "", secondData.sortName ?? "");
    }

    // ---- rendering ----

    render() {
      this.syncControls();
      const shownRows = this.rows.filter((row) => this.matchesView(row))
        .sort((first, second) => this.compareRows(first, second));
      this.renderSections(shownRows);
      this.renderSortIndicators();
      const isFiltered = this.hasFilters();
      this.resultCount.textContent = isFiltered
        ? `${shownRows.length} of ${this.rows.length} entries`
        : `${this.rows.length} entries`;
      this.noResults.hidden = shownRows.length > 0;
      this.clearButton.hidden = !isFiltered;
    }

    syncControls() {
      // Never write into the box while the reader types in it: that would undo their last keys.
      if (document.activeElement !== this.searchInput) this.searchInput.value = this.view.query;
      this.yearFilter.value = this.view.year;
      this.groupFilter.value = this.view.group;
      this.scopeFilter.value = this.view.scope;
      this.sortOrder.value = this.view.sort;
      for (const button of this.stateFilter.querySelectorAll("button")) {
        button.setAttribute("aria-pressed", String(button.dataset.stateValue === this.view.state));
      }
    }

    /**
     * One <tbody> per year when sorted by date, as build.py renders it; one plain <tbody> otherwise.
     * @param {HTMLTableRowElement[]} shownRows
     */
    renderSections(shownRows) {
      const isGroupedByYear = this.view.sort.startsWith("date");
      /** @type {HTMLTableSectionElement[]} */
      const sections = [];
      /** @type {string | null} */
      let currentYear = null;
      for (const row of shownRows) {
        const year = isGroupedByYear ? (row.dataset.keyDate ?? "").slice(0, 4) : "";
        if (year !== currentYear) {
          sections.push(this.createSection(year));
          currentYear = year;
        }
        sections[sections.length - 1].append(row);
      }
      for (const section of [...this.table.tBodies]) section.remove();
      this.table.append(...sections);
    }

    /** @param {string} year  empty for a section without a year heading */
    createSection(year) {
      const section = document.createElement("tbody");
      if (year) {
        const yearRow = /** @type {HTMLTableRowElement} */ (this.yearRowTemplate.cloneNode(true));
        requireElement(yearRow, "th", HTMLTableCellElement).textContent = year;
        section.append(yearRow);
      }
      return section;
    }

    renderSortIndicators() {
      const [field, direction] = this.view.sort.split("-");
      for (const header of this.table.tHead?.rows[0].cells ?? []) {
        const button = header.querySelector("button");
        if (!button) continue;
        const isSorted = button.dataset.sortField === field;
        header.setAttribute("aria-sort", isSorted ? (direction === "asc" ? "ascending" : "descending") : "none");
      }
    }

    // ---- moving to one entry ----

    /** @param {HTMLTableRowElement} row */
    jumpTo(row) {
      if (!this.matchesView(row)) this.updateView(emptyFilters());
      requireElement(row, "details", HTMLDetailsElement).open = true;
      this.table.querySelector(`.${JUMP_TARGET_CLASS}`)?.classList.remove(JUMP_TARGET_CLASS);
      row.classList.add(JUMP_TARGET_CLASS);
      history.replaceState(null, "", `${location.search}#${row.id}`);
      const prefersReducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
      row.scrollIntoView({ block: "center", behavior: prefersReducedMotion ? "auto" : "smooth" });
      requireElement(row, "summary", HTMLElement).focus({ preventScroll: true });
    }

    /** Ids are slugs, plain ASCII, so the hash needs no decoding, and a malformed one cannot throw. */
    jumpToUrlHash() {
      const target = location.hash ? document.getElementById(location.hash.slice(1)) : null;
      if (target instanceof HTMLTableRowElement && this.rows.includes(target)) this.jumpTo(target);
    }

    // ---- events ----

    bindEvents() {
      this.searchInput.addEventListener("input", () => {
        clearTimeout(this.searchTimer);
        this.searchTimer = setTimeout(() => this.updateView({}), SEARCH_DELAY_MILLISECONDS);
      });
      this.searchInput.addEventListener("keydown", (event) => this.handleSearchKey(event));
      this.yearFilter.addEventListener("change", () => this.updateView({ year: this.yearFilter.value }));
      this.groupFilter.addEventListener("change", () => this.updateView({ group: this.groupFilter.value }));
      this.scopeFilter.addEventListener("change", () => this.updateView({ scope: this.scopeFilter.value }));
      this.sortOrder.addEventListener("change", () => this.updateView({ sort: this.sortOrder.value }));
      for (const button of this.table.querySelectorAll("thead button")) {
        if (!(button instanceof HTMLButtonElement)) continue;
        button.addEventListener("click", () => this.toggleSort(button.dataset.sortField ?? ""));
      }
      for (const button of [this.clearButton, this.clearButtonInEmptyState]) {
        button.addEventListener("click", () => this.clearFilters());
      }
      this.upcomingList.addEventListener("click", (event) => this.handleCardClick(event));
      this.trackToolbarHeight();
    }

    /**
     * Year headings stick just under the toolbar, whose height changes with wrapping, and the page scrolls
     * a focused control clear of it. Where the toolbar does not stick, nothing sits over the rows: 0px.
     */
    trackToolbarHeight() {
      const stickyToolbar = matchMedia(STICKY_TOOLBAR_QUERY);
      const update = () => {
        const height = stickyToolbar.matches ? this.toolbar.offsetHeight : 0;
        document.documentElement.style.setProperty("--toolbar-height", `${height}px`);
      };
      new ResizeObserver(update).observe(this.toolbar);
      stickyToolbar.addEventListener("change", update);
    }

    /**
     * A first click sorts any column up (dates oldest first, names A to Z); the next click reverses. The Sort select
     * holds the one list of orders, so a header never sets an order the select and a link cannot show.
     * @param {string} field
     */
    toggleSort(field) {
      const [currentField, currentDirection] = this.view.sort.split("-");
      const direction = currentField === field && currentDirection === "asc" ? "desc" : "asc";
      const sort = `${field}-${direction}`;
      if (isOffered(this.sortOrder, sort)) this.updateView({ sort });
    }

    /** @param {MouseEvent} event */
    handleCardClick(event) {
      const link = event.target instanceof Element ? event.target.closest("a[href^='#']") : null;
      const row = link ? document.getElementById((link.getAttribute("href") ?? "").slice(1)) : null;
      if (!(row instanceof HTMLTableRowElement)) return;
      event.preventDefault();
      this.jumpTo(row);
    }

    /** Escape empties the search box and shows every row again. @param {KeyboardEvent} event */
    handleSearchKey(event) {
      if (event.key !== "Escape" || event.isComposing || !this.searchInput.value) return;
      event.preventDefault();
      this.searchInput.value = "";
      this.updateView({});
    }
  }

  new RegisterPage().start();
}
