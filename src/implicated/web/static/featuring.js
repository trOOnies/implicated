// The song details' Featuring picker (`[data-featuring]`, see _song_details_edit.html): typing
// lists the matching artists below the search box; clicking one, or picking it with the arrow
// keys and Enter, adds it as a tag and clears the box. A tag's ✕ takes it away, and Backspace in
// the empty box takes the last one. Each tag holds a hidden `featuring` field, so the form posts as before.
// The form comes and goes with htmx, so everything is handled from the document.
(() => {
  const LIMIT = 8;
  // Ignore case and accents, so "bjork" finds "Björk".
  const fold = (text) => text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();

  const parts = (input) => {
    const picker = input.closest("[data-featuring]");
    return {
      picker,
      tags: picker.querySelector(".feat-tags"),
      list: picker.querySelector(".feat-options"),
      artists: JSON.parse(picker.querySelector(".feat-artists").textContent),
    };
  };
  const chosenIds = (tags) =>
    new Set([...tags.querySelectorAll('input[name="featuring"]')].map((el) => Number(el.value)));

  function close(input) {
    const { list } = parts(input);
    list.hidden = true;
    list.replaceChildren();
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function show(input) {
    const { tags, list, artists } = parts(input);
    const query = fold(input.value.trim());
    if (!query) return close(input);
    const chosen = chosenIds(tags);
    const matches = artists
      .filter((a) => !chosen.has(a.id) && fold(a.name).includes(query))
      // Names starting with the text first, then the rest, each alphabetically.
      .sort((a, b) => fold(b.name).startsWith(query) - fold(a.name).startsWith(query)
        || a.name.localeCompare(b.name))
      .slice(0, LIMIT);
    list.replaceChildren(
      ...(matches.length ? matches.map((a, i) => {
        const li = document.createElement("li");
        li.id = `${list.id}-${a.id}`;
        li.setAttribute("role", "option");
        li.dataset.id = a.id;
        li.textContent = a.name;
        li.setAttribute("aria-selected", i === 0 ? "true" : "false");
        return li;
      }) : [Object.assign(document.createElement("li"), {
        className: "feat-none", textContent: "No other artist matches",
      })]),
    );
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    mark(input, list.querySelector('[aria-selected="true"]'));
  }

  function mark(input, option) {
    const { list } = parts(input);
    for (const each of list.querySelectorAll("[role=option]")) {
      each.setAttribute("aria-selected", each === option ? "true" : "false");
    }
    if (option) {
      input.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({ block: "nearest" });
    }
  }

  function add(input, option) {
    const { tags, artists } = parts(input);
    const artist = artists.find((a) => a.id === Number(option.dataset.id));
    if (!artist || chosenIds(tags).has(artist.id)) return;
    const li = document.createElement("li");
    li.className = "tag feat-tag";
    const field = Object.assign(document.createElement("input"), {
      type: "hidden", name: "featuring", value: artist.id,
    });
    const remove = Object.assign(document.createElement("button"), {
      type: "button", className: "link feat-remove", title: "Remove", textContent: "✕",
    });
    remove.setAttribute("aria-label", `Remove ${artist.name}`);
    li.append(field, document.createTextNode(artist.name + " "), remove);
    tags.append(li);
    input.value = "";
    close(input);
    input.focus();
  }

  document.addEventListener("input", (event) => {
    if (event.target.matches("[data-featuring] .feat-input")) show(event.target);
  });

  document.addEventListener("keydown", (event) => {
    const input = event.target;
    if (!input.matches("[data-featuring] .feat-input")) return;
    const { tags, list } = parts(input);
    const options = [...list.querySelectorAll("[role=option]")];
    const at = options.findIndex((o) => o.getAttribute("aria-selected") === "true");
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (list.hidden) return show(input);
      if (!options.length) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      mark(input, options[(at + step + options.length) % options.length]);
    } else if (event.key === "Enter") {
      // Never submit the form from the search box.
      event.preventDefault();
      if (!list.hidden && at >= 0) add(input, options[at]);
    } else if (event.key === "Escape" && !list.hidden) {
      event.preventDefault();
      close(input);
    } else if (event.key === "Backspace" && !input.value) {
      tags.lastElementChild?.remove();
    }
  });

  // mousedown, not click, so the choice lands before the box loses focus and closes the list.
  document.addEventListener("mousedown", (event) => {
    const option = event.target.closest("[data-featuring] .feat-options [role=option]");
    if (!option) return;
    event.preventDefault();
    add(option.closest("[data-featuring]").querySelector(".feat-input"), option);
  });

  document.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-featuring] .feat-remove");
    if (!remove) return;
    const input = remove.closest("[data-featuring]").querySelector(".feat-input");
    remove.closest(".feat-tag").remove();
    input.focus();
  });

  document.addEventListener("focusout", (event) => {
    if (event.target.matches("[data-featuring] .feat-input")) close(event.target);
  });
})();
