// Sortable tables: clicking a header of a `table.sortable` sorts its rows by that column, and
// clicking it again reverses the order. Headers without text (buttons, covers) do not sort.
//
// Cells sort by their `data-sort` attribute when they have one, otherwise by their text; headers
// with `data-type="number"` compare numbers. Empty values always go last. The order is kept when
// htmx reloads a table, keyed by the id of the element around it, until a control with
// `data-sort-reset="<that id>"` changes (such as a select that orders the rows on the server).
(() => {
  const remembered = new Map(); // table key -> { index, ascending }
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

  const keyOf = (table) => {
    const holder = table.closest("[id]");
    return `${location.pathname} ${holder ? holder.id : ""}`;
  };

  const valueOf = (row, index) => {
    const cell = row.cells[index];
    if (!cell || row.cells.length === 1) return ""; // edit rows span the whole table
    return (cell.dataset.sort ?? cell.textContent).trim();
  };

  function sort(table, index, ascending) {
    const header = table.tHead.rows[0].cells[index];
    if (!header) return;
    const numeric = header.dataset.type === "number";
    const body = table.tBodies[0];
    const rows = [...body.rows].sort((a, b) => {
      const x = valueOf(a, index);
      const y = valueOf(b, index);
      if (x === "" || y === "") return (x === "") - (y === "");
      const order = numeric ? Number(x) - Number(y) : collator.compare(x, y);
      return ascending ? order : -order;
    });
    body.append(...rows);
    for (const th of table.tHead.rows[0].cells) th.removeAttribute("aria-sort");
    header.setAttribute("aria-sort", ascending ? "ascending" : "descending");
    remembered.set(keyOf(table), { index, ascending });
  }

  function setUp(root) {
    for (const table of root.querySelectorAll("table.sortable")) {
      if (!table.tHead || table.dataset.sortReady) continue;
      table.dataset.sortReady = "1";
      for (const th of table.tHead.rows[0].cells) {
        if (!th.textContent.trim()) continue;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "sort";
        button.title = "Sort by this column";
        button.append(...th.childNodes);
        th.append(button);
      }
      const last = remembered.get(keyOf(table));
      if (last) sort(table, last.index, last.ascending);
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("table.sortable th button.sort");
    if (!button) return;
    const th = button.closest("th");
    const table = th.closest("table");
    // Text columns start A–Z; number columns start with the biggest value.
    const ascending = th.hasAttribute("aria-sort")
      ? th.getAttribute("aria-sort") !== "ascending"
      : th.dataset.type !== "number";
    sort(table, th.cellIndex, ascending);
  });

  document.addEventListener("change", (event) => {
    const reset = event.target.closest("[data-sort-reset]");
    if (reset) remembered.delete(`${location.pathname} ${reset.dataset.sortReset}`);
  });

  document.addEventListener("DOMContentLoaded", () => setUp(document));
  document.addEventListener("htmx:afterSettle", (event) => setUp(event.target));
})();
