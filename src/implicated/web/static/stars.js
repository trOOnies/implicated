// Star ratings in forms (`.rating[data-input]`, see _rating_input.html): a click sets the hidden
// rating field and fills the stars up to it, like the stars of saved records; clicking the
// current rating takes it away.
document.addEventListener("click", (event) => {
  const star = event.target.closest(".rating[data-input] .star");
  if (!star) return;
  const group = star.closest(".rating");
  const field = group.querySelector('input[name="rating"]');
  const picked = Number(star.dataset.value);
  const rating = Number(field.value) === picked ? 0 : picked;
  field.value = rating || "";
  for (const each of group.querySelectorAll(".star")) {
    const value = Number(each.dataset.value);
    each.classList.toggle("on", value <= rating);
    each.setAttribute("aria-pressed", value === rating ? "true" : "false");
  }
});
