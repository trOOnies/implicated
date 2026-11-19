// Filter tags in a song search box (`.search-field`): typing a filter like `artist:abba ` (it
// ends with the space; quote a value with spaces, `artist:"the cure" `) turns it into a tag in
// front of the text, and the search runs again. Each tag sends a hidden `f` field; the server
// reads them (and any filter left in the text) with `implicated.songquery`.
//
// ✕ on a tag removes it; Backspace at the start of the box takes the last tag back into the
// text to edit. Enter also turns a filter at the very end into a tag before searching.
(() => {
  const FILTER =
    /(^|\s)(?:(artist|album|from|to|language|genre|stars):(?:"([^"]*)"|([^\s"]+))|(fav|later):)(?=\s|$)/gi;

  function addChip(field, key, value) {
    for (const old of field.querySelectorAll(".search-chip")) {
      if (old.querySelector("input").value.split(":")[0] === key) old.remove();
    }
    const chip = document.getElementById("search-chip").content.firstElementChild.cloneNode(true);
    chip.querySelector("input").value = `${key}:${value}`;
    chip.querySelector(".search-chip-key").textContent = key;
    const shown = chip.querySelector(".search-chip-value");
    if (value) shown.textContent = value;
    else shown.remove();
    chip.querySelector(".search-chip-x").setAttribute("aria-label", `Remove the filter ${key}${value ? ` ${value}` : ""}`);
    field.querySelector("input[name=q]").before(chip);
  }

  // Turn the finished filters in the box into tags; at the end of the text too when `atEnd`.
  function takeFilters(input, atEnd) {
    const field = input.closest(".search-field");
    let made = false;
    const rest = input.value.replace(FILTER, (whole, space, key, quoted, plain, flag, offset) => {
      const end = offset + whole.length;
      if (!atEnd && end === input.value.length) return whole; // still being typed
      if (flag) {
        addChip(field, flag.toLowerCase(), "");
      } else {
        const value = (quoted ?? plain).trim();
        if (!value) return whole;
        addChip(field, key.toLowerCase(), value);
      }
      made = true;
      return space;
    });
    if (made) input.value = rest.replace(/\s+/g, " ").replace(/^ /, "");
    return made;
  }

  const search = (field) => field.closest("form").requestSubmit();

  document.addEventListener("input", (event) => {
    const input = event.target.closest(".search-field input[name=q]");
    if (input && takeFilters(input, false)) search(input.closest(".search-field"));
  });

  document.addEventListener("keydown", (event) => {
    const input = event.target.closest?.(".search-field input[name=q]");
    if (!input) return;
    if (event.key === "Enter") {
      takeFilters(input, true); // the form's own submit then searches
    } else if (event.key === "Backspace" && input.selectionStart === 0 && input.selectionEnd === 0) {
      const chip = input.closest(".search-field").querySelector(".search-chip:last-of-type");
      if (!chip) return;
      event.preventDefault();
      const [key, ...value] = chip.querySelector("input").value.split(":");
      const text = value.join(":");
      const filter = `${key}:${/\s/.test(text) ? `"${text}"` : text}`;
      chip.remove();
      input.value = input.value ? `${filter} ${input.value}` : filter;
      input.setSelectionRange(filter.length, filter.length);
    }
  });

  document.addEventListener("click", (event) => {
    const x = event.target.closest(".search-chip-x");
    if (x) {
      const field = x.closest(".search-field");
      x.closest(".search-chip").remove();
      field.querySelector("input[name=q]").focus();
      search(field);
      return;
    }
    const field = event.target.closest(".search-field");
    if (field && event.target === field) field.querySelector("input[name=q]").focus();
  });
})();
