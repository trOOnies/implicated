// Reordering a playlist: each song row has a grip at its left (`table[data-playlist] .grip`).
//
// Click a grip to select its song, Ctrl (⌘ on a Mac) + click to add or drop one, Shift + click
// to select every song from the last one clicked. Drag a grip to move the selection (or just
// that song, when it is not selected) to where it is dropped; Escape cancels a drag, or else
// clears the selection. With a grip focused, ↑ and ↓ go to the next grip (with Shift, they
// select as they go) and Alt + ↑ or Alt + ↓ moves the selection one place.
//
// The rows move at once; the server then reloads the table ("playlists-changed"), and the
// selection is kept. Dragging only works in the playlist's own order: unsorted, or sorted by #.
(() => {
  const selected = new Set(); // song ids, as strings
  let anchor = null; // the song a Shift + click selects from
  let press = null; // a grip pressed and not yet released: { grip, x, y, pointerId }
  let drag = null; // a drag going on: { table, rows, badge, before }
  let justDragged = false;

  const tableOf = (el) => el.closest("table[data-playlist]");
  const rowsOf = (table) => [...table.tBodies[0].rows].filter((row) => row.dataset.songId);
  const idOf = (row) => row.dataset.songId;

  function status(table, message) {
    let el = document.getElementById("playlist-order-status");
    if (!el) {
      el = document.createElement("p");
      el.id = "playlist-order-status";
      el.className = "order-status";
      el.setAttribute("role", "status");
    }
    // After the part that reloads, so the message stays.
    const holder = table.closest("#songs") ?? table.closest(".table-scroll");
    if (el.previousElementSibling !== holder) holder.after(el);
    el.textContent = message;
    clearTimeout(el.timer);
    el.timer = setTimeout(() => (el.textContent = ""), 4000);
  }

  function paint(table) {
    for (const row of rowsOf(table)) {
      const on = selected.has(idOf(row));
      row.classList.toggle("selected", on);
      row.querySelector(".grip")?.setAttribute("aria-pressed", String(on));
    }
  }

  // Dragging in a column's order would not say where the songs go in the playlist.
  function inPlaylistOrder(table) {
    const sorted = table.tHead.querySelector("th[aria-sort]");
    return !sorted || (sorted.classList.contains("grip-col") && sorted.getAttribute("aria-sort") === "ascending");
  }

  function select(row, { shift = false, toggle = false } = {}) {
    const table = tableOf(row);
    const id = idOf(row);
    if (shift && anchor) {
      const rows = rowsOf(table);
      const from = rows.findIndex((r) => idOf(r) === anchor);
      const to = rows.indexOf(row);
      if (from !== -1) {
        if (!toggle) selected.clear();
        for (const r of rows.slice(Math.min(from, to), Math.max(from, to) + 1)) selected.add(idOf(r));
        paint(table);
        return;
      }
    }
    if (toggle) {
      selected.has(id) ? selected.delete(id) : selected.add(id);
    } else if (selected.size === 1 && selected.has(id)) {
      selected.clear();
    } else {
      selected.clear();
      selected.add(id);
    }
    anchor = id;
    paint(table);
  }

  // Move the songs to just before the song `before` (the end when null): at once in the
  // table, then on the server.
  async function move(table, ids, before) {
    const rows = rowsOf(table);
    const order = rows.map(idOf);
    const moving = order.filter((id) => ids.includes(id));
    const rest = order.filter((id) => !ids.includes(id));
    const at = before === null ? rest.length : rest.indexOf(before);
    const next = [...rest.slice(0, at), ...moving, ...rest.slice(at)];
    if (next.every((id, i) => id === order[i])) return; // dropped where they were
    const byId = new Map(rows.map((row) => [idOf(row), row]));
    const after = rows.at(-1).nextSibling; // rows that are not songs (an edit row) stay last
    for (const id of next) table.tBodies[0].insertBefore(byId.get(id), after);
    const body = new URLSearchParams();
    for (const id of moving) body.append("song_id", id);
    if (before !== null) body.append("before", before);
    const noun = moving.length === 1 ? "song" : `${moving.length} songs`;
    try {
      const response = await fetch(`/playlists/${table.dataset.playlist}/songs/reorder`, {
        method: "POST",
        body,
        headers: { "HX-Request": "true" },
      });
      if (!response.ok) throw new Error(await response.text());
      status(table, `Moved ${noun}.`);
    } catch {
      status(table, `Could not move the ${noun}; the playlist is as it was.`);
    }
    htmx.trigger(document.body, "playlists-changed");
  }

  // Alt + ↑ / ↓: the selection (or the focused song) one place earlier or later.
  function nudge(row, step) {
    const table = tableOf(row);
    if (!inPlaylistOrder(table)) return status(table, "Sort by # to move songs.");
    const ids = selected.has(idOf(row)) ? [...selected] : [idOf(row)];
    const order = rowsOf(table).map(idOf);
    const rest = order.filter((id) => !ids.includes(id));
    const first = order.findIndex((id) => ids.includes(id));
    const last = order.findLastIndex((id) => ids.includes(id));
    let before;
    if (step < 0) {
      // Before the song just above the first of them.
      const above = order.slice(0, first).filter((id) => !ids.includes(id)).at(-1);
      if (above === undefined) return;
      before = above;
    } else {
      // After the song just below the last of them.
      const below = order.slice(last + 1).find((id) => !ids.includes(id));
      if (below === undefined) return;
      before = rest[rest.indexOf(below) + 1] ?? null;
    }
    move(table, ids, before);
    row.querySelector(".grip").focus();
  }

  // Where the dragged songs would go: before the first other row below the pointer's middle.
  function dropTarget(y) {
    const others = rowsOf(drag.table).filter((row) => !selected.has(idOf(row)));
    for (const row of others) row.classList.remove("drop-before", "drop-after");
    const below = others.find((row) => {
      const box = row.getBoundingClientRect();
      return y < box.top + box.height / 2;
    });
    if (below) below.classList.add("drop-before");
    else others.at(-1)?.classList.add("drop-after");
    drag.before = below ? idOf(below) : null;
  }

  function startDrag(event) {
    const row = press.grip.closest("tr");
    const table = tableOf(row);
    if (!inPlaylistOrder(table)) {
      status(table, "Sort by # to drag songs.");
      press = null;
      return;
    }
    if (!selected.has(idOf(row))) {
      selected.clear();
      selected.add(idOf(row));
      anchor = idOf(row);
      paint(table);
    }
    const badge = document.createElement("div");
    badge.className = "drag-badge";
    const title = row.querySelector("a[href^='/songs/']")?.textContent.trim();
    badge.textContent = selected.size === 1 ? title || "1 song" : `${selected.size} songs`;
    document.body.append(badge);
    drag = { table, badge, before: undefined };
    document.documentElement.classList.add("dragging-songs");
    for (const r of rowsOf(table)) r.classList.toggle("dragging", selected.has(idOf(r)));
    moveDrag(event);
  }

  function moveDrag(event) {
    drag.badge.style.translate = `${event.clientX + 14}px ${event.clientY + 10}px`;
    dropTarget(event.clientY);
    // Scroll when near the top or bottom of the window.
    const edge = 48;
    if (event.clientY < edge) scrollBy(0, -12);
    else if (event.clientY > innerHeight - edge) scrollBy(0, 12);
  }

  function endDrag(drop) {
    const { table, badge, before } = drag;
    badge.remove();
    document.documentElement.classList.remove("dragging-songs");
    for (const row of rowsOf(table)) row.classList.remove("dragging", "drop-before", "drop-after");
    drag = null;
    justDragged = true;
    setTimeout(() => (justDragged = false));
    if (drop && before !== undefined) {
      const order = rowsOf(table).map(idOf);
      move(table, order.filter((id) => selected.has(id)), before);
    }
  }

  document.addEventListener("pointerdown", (event) => {
    const grip = event.target.closest("table[data-playlist] .grip");
    if (!grip || event.button !== 0) return;
    press = { grip, x: event.clientX, y: event.clientY, pointerId: event.pointerId };
    grip.setPointerCapture(event.pointerId);
  });

  document.addEventListener("pointermove", (event) => {
    if (!press || event.pointerId !== press.pointerId) return;
    if (!drag && Math.hypot(event.clientX - press.x, event.clientY - press.y) > 5) startDrag(event);
    else if (drag) moveDrag(event);
  });

  document.addEventListener("pointerup", (event) => {
    if (!press || event.pointerId !== press.pointerId) return;
    press = null;
    if (drag) endDrag(true);
  });

  document.addEventListener("pointercancel", () => {
    press = null;
    if (drag) endDrag(false);
  });

  document.addEventListener("click", (event) => {
    const grip = event.target.closest("table[data-playlist] .grip");
    if (!grip) return;
    if (justDragged) return;
    select(grip.closest("tr"), { shift: event.shiftKey, toggle: event.ctrlKey || event.metaKey });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (drag) {
        press = null;
        endDrag(false);
        return;
      }
      const table = document.querySelector("table[data-playlist]");
      if (table && selected.size && !event.target.closest("input, textarea, select, dialog")) {
        selected.clear();
        paint(table);
      }
      return;
    }
    const grip = event.target.closest?.("table[data-playlist] .grip");
    if (!grip || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
    event.preventDefault();
    const row = grip.closest("tr");
    const step = event.key === "ArrowUp" ? -1 : 1;
    if (event.altKey) return nudge(row, step);
    const rows = rowsOf(tableOf(row));
    const next = rows[rows.indexOf(row) + step];
    if (!next) return;
    if (event.shiftKey) {
      if (!anchor || !selected.size) {
        anchor = idOf(row);
        selected.add(idOf(row));
      }
      select(next, { shift: true });
    }
    next.querySelector(".grip").focus();
  });

  // The table is reloaded after every change: keep the selection, and the focused grip.
  let focused = null;
  document.addEventListener("htmx:beforeSwap", () => {
    focused = document.activeElement?.closest?.("table[data-playlist] tr")?.dataset.songId ?? null;
  });
  document.addEventListener("htmx:afterSettle", () => {
    const table = document.querySelector("table[data-playlist]");
    if (!table) return;
    const present = new Set(rowsOf(table).map(idOf));
    for (const id of [...selected]) if (!present.has(id)) selected.delete(id);
    paint(table);
    if (focused && present.has(focused)) {
      table.querySelector(`tr[data-song-id="${focused}"] .grip`)?.focus();
    }
    focused = null;
  });
})();
