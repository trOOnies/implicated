// Live Play keeps time with the song: its bar fills over the song's length, and then the next
// song comes on as if pressed. The Play/Pause button stops and resumes both, and every song that
// comes on starts playing. A timer, not the end of the bar: bars stop in tabs in the background.
// Timed lyrics follow the same clock, and clicking a line moves it there. The app plays no sound,
// so that is how the clock catches up with the music.
(() => {
  let timer = null;
  let total = 0; // milliseconds in the song
  let left = 0; // milliseconds of the song to go
  let since = 0; // when they started going by
  let line = null; // the timed lyrics line showing as current

  const show = (paused) => {
    const toggle = document.getElementById("live-toggle");
    document.getElementById("live").classList.toggle("paused", paused);
    toggle.title = paused ? "Play" : "Pause";
    toggle.setAttribute("aria-label", toggle.title);
  };

  const position = () => total - left + (timer ? Date.now() - since : 0);

  // Lines are looked up every time, as Reload lyrics swaps them for new ones.
  const mark = () => {
    let current = null;
    for (const candidate of document.querySelectorAll("#lyrics [data-at]")) {
      if (Number(candidate.dataset.at) > position()) break;
      current = candidate;
    }
    if (current === line) return;
    line?.removeAttribute("aria-current");
    line = current;
    if (!line) return;
    line.setAttribute("aria-current", "true");
    // On narrow windows the page scrolls, not the card, and it stays where the reader left it.
    const box = line.closest("#lyrics");
    if (box.scrollHeight > box.clientHeight) box.scrollTop = line.offsetTop - box.clientHeight / 2;
  };

  const finish = () => {
    timer = null;
    left = 0;
    const next = document.getElementById("live-next");
    if (next && !next.disabled) return next.click();
    show(true); // the list is over
    document.getElementById("live-toggle").disabled = true;
  };

  const play = () => {
    since = Date.now();
    timer = setTimeout(finish, left);
    show(false);
  };

  const pause = () => {
    clearTimeout(timer);
    timer = null;
    left -= Date.now() - since;
    show(true);
  };

  const seek = (at) => {
    const running = timer !== null;
    clearTimeout(timer);
    timer = null;
    left = Math.max(total - at, 0);
    if (running) play();
    document.getElementById("live-toggle").disabled = false; // the list may have been over
    const [bar] = document.querySelector(".live-progress span").getAnimations();
    if (bar) bar.currentTime = at;
    mark();
  };

  const enter = () => {
    clearTimeout(timer);
    timer = null;
    total = left = 0;
    const toggle = document.getElementById("live-toggle");
    if (!toggle) return;
    // The list scrolls by itself, and would otherwise go back to its start with every song.
    const current = document.querySelector(".live-list [aria-current]");
    const box = current.closest(".live-tracks");
    box.scrollTop = current.offsetTop - box.clientHeight / 2;
    if (!toggle.dataset.seconds) return; // nothing to keep time with
    toggle.disabled = false;
    total = left = Number(toggle.dataset.seconds) * 1000;
    play();
    mark();
  };

  document.addEventListener("click", (event) => {
    const clicked = event.target.closest("[data-at]");
    if (clicked) return seek(Number(clicked.dataset.at));
    if (!event.target.closest("#live-toggle")) return;
    if (timer) pause();
    else play();
  });
  // Marking a song swaps its button, which also counts as new content: only a new song matters.
  document.addEventListener("htmx:load", (event) => {
    if (event.detail.elt === document.body || event.detail.elt.id === "live") enter();
  });
  document.addEventListener("htmx:historyRestore", enter);
  setInterval(mark, 250);
})();
