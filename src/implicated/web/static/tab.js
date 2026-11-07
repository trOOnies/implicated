// Song tabs: draws the Guitar Pro file of a `.tab-score` (its `data-file`) with alphaTab, as
// notation and tab, and plays it with the soundfont in `data-soundfont`. The card around it
// holds the Play/Stop buttons, the track picker and a status line. Runs again whenever htmx
// swaps the card in, and drops the previous score (stopping its playback) first. Space plays or
// pauses and Escape stops, unless you are typing in a field.
(() => {
  let current = null; // { element, api, play, stop }

  const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  // Standard tunings as MIDI notes, high string first like alphaTab: 7- and 6-string guitar,
  // 5- and 4-string bass.
  const STANDARD = ["64 59 55 50 45 40 35", "64 59 55 50 45 40", "43 38 33 28 23", "43 38 33 28"];

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
    const tuning = card.querySelector("[data-tab-tuning]");
    const only = card.querySelector("[data-tab-only]");

    // Play just the shown track, or the whole band. Only called from the page's own events and
    // playerReady: calling it from alphaTab's midiLoaded made alphaTab recurse until the stack
    // ran out, so the score never drew.
    function applyOnly() {
      if (!api.score) return;
      try {
        api.changeTrackSolo(api.score.tracks, false);
        if (only.checked) api.changeTrackSolo([api.score.tracks[Number(tracks.value) || 0]], true);
      } catch (error) {
        console.error("Could not solo the track", error);
      }
    }

    // The strings of the shown track, low to high, like "Tuning: E A D G B E (standard)".
    function showTuning(track) {
      const staff = track.staves[0];
      const strings = staff && !staff.isPercussion ? staff.tuning : [];
      tuning.hidden = !strings.length;
      if (!strings.length) return;
      const notes = [...strings].reverse().map((midi) => NOTE_NAMES[midi % 12]);
      const standard = STANDARD.includes(strings.join(" "));
      tuning.textContent = `Tuning: ${notes.join(" ")}${standard ? " (standard)" : ""}`;
    }

    const api = new alphaTab.AlphaTabApi(element, {
      core: { file: element.dataset.file, fontDirectory: element.dataset.fonts, tracks: [0] },
      player: {
        playerMode: alphaTab.PlayerMode.EnabledSynthesizer,
        soundFont: element.dataset.soundfont,
        // alphaTab scrolls the page to the cursor as soon as the score is drawn; only follow it
        // once Play is pressed (see below).
        scrollMode: alphaTab.ScrollMode.Off,
      },
    });
    current = { element, api, play, stop };

    api.scoreLoaded.on((score) => {
      // Guitar Pro files often hold the whole band; pick the part to show and play.
      tracks.replaceChildren(
        ...score.tracks.map((track) => new Option(track.name || `Track ${track.index + 1}`, track.index)),
      );
      tracks.closest(".tab-tracks").hidden = score.tracks.length < 2;
      showTuning(score.tracks[0]);
    });
    tracks.addEventListener("change", () => {
      const track = api.score.tracks[Number(tracks.value)];
      api.renderTracks([track]);
      showTuning(track);
      applyOnly();
      tracks.blur(); // So Space plays again instead of opening the picker.
    });
    only.addEventListener("change", () => {
      applyOnly();
      only.blur(); // So Space plays instead of ticking the box again.
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
      applyOnly();
    });
    api.playerStateChanged.on((event) => {
      play.textContent = event.state === alphaTab.synth.PlayerState.Playing ? "Pause" : "Play";
    });
    play.addEventListener("click", () => {
      // alphaTab makes its scroll handler when settings are applied, so apply them once here
      // (this neither redraws the score nor reloads the player).
      if (api.settings.player.scrollMode !== alphaTab.ScrollMode.Continuous) {
        api.settings.player.scrollMode = alphaTab.ScrollMode.Continuous;
        api.updateSettings();
      }
      api.playPause();
    });
    stop.addEventListener("click", () => api.stop());
  }

  // One listener for the page, so it doesn't pile up as htmx swaps the card in again.
  document.addEventListener("keydown", (event) => {
    if (!current || event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
    if (!document.contains(current.element)) return;
    const target = event.target;
    if (target.closest("input, textarea, select, [contenteditable]")) return;
    let button;
    if (event.key === "Escape") button = current.stop;
    // A focused button or link already does something with Space.
    else if (event.key === " " && !target.closest("button, a, summary")) button = current.play;
    if (!button || button.disabled) return;
    event.preventDefault(); // Space would otherwise scroll the page.
    button.click();
  });

  document.addEventListener("DOMContentLoaded", setUp);
  document.addEventListener("htmx:afterSettle", setUp);
})();
