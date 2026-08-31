// Hover-to-enlarge for any card image.
//
// One popup element lives on <body> and is driven by event delegation, so it
// works for markup that arrives later via HTMX swaps or fetch() without any
// re-initialisation. To opt an image in, give it:
//     data-preview-src="<large image url>"
//
// The popup is appended to <body> and positioned with position:fixed rather
// than nested next to each thumbnail — inside the card modal (or any element
// with a transform or overflow:hidden) a nested popup would be clipped or
// positioned against the wrong ancestor.
//
// It is also a `popover`, which is what lets it show *over* the card modal.
// A <dialog> opened with showModal() is promoted to the browser's top layer
// and paints a ::backdrop across everything beneath it — no z-index can climb
// past that, so an ordinary element renders behind the dim. Popovers join the
// same top layer, so the preview sits above the backdrop instead of under it.

(function () {
  const GAP = 12;

  let popup = null;
  let popupImg = null;
  let activeTrigger = null;

  function ensurePopup() {
    if (popup) return;
    popup = document.createElement("div");
    popup.className = "hover-preview";
    // "manual" keeps it under our control: no light-dismiss, and opening it
    // never closes the card modal the way an "auto" popover would.
    popup.setAttribute("popover", "manual");
    popupImg = document.createElement("img");
    popupImg.alt = "";
    popup.appendChild(popupImg);
    document.body.appendChild(popup);
  }

  function place(trigger) {
    const rect = trigger.getBoundingClientRect();
    const pw = popup.offsetWidth;
    const ph = popup.offsetHeight;
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;

    // Prefer the side with more room, so the popup never runs off-screen.
    const roomLeft = rect.left;
    const roomRight = vw - rect.right;
    let left = roomLeft >= pw + GAP || roomLeft > roomRight ? rect.left - pw - GAP : rect.right + GAP;
    left = Math.max(GAP, Math.min(left, vw - pw - GAP));

    // Vertically centre on the thumbnail, then clamp into the viewport.
    let top = rect.top + rect.height / 2 - ph / 2;
    top = Math.max(GAP, Math.min(top, vh - ph - GAP));

    popup.style.left = `${Math.round(left)}px`;
    popup.style.top = `${Math.round(top)}px`;
  }

  function show(trigger) {
    const src = trigger.dataset.previewSrc;
    if (!src) return;
    ensurePopup();
    activeTrigger = trigger;

    const reposition = () => {
      if (activeTrigger === trigger) place(trigger);
    };

    if (popupImg.getAttribute("src") !== src) {
      popupImg.setAttribute("src", src);
      // Size isn't known until the image loads; place again once it is.
      popupImg.addEventListener("load", reposition, { once: true });
    }
    // Clear the fallback's attribute unconditionally. Both mechanisms are in
    // play, and `[hidden]` wins over a popover being open — so a single early
    // hide() taking the fallback branch would otherwise leave the popup
    // display:none forever, open but invisible.
    popup.hidden = false;
    if (!popup.matches(":popover-open")) {
      try {
        popup.showPopover();
      } catch (err) {
        // Older engines without popover support still get a working preview,
        // just beneath the modal backdrop rather than above it.
      }
    }
    reposition();
  }

  function hide() {
    activeTrigger = null;
    if (!popup) return;
    if (popup.matches(":popover-open")) {
      popup.hidePopover();
    } else {
      popup.hidden = true;   // fallback for engines without popover support
    }
  }

  document.addEventListener("mouseover", (e) => {
    const trigger = e.target.closest("[data-preview-src]");
    if (trigger && trigger !== activeTrigger) show(trigger);
  });

  document.addEventListener("mouseout", (e) => {
    const trigger = e.target.closest("[data-preview-src]");
    if (trigger && !trigger.contains(e.relatedTarget)) hide();
  });

  // Keyboard users get the same preview when tabbing to a focusable thumbnail.
  document.addEventListener("focusin", (e) => {
    const trigger = e.target.closest("[data-preview-src]");
    if (trigger) show(trigger);
  });
  document.addEventListener("focusout", hide);

  // Anything that moves the page out from under the popup should dismiss it.
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hide();
  });
})();
