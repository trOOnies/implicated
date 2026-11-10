// Tier list boards (`#tier-board`): move songs between the levels and the unassigned ones, one
// at a time or many at once.
//
// Selecting: click a song (or Enter or Space on it) to add it to the selection or take it out,
// Shift+click another to select every song between the two, or drag a box from an empty spot to
// select the songs it touches (holding Shift, Ctrl or Cmd adds them to the selection instead).
// "Select all" selects the unassigned songs; Escape or "Clear" empties the selection.
//
// Moving: drag a song (`.tier-song`) onto a level or the unassigned songs (`.tier-drop`); when
// it is selected, the whole selection goes with it, in board order. It shows where it lands
// while dragging, and dropping it saves that place. Clicking a level (or Enter on it) puts the
// selected songs last there. Every move is posted to the board's `data-place` URL as `song_id`
// (the ids, split by commas), `tier_id` (empty for the unassigned songs) and `position` (where
// in the level, counting only the songs not moving), and the board the server sends back
// replaces this one.
//
// Locking mode (the board's `.locking`): a locked song (`.tier-song.locked`) cannot be dragged or
// moved, but other songs can be put anywhere around it. A selection holds locked songs or
// unlocked ones, never both: selecting a song of the other kind starts a new selection, and a
// box selects one kind (the unlocked songs it touches, if any). A selection of locked songs (in
// grey) cannot move; the lock button (`.tier-lock`) of any of its songs unlocks them all, and the
// lock of a selected unlocked song locks them all; so does "Lock" or "Unlock" in the selection
// bar. Alone, a song's lock button locks or unlocks just it. Locks show at once, without
// redrawing the board: they are posted quietly to the board's `data-lock`, and only when that
// fails is the board the server sends (with why) put in its place.
(() => {
  let dragged = null; // { song, group, from, next, dropped }
  let box = null; // { board, x, y, additive, base, element }
  let anchor = null; // the song a Shift+click selects from
  let skipClick = false; // the click that ends a box selection

  const boardOf = (element) => element?.closest?.("#tier-board");
  const songsIn = (root) => [...root.querySelectorAll(".tier-song")];
  const selectedIn = (board) => [...board.querySelectorAll(".tier-song.selected")];
  const isLocked = (song) => song.classList.contains("locked");
  // Whether the selection is of locked songs; null when nothing is selected.
  const lockedSelection = (board) => {
    const first = board.querySelector(".tier-song.selected");
    return first ? isLocked(first) : null;
  };
  const ownSongs = (zone) => [...zone.querySelectorAll(":scope > .tier-song")];

  function select(song, on) {
    song.classList.toggle("selected", on);
    song.setAttribute("aria-pressed", String(on));
  }

  function showSelection(board) {
    const count = selectedIn(board).length;
    const locked = lockedSelection(board);
    const songs = `${count} ${locked ? "locked " : ""}${count === 1 ? "song" : "songs"}`;
    const bar = board.querySelector(".tier-selection");
    if (bar) {
      bar.hidden = count === 0;
      bar.querySelector("[data-selection-count]").textContent = locked
        ? `${songs} selected: unlock them to move them`
        : `${songs} selected: drag one, or click a level`;
      const lockButton = bar.querySelector("[data-lock-selection]");
      if (lockButton) {
        lockButton.textContent = locked ? "Unlock" : "Lock";
        lockButton.title = locked ? "Unlock the selected songs" : "Lock the selected songs where they are";
      }
    }
    // Clicking a level moves an unlocked selection there; a locked one shows in grey.
    board.classList.toggle("picking", count > 0 && !locked);
    board.classList.toggle("picking-locked", count > 0 && locked === true);
  }

  function clear(board) {
    for (const song of selectedIn(board)) select(song, false);
    anchor = null;
    showSelection(board);
  }

  // Show songs as locked (or not) right away, and save that. They leave the selection.
  function lock(board, songs, locked) {
    for (const song of songs) {
      select(song, false);
      song.classList.toggle("locked", locked);
      song.draggable = !locked;
      const button = song.querySelector(".tier-lock");
      const title = song.querySelector(".tier-song-name").textContent;
      button.setAttribute("aria-pressed", String(locked));
      button.setAttribute("aria-label", `${locked ? "Unlock" : "Lock"} ${title}`);
      button.title = locked ? "Unlock it, to move it again" : "Lock it here: decided";
    }
    if (anchor && (!anchor.isConnected || songs.includes(anchor))) anchor = null;
    const count = board.querySelectorAll(".tier-song.locked").length;
    const label = board.querySelector("[data-locked-count]");
    if (label) label.textContent = count ? `(${count} locked)` : "";
    showSelection(board);

    const body = new URLSearchParams({
      song_id: songs.map((song) => song.dataset.song).join(","),
      locked: locked ? "1" : "0",
      quiet: "1",
    });
    const redraw = () => htmx.ajax("GET", board.dataset.board, { target: board, swap: "outerHTML" });
    fetch(board.dataset.lock, { method: "POST", body, headers: { "HX-Request": "true" } })
      .then(async (response) => {
        if (response.ok) {
          // Autoadd's counts leave locked songs out: refresh that button alone.
          htmx.ajax("GET", board.dataset.board, {
            target: "#tier-autoadd",
            select: "#tier-autoadd",
            swap: "outerHTML",
          });
        } else if (response.status === 422) {
          htmx.swap(board, await response.text(), { swapStyle: "outerHTML" }); // says why
        } else {
          redraw();
        }
      })
      .catch(redraw);
  }

  function place(board, songs, zone, position) {
    const values = {
      song_id: songs.map((song) => song.dataset.song).join(","),
      tier_id: zone.dataset.tier,
    };
    if (position !== undefined) values.position = position;
    htmx.ajax("POST", board.dataset.place, { target: board, swap: "outerHTML", values });
  }

  // The song of `zone` the pointer at (x, y) is before, or null when it is after all of them.
  function songAfter(zone, x, y) {
    for (const song of ownSongs(zone)) {
      if (song === dragged?.song) continue;
      const rect = song.getBoundingClientRect();
      if (y < rect.top) return song;
      if (y <= rect.bottom && x < rect.left + rect.width / 2) return song;
    }
    return null;
  }

  // Clicks and keys

  document.addEventListener("click", (event) => {
    if (skipClick) {
      skipClick = false;
      return;
    }
    const board = boardOf(event.target);
    if (!board) return;
    const lockButton = event.target.closest(".tier-lock");
    if (lockButton) {
      const song = lockButton.closest(".tier-song");
      // On a selected song, for the whole selection: all locked, or all not.
      const songs = song.classList.contains("selected") ? selectedIn(board) : [song];
      lock(board, songs, !isLocked(song));
      return;
    }
    if (event.target.closest("[data-lock-selection]")) {
      const selected = selectedIn(board);
      if (selected.length) lock(board, selected, !lockedSelection(board));
      return;
    }
    if (event.target.closest("[data-clear-selection]")) {
      clear(board);
      return;
    }
    if (event.target.closest("[data-select-all]")) {
      // The unlocked ones, or, when all of them are locked, the locked ones.
      const all = songsIn(board.querySelector(".tier-drop.unassigned"));
      const kind = all.some((s) => !isLocked(s)) ? false : true;
      if (lockedSelection(board) !== kind) clear(board);
      for (const song of all) if (isLocked(song) === kind) select(song, true);
      showSelection(board);
      return;
    }
    const song = event.target.closest(".tier-song");
    if (song) {
      const kind = lockedSelection(board);
      if (kind !== null && kind !== isLocked(song)) {
        // The other kind: a new selection.
        clear(board);
        select(song, true);
      } else if (event.shiftKey && anchor?.isConnected && anchor !== song && isLocked(anchor) === isLocked(song)) {
        const all = songsIn(board);
        const [from, to] = [all.indexOf(anchor), all.indexOf(song)].sort((a, b) => a - b);
        for (const between of all.slice(from, to + 1)) {
          if (isLocked(between) === isLocked(song)) select(between, true);
        }
      } else {
        select(song, !song.classList.contains("selected"));
      }
      anchor = song;
      showSelection(board);
      return;
    }
    const zone = event.target.closest(".tier-drop");
    const selected = selectedIn(board);
    if (zone && selected.length && !lockedSelection(board)) {
      // Unassigned songs clicked into the unassigned ones go nowhere.
      const moving = zone.dataset.tier === "" ? selected.filter((s) => s.parentElement !== zone) : selected;
      if (moving.length) place(board, moving, zone);
      else clear(board);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      const board = document.querySelector("#tier-board");
      if (board && selectedIn(board).length) clear(board);
      return;
    }
    if (event.key !== "Enter" && event.key !== " ") return;
    if (!event.target.matches?.(".tier-song, .tier-drop")) return; // not its lock button
    event.preventDefault();
    event.target.click();
  });

  // Dragging songs

  document.addEventListener("dragstart", (event) => {
    const song = event.target.closest?.(".tier-song");
    const board = boardOf(song);
    if (!song || !board) return;
    if (song.classList.contains("locked")) {
      event.preventDefault();
      return;
    }
    let group = [song];
    if (song.classList.contains("selected")) group = selectedIn(board);
    else clear(board);
    dragged = { song, group, from: song.parentElement, next: song.nextSibling, dropped: false };
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", group.map((s) => s.dataset.song).join(","));
    // Styled on the next frame, so the image the browser drags is the song as it was.
    requestAnimationFrame(() => {
      for (const member of group) member.classList.add(member === song ? "dragging" : "dragging-along");
    });
  });

  document.addEventListener("dragover", (event) => {
    const zone = event.target.closest?.(".tier-drop");
    if (!dragged || !zone) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const after = songAfter(zone, event.clientX, event.clientY);
    if (after) {
      if (dragged.song.nextElementSibling !== after) zone.insertBefore(dragged.song, after);
    } else if (dragged.song.parentElement !== zone || zone.lastElementChild !== dragged.song) {
      zone.append(dragged.song);
    }
  });

  document.addEventListener("drop", (event) => {
    const zone = event.target.closest?.(".tier-drop");
    if (!dragged || !zone) return;
    event.preventDefault();
    dragged.dropped = true;
    const { song, group, from, next } = dragged;
    const moved = group.length > 1 || song.parentElement !== from || song.nextSibling !== next;
    if (!moved) return;
    // Where in the level, among the songs that stay.
    const before = ownSongs(zone);
    const position = before.slice(0, before.indexOf(song)).filter((s) => !group.includes(s)).length;
    place(boardOf(zone), group, zone, position);
  });

  document.addEventListener("dragend", () => {
    if (!dragged) return;
    const { song, group, from, next, dropped } = dragged;
    for (const member of group) member.classList.remove("dragging", "dragging-along");
    // Let go somewhere else: put the song back where it was.
    if (!dropped) from.insertBefore(song, next && next.parentElement === from ? next : null);
    dragged = null;
  });

  // Selecting with a box

  document.addEventListener("pointerdown", (event) => {
    const board = boardOf(event.target);
    if (!board || event.button !== 0 || event.pointerType === "touch") return;
    if (event.target.closest(".tier-song, button, input, select, textarea, a, form")) return;
    event.preventDefault(); // no text selection while drawing the box
    const additive = event.shiftKey || event.ctrlKey || event.metaKey;
    box = { board, x: event.clientX, y: event.clientY, additive, base: selectedIn(board), element: null };
  });

  document.addEventListener("pointermove", (event) => {
    if (!box) return;
    const left = Math.min(box.x, event.clientX);
    const top = Math.min(box.y, event.clientY);
    const width = Math.abs(event.clientX - box.x);
    const height = Math.abs(event.clientY - box.y);
    if (!box.element) {
      if (width < 5 && height < 5) return; // a click, so far
      box.element = document.createElement("div");
      box.element.className = "tier-box";
      document.body.append(box.element);
    }
    Object.assign(box.element.style, {
      left: `${left}px`,
      top: `${top}px`,
      width: `${width}px`,
      height: `${height}px`,
    });
    const songs = songsIn(box.board);
    const hits = songs.filter((song) => {
      const rect = song.getBoundingClientRect();
      return rect.left < left + width && rect.right > left && rect.top < top + height && rect.bottom > top;
    });
    // One kind: the selection's, when adding to it; else the unlocked songs, if it touches any.
    const kept = box.additive ? box.base : [];
    const kind = kept.length ? isLocked(kept[0]) : !hits.some((song) => !isLocked(song));
    for (const song of songs) {
      select(song, isLocked(song) === kind && (hits.includes(song) || kept.includes(song)));
    }
    showSelection(box.board);
  });

  function endBox() {
    if (!box) return;
    if (box.element) {
      box.element.remove();
      skipClick = true;
      setTimeout(() => (skipClick = false)); // when no click follows
    }
    box = null;
  }
  document.addEventListener("pointerup", endBox);
  document.addEventListener("pointercancel", endBox);

  // A new board has nothing selected.
  document.addEventListener("htmx:afterSwap", () => {
    if (anchor && !anchor.isConnected) anchor = null;
  });
})();
