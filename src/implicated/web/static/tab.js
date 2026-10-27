// Song tabs: draws the Guitar Pro file of a `.tab-score` (its `data-file`) with alphaTab, as
// notation and tab, and plays it with the soundfont in `data-soundfont`. The card around it
// holds the Play/Stop buttons, the track picker and a status line. Runs again whenever htmx
// swaps the card in, and drops the previous score (stopping its playback) first.
(() => {
  let current = null; // { element, api }

  function drop() {
    if (!current) return;
    current.api.destroy();
    current = null;
  }

  function setUp() {
    const element = document.querySelector(".tab-score[data-file]");
    if (current && current.element === element) return;
    drop();
    if (!element || !window.alphaTab) return;

    const card = element.closest(".card");
    const play = card.querySelector("[data-tab-play]");
    const stop = card.querySelector("[data-tab-stop]");
    const status = card.querySelector("[data-tab-status]");
    const tracks = card.querySelector("[data-tab-tracks]");

    const api = new alphaTab.AlphaTabApi(element, {
      core: { file: element.dataset.file, fontDirectory: element.dataset.fonts, tracks: [0] },
      player: {
        playerMode: alphaTab.PlayerMode.EnabledSynthesizer,
        soundFont: element.dataset.soundfont,
      },
    });
    current = { element, api };

    api.scoreLoaded.on((score) => {
      // Guitar Pro files often hold the whole band; pick the part to show and play.
      tracks.replaceChildren(
        ...score.tracks.map((track) => new Option(track.name || `Track ${track.index + 1}`, track.index)),
      );
      tracks.closest(".tab-tracks").hidden = score.tracks.length < 2;
    });
    tracks.addEventListener("change", () => {
      api.renderTracks([api.score.tracks[Number(tracks.value)]]);
    });
    api.renderFinished.on(() => {
      status.hidden = true;
    });
    api.error.on((error) => {
      status.hidden = false;
      status.className = "error";
      status.textContent = `Could not show the tab: ${error.message || error}`;
    });
    api.playerReady.on(() => {
      play.disabled = false;
      stop.disabled = false;
    });
    api.playerStateChanged.on((event) => {
      play.textContent = event.state === alphaTab.synth.PlayerState.Playing ? "Pause" : "Play";
    });
    play.addEventListener("click", () => api.playPause());
    stop.addEventListener("click", () => api.stop());
  }

  document.addEventListener("DOMContentLoaded", setUp);
  document.addEventListener("htmx:afterSettle", setUp);
})();
