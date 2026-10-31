// Live Play keeps time with the song: its bar fills over the song's length, and then the next
// song comes on as if pressed. The Play/Pause button stops and resumes both, and every song that
// comes on starts playing. A timer, not the end of the bar: bars stop in tabs in the background.
(() => {
  let timer = null;
  let left = 0; // milliseconds of the song to go
  let since = 0; // when they started going by

  const show = (paused) => {
    const toggle = document.getElementById("live-toggle");
    document.getElementById("live").classList.toggle("paused", paused);
    toggle.title = paused ? "Play" : "Pause";
    toggle.setAttribute("aria-label", toggle.title);
  };

  const finish = () => {
    timer = null;
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

  const enter = () => {
    clearTimeout(timer);
    timer = null;
    const toggle = document.getElementById("live-toggle");
    if (!toggle) return;
    // The list scrolls by itself, and would otherwise go back to its start with every song.
    const current = document.querySelector(".live-list [aria-current]");
    const box = current.closest(".live-tracks");
    box.scrollTop = current.offsetTop - box.clientHeight / 2;
    if (!toggle.dataset.seconds) return; // nothing to keep time with
    toggle.disabled = false;
    left = Number(toggle.dataset.seconds) * 1000;
    play();
  };

  document.addEventListener("click", (event) => {
    if (!event.target.closest("#live-toggle")) return;
    if (timer) pause();
    else play();
  });
  // Marking a song swaps its button, which also counts as new content: only a new song matters.
  document.addEventListener("htmx:load", (event) => {
    if (event.detail.elt === document.body || event.detail.elt.id === "live") enter();
  });
  document.addEventListener("htmx:historyRestore", enter);
})();
