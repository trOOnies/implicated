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
    showDecided(board);
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

  // In locking mode, a level whose songs are all locked, or that has none, is decided: grey.
  function showDecided(board) {
    const locking = board.classList.contains("locking");
    for (const tier of board.querySelectorAll(".tier")) {
      const open = tier.querySelector(".tier-drop .tier-song:not(.locked)");
      tier.classList.toggle("decided", locking && !open);
    }
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

// Fast assign (`[data-fast-assign]`): gives each unassigned song (the unlocked ones, in board
// order) its place by asking which of two songs is better, in a dialog over the board.
//
// It is a binary search over the ranked songs, top level first. It first matches the song against
// the levels' borders (each level's top and bottom songs), the middle one first, to find its level.
// When it falls inside a level, it goes on with that level's songs between its top and bottom.
// When it falls between two levels, it would be a new border of either one, so a last question
// asks which: "the bottom of Super, or the top of Meh?" (empty levels between them are options too).
// Then it is placed there through the board's `data-place`, and the next song's questions start.
// Undo takes back the last answer, Skip leaves the song unassigned. With no song to place, it
// says so by the tools instead.
(() => {
  let run = null; // { song, levels, answers, skipped, placed, total, busy, note }
  let dialog = null;

  const boardNow = () => document.querySelector("#tier-board");

  const songData = (element) => ({
    id: element.dataset.song,
    title: element.querySelector(".tier-song-name").textContent,
    artist: element.dataset.artist || "",
    album: element.dataset.album || "",
    cover: element.querySelector("img")?.getAttribute("src") || null,
  });

  // The levels, top first, with their songs, as they are now.
  const levelsOf = (board) =>
    [...board.querySelectorAll(".tier")].map((tier, index) => {
      const zone = tier.querySelector(".tier-drop");
      const songs = [...zone.querySelectorAll(":scope > .tier-song")].map(songData);
      return { id: zone.dataset.tier, name: tier.dataset.name, index, songs };
    });

  // The unassigned songs fast assign can place (not locked, not skipped).
  const waiting = (board, skipped) =>
    [...board.querySelectorAll(".tier-drop.unassigned > .tier-song:not(.locked)")].filter(
      (song) => !skipped.has(song.dataset.song),
    );

  // The questions to place a song in `levels`: it yields each one, and takes its answer (for a
  // match, whether the song is the better one; for a level, which option), then returns where the
  // song goes, `{ level, position }`.
  function* search(levels) {
    const borders = [];
    for (const level of levels) {
      const { songs } = level;
      if (!songs.length) continue;
      borders.push({ song: songs[0], level, at: 0 });
      if (songs.length > 1) borders.push({ song: songs.at(-1), level, at: songs.length - 1 });
    }
    // How many borders are better than the song.
    let low = 0;
    let high = borders.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (yield { kind: "match", against: borders[middle] }) high = middle;
      else low = middle + 1;
    }
    const above = borders[low - 1];
    const below = borders[low];
    if (above && below && above.level === below.level) {
      // Inside a level: on with its songs between the top and the bottom ones.
      const { level } = above;
      const inner = level.songs.slice(1, -1);
      let from = 0;
      let to = inner.length;
      while (from < to) {
        const middle = (from + to) >> 1;
        if (yield { kind: "match", against: { song: inner[middle], level, at: middle + 1 } }) to = middle;
        else from = middle + 1;
      }
      return { level, position: from + 1 };
    }
    // Between two levels (or above the first songs, or below the last): a new border of which?
    const options = [];
    if (above) options.push({ level: above.level, position: above.level.songs.length, where: "bottom", by: above.song });
    const first = above ? above.level.index + 1 : 0;
    const last = below ? below.level.index : levels.length;
    for (const level of levels.slice(first, last)) options.push({ level, position: 0, where: "empty" });
    if (below) options.push({ level: below.level, position: 0, where: "top", by: below.song });
    if (options.length === 1) return options[0];
    return options[yield { kind: "level", options }];
  }

  // The next question for the song, or (done) where it goes.
  function step() {
    const questions = search(run.levels);
    let next = questions.next();
    for (const answer of run.answers) next = questions.next(answer);
    return next;
  }

  // Building the dialog

  function element(tag, className, text) {
    const made = document.createElement(tag);
    if (className) made.className = className;
    if (text !== undefined) made.textContent = text;
    return made;
  }

  function cover(song, className) {
    if (song.cover) {
      const image = element("img", className);
      image.src = song.cover;
      image.alt = "";
      return image;
    }
    return element("span", `${className} tier-fast-none`, "♪");
  }

  function badge(level, text) {
    return element("span", `tier-fast-level tier-${level.index}`, text ?? level.name);
  }

  // A big button for a song: its cover, title, artist and album, and where it is.
  function songOption(song, place) {
    const button = element("button", "tier-fast-option");
    button.type = "button";
    button.append(cover(song, "tier-fast-cover"), element("strong", "tier-fast-title", song.title));
    const by = [song.artist, song.album].filter(Boolean).join(" · ");
    if (by) button.append(element("span", "small", by));
    button.append(place);
    return button;
  }

  // A big button for a level the song could be a new border of.
  function levelOption(option) {
    const { level, where, by } = option;
    const button = element("button", "tier-fast-option tier-fast-place");
    button.type = "button";
    const text = where === "bottom" ? `Bottom of ${level.name}` : where === "top" ? `Top of ${level.name}` : level.name;
    button.append(badge(level, text));
    if (by) {
      const line = element("span", "tier-fast-by small");
      line.append(cover(by, "tier-fast-thumb"), element("span", "", `${where === "bottom" ? "below" : "above"} ${by.title}`));
      button.append(line);
    } else {
      button.append(element("span", "small", "an empty level"));
    }
    return button;
  }

  function build() {
    dialog = element("dialog", "tier-fast");
    dialog.setAttribute("aria-labelledby", "tier-fast-title");
    dialog.tabIndex = -1;
    dialog.innerHTML = `
      <div class="row tier-fast-head">
        <h2 id="tier-fast-title">Fast assign</h2>
        <span class="small" data-progress></span>
        <span class="spacer"></span>
        <button type="button" class="link" data-close aria-label="Close fast assign" title="Close (Escape)">✕</button>
      </div>
      <p class="tier-fast-question" data-question aria-live="polite"></p>
      <div class="tier-fast-options" data-options></div>
      <p class="warning" data-note hidden></p>
      <div class="row wrap tier-fast-foot">
        <button type="button" class="secondary" data-undo title="Take back the last answer (Backspace)">Undo</button>
        <button type="button" class="secondary" data-skip title="Leave this song unassigned, and go on with the next one (S)">Skip song</button>
        <span class="spacer"></span>
        <span class="small" data-keys></span>
      </div>`;
    dialog.addEventListener("close", () => (run = null));
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog || event.target.closest("[data-close]")) dialog.close(); // the backdrop too
      else if (event.target.closest("[data-undo]")) undo();
      else if (event.target.closest("[data-skip]")) skip();
      else {
        const option = event.target.closest(".tier-fast-option");
        if (option) answer(Number(option.dataset.answer));
      }
    });
    dialog.addEventListener("keydown", (event) => {
      if (!run || event.ctrlKey || event.metaKey || event.altKey) return;
      const options = [...dialog.querySelectorAll(".tier-fast-option")];
      let pick = null;
      if (/^[1-9]$/.test(event.key)) pick = Number(event.key) - 1;
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") pick = 0;
      else if ((event.key === "ArrowRight" || event.key === "ArrowDown") && options.length === 2) pick = 1;
      if (pick !== null && options[pick]) {
        event.preventDefault();
        options[pick].click();
      } else if (event.key === "Backspace") {
        event.preventDefault();
        undo();
      } else if (event.key === "s" || event.key === "S") {
        event.preventDefault();
        skip();
      }
    });
    document.body.append(dialog);
  }

  function draw() {
    const question = dialog.querySelector("[data-question]");
    const options = dialog.querySelector("[data-options]");
    const note = dialog.querySelector("[data-note]");
    dialog.querySelector("[data-progress]").textContent =
      `Song ${run.placed + 1} of ${run.total}` + (run.answers.length ? ` · ${run.answers.length} answered` : "");
    dialog.querySelector("[data-undo]").disabled = run.busy || !run.answers.length;
    dialog.querySelector("[data-skip]").disabled = run.busy;
    note.hidden = !run.note;
    note.textContent = run.note || "";
    options.replaceChildren();
    const next = step();
    if (next.done) {
      place(next.value);
      return;
    }
    const ask = next.value;
    options.classList.toggle("versus", ask.kind === "match");
    if (ask.kind === "match") {
      question.textContent = "Which is better?";
      const song = songOption(run.song, element("span", "tier-fast-level tier-fast-new", "Unassigned"));
      const { song: other, level, at } = ask.against;
      const where = at === 0 ? `Top of ${level.name}` : at === level.songs.length - 1 ? `Bottom of ${level.name}` : level.name;
      const against = songOption(other, badge(level, where));
      song.dataset.answer = "1";
      against.dataset.answer = "0";
      options.append(song, element("span", "tier-fast-vs", "vs"), against);
      dialog.querySelector("[data-keys]").textContent = "← or 1, → or 2";
    } else {
      question.textContent = "";
      question.append(`Where does `, element("strong", "", run.song.title), ` go?`);
      ask.options.forEach((option, index) => {
        const button = levelOption(option);
        button.dataset.answer = String(index);
        options.append(button);
      });
      dialog.querySelector("[data-keys]").textContent =
        ask.options.length === 2 ? "← or 1, → or 2" : `1 to ${ask.options.length}`;
    }
    dialog.focus();
  }

  function answer(value) {
    if (!run || run.busy) return;
    const next = step();
    if (next.done) return;
    run.answers.push(next.value.kind === "match" ? value === 1 : value);
    run.note = null;
    draw();
  }

  function undo() {
    if (!run || run.busy || !run.answers.length) return;
    run.answers.pop();
    draw();
  }

  function skip() {
    if (!run || run.busy) return;
    run.skipped.add(run.song.id);
    run.total -= 1;
    run.note = null;
    nextSong(boardNow());
  }

  // Put the song where it goes, then go on with the next one.
  function place({ level, position }) {
    const board = boardNow();
    const { song } = run;
    run.busy = true;
    dialog.querySelector("[data-question]").textContent = `Placing ${song.title} in ${level.name}…`;
    dialog.querySelector("[data-undo]").disabled = true;
    dialog.querySelector("[data-skip]").disabled = true;
    const values = { song_id: song.id, tier_id: level.id, position };
    const after = () => {
      if (!run) return;
      run.busy = false;
      const now = boardNow();
      if (!now) {
        dialog.close();
        return;
      }
      const still = now.querySelector(`.tier-drop.unassigned > .tier-song[data-song="${CSS.escape(song.id)}"]`);
      if (still) {
        // Not placed: leave it, and say why.
        run.skipped.add(song.id);
        run.total -= 1;
        run.note = `${song.title} could not be placed: ${now.querySelector(".error")?.textContent || "try again"}`;
      } else {
        run.placed += 1;
      }
      nextSong(now);
    };
    htmx.ajax("POST", board.dataset.place, { target: board, swap: "outerHTML", values }).then(after, after);
  }

  function nextSong(board) {
    const songs = board ? waiting(board, run.skipped) : [];
    if (!songs.length) {
      finish();
      return;
    }
    run.total = run.placed + songs.length;
    run.song = songData(songs[0]);
    run.levels = levelsOf(board);
    run.answers = [];
    draw();
  }

  function finish() {
    const { placed, note } = run;
    dialog.querySelector("[data-options]").replaceChildren();
    dialog.querySelector("[data-keys]").textContent = "";
    dialog.querySelector("[data-progress]").textContent = "";
    dialog.querySelector("[data-undo]").disabled = true;
    dialog.querySelector("[data-skip]").disabled = true;
    dialog.querySelector("[data-question]").textContent = placed
      ? `Done: ${placed} ${placed === 1 ? "song" : "songs"} placed.`
      : "Done: no song placed.";
    const shown = dialog.querySelector("[data-note]");
    shown.hidden = !note;
    shown.textContent = note || "";
    run = null;
    dialog.focus();
  }

  function warn(board, text) {
    const warning = board.querySelector(".tier-warning");
    if (!warning) return;
    warning.textContent = text;
    warning.hidden = false;
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-fast-assign]");
    const board = button?.closest("#tier-board");
    if (!board) return;
    const warning = board.querySelector(".tier-warning");
    if (warning) warning.hidden = true;
    if (!waiting(board, new Set()).length) {
      warn(
        board,
        board.querySelector(".tier-drop.unassigned > .tier-song")
          ? "Every unassigned song is locked: unlock one to fast assign it."
          : "Every song already has a level: there is no song to fast assign.",
      );
      return;
    }
    if (!dialog) build();
    run = { skipped: new Set(), placed: 0, total: 0, busy: false, note: null };
    dialog.showModal();
    nextSong(board);
  });
})();
