// Asks every hx-confirm question in the app's own dialog (base.html's #confirm-dialog) instead of
// the browser's confirm(). The asking element may set data-confirm-title (the heading; the
// question is then the text under it) and data-confirm-ok (the OK button's label); a "danger"
// button gets a red OK button. Cancel has the focus, as the safe choice; Esc, the back gesture
// and a click outside (closedby="any") cancel too, and the browser puts the focus back.
document.addEventListener("htmx:confirm", (event) => {
  const question = event.detail.question;
  const dialog = document.getElementById("confirm-dialog");
  if (!question || !dialog?.showModal) return;  // no question, or fall back to confirm()
  event.preventDefault();
  const asker = event.detail.elt;
  const title = asker.dataset.confirmTitle;
  dialog.querySelector(".confirm-title").textContent = title || question;
  const text = dialog.querySelector(".confirm-text");
  text.textContent = title ? question : "";
  text.hidden = !title;
  const ok = dialog.querySelector("button[value=ok]");
  ok.textContent = asker.dataset.confirmOk || "OK";
  ok.classList.toggle("danger", asker.classList.contains("danger"));
  dialog.returnValue = "";
  dialog.addEventListener("close", () => {
    // Esc and clicks outside set no value: "" is dismissed without choosing.
    const confirmed = dialog.returnValue === "ok";
    dialog.returnValue = "";
    if (confirmed) event.detail.issueRequest(true);
  }, { once: true });
  dialog.showModal();
});

// Click outside to cancel where the browser lacks closedby: the press and the release must both
// be outside the dialog's box, so a drag that ends on the backdrop does not cancel.
if (!("closedBy" in HTMLDialogElement.prototype)) {
  const outside = (event, dialog) => {
    const box = dialog.getBoundingClientRect();
    return event.clientX < box.left || event.clientX > box.right
      || event.clientY < box.top || event.clientY > box.bottom;
  };
  let pressedOutside = false;
  document.addEventListener("pointerdown", (event) => {
    const dialog = event.target;
    pressedOutside = dialog.id === "confirm-dialog" && outside(event, dialog);
  });
  document.addEventListener("pointerup", (event) => {
    const dialog = event.target;
    if (pressedOutside && dialog.id === "confirm-dialog" && dialog.open && outside(event, dialog)) {
      dialog.close();
    }
    pressedOutside = false;
  });
}
