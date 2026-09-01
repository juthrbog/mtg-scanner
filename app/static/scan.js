// The one hand-written piece of client JS in the whole app: open the camera,
// let the user pick which one if there's more than one, grab a single frame
// on demand, and post it. Everything after that is server-rendered HTML
// swapped in by hand — no framework needed for a snapshot-and-confirm flow.

const video = document.getElementById("camera");
const canvas = document.getElementById("capture-canvas");
const captureBtn = document.getElementById("capture-btn");
const cameraSelect = document.getElementById("camera-select");
const resultEl = document.getElementById("scan-result");
const overlay = document.getElementById("detect-overlay");
const statusEl = document.getElementById("detect-status");
const statusText = document.getElementById("detect-status-text");
const autoCaptureToggle = document.getElementById("auto-capture");
const ocrToggle = document.getElementById("use-ocr");

let currentStream = null;

// Longest edge of the uploaded frame. Measured against the real index, the
// distance to the correct card keeps improving with resolution (mean 3.0 at
// 640px, 2.0 at 1280, 1.2 at 1920) even though the number of correct top-1
// matches plateaus around 1280 — the extra pixels buy margin rather than new
// hits. 1600 takes most of that margin without a needlessly large upload.
const MAX_UPLOAD_EDGE = 1600;

// Laptops don't reliably expose "front" vs "rear" the way phones do, and a
// USB webcam used for scanning shows up as just another video input — so
// instead of guessing facingMode, list every camera by its real label and
// let the user pick.
async function listCameras() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  const cameras = devices.filter((d) => d.kind === "videoinput");

  cameraSelect.innerHTML = "";
  cameras.forEach((cam, i) => {
    const opt = document.createElement("option");
    opt.value = cam.deviceId;
    opt.textContent = cam.label || `Camera ${i + 1}`;
    cameraSelect.appendChild(opt);
  });
  cameraSelect.disabled = cameras.length <= 1;
  return cameras;
}

async function startCamera(deviceId) {
  if (currentStream) {
    currentStream.getTracks().forEach((track) => track.stop());
  }
  try {
    const constraints = {
      video: deviceId
        ? { deviceId: { exact: deviceId }, width: { ideal: 1280 }, height: { ideal: 1280 } }
        : { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 1280 } },
    };
    currentStream = await navigator.mediaDevices.getUserMedia(constraints);
    video.srcObject = currentStream;
    captureBtn.disabled = false;

    // Start (or restart, after a camera switch) the live outline overlay.
    if (window.liveDetect && overlay) window.liveDetect.start(video, overlay);

    // Device labels are only populated once permission has been granted,
    // so (re-)list cameras now that we have a stream.
    await listCameras();
    const [track] = currentStream.getVideoTracks();
    const activeId = track.getSettings().deviceId;
    if (activeId) cameraSelect.value = activeId;
  } catch (err) {
    resultEl.innerHTML = `<div role="alert" class="alert alert-error"><span>Couldn't access a camera: ${err.message}</span></div>`;
  }
}

async function captureFrame() {
  // Upload the whole camera frame. This used to be cropped to a centred
  // card-shaped rectangle, which discarded ~60% of a 16:9 frame before the
  // server ever saw it — a card even slightly off-centre was cut apart and
  // could never be matched. Locating the card is now the server's job, and
  // it can only do that with the full picture.
  const scale = Math.min(1, MAX_UPLOAD_EDGE / Math.max(video.videoWidth, video.videoHeight));
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);

  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));
  const form = new FormData();
  form.append("photo", blob, "capture.jpg");

  // Send the outline the overlay is showing. The browser has been watching
  // the card across many frames and settling on a stable answer, while the
  // server gets one still and has to find it from scratch — so when the
  // preview has a lock, the browser's corners are the better information.
  // The server scores this crop alongside its own guesses rather than
  // trusting it blindly, so a stale or wrong outline can only lose.
  const quad = window.liveDetect && window.liveDetect.lastQuad;
  if (quad && quad.length === 4) {
    form.append("corners", JSON.stringify(quad.map((p) => [p.x * scale, p.y * scale])));
  }
  if (ocrToggle && ocrToggle.checked) form.append("use_ocr", "true");

  captureBtn.disabled = true;
  capturing = true;
  resultEl.innerHTML = `<div class="flex justify-center py-6"><span class="loading loading-spinner loading-lg"></span></div>`;

  try {
    const resp = await fetch("/scan/capture", { method: "POST", body: form });
    resultEl.innerHTML = await resp.text();
    // htmx doesn't see content injected via fetch() automatically — tell it
    // to scan the new HTML for hx-* attributes (the confirm buttons below).
    htmx.process(resultEl);
  } finally {
    captureBtn.disabled = false;
    capturing = false;
  }
}

// Reflect what the detector currently sees. This is advisory only — capture
// always works, because the server runs its own detection on the upload
// regardless of whether the browser managed to find the card first.
function setStatus(state, text) {
  if (!statusEl) return;
  statusEl.dataset.state = state;
  statusText.textContent = text;
}

document.addEventListener("livedetect:state", () => {
  if (window.liveDetect.state === "unsupported") {
    setStatus("unsupported", "Live detection unavailable — capture still works.");
  }
});

// ---- Auto-capture -------------------------------------------------------
// Fires the shutter once a card has been held still for AUTO_FRAMES detection
// passes. Two rules keep it from running away:
//
//   * it never re-fires while results from the last capture are on screen, and
//   * after firing it disarms until the card leaves the view, so swapping in
//     the next card is what re-arms it rather than a timer.
//
// It only captures — confirming a match is still yours, because auto-adding
// the wrong card is far more annoying than pressing a button.
const AUTO_FRAMES = 7; // ~0.85s at the detector's 120ms interval
const AUTO_CLEAR_FRAMES = 3; // consecutive empty passes before re-arming

let autoEnabled = localStorage.getItem("mtg.autoCapture") === "1";
let autoArmed = true;
let emptyFrames = 0;
let capturing = false;

if (autoCaptureToggle) {
  autoCaptureToggle.checked = autoEnabled;
  autoCaptureToggle.addEventListener("change", () => {
    autoEnabled = autoCaptureToggle.checked;
    localStorage.setItem("mtg.autoCapture", autoEnabled ? "1" : "0");
    autoArmed = true;
    emptyFrames = 0;
  });
}

// Name check is remembered between visits, like auto-capture, and defaults
// *on*: measured over 26 real captures the image hash never put the correct
// card first, while reading the printed name identified 21 of 25. Leaving it
// off ships the scanner with the half that works disabled.
//
// A stale "0" from before it became the default would do exactly that, so the
// old key is retired rather than read — anyone who had it off got that setting
// when art matching still existed and the choice meant something else.
[["mtg.nameCheck", ocrToggle, true]].forEach(([key, el, fallback]) => {
  if (!el) return;
  const stored = localStorage.getItem(key);
  el.checked = stored === null ? fallback : stored === "1";
  el.addEventListener("change", () => localStorage.setItem(key, el.checked ? "1" : "0"));
});
localStorage.removeItem("mtg.useArt");
localStorage.removeItem("mtg.useOcr");

function resultsOnScreen() {
  return resultEl.children.length > 0;
}

document.addEventListener("livedetect:frame", () => {
  if (window.liveDetect.state !== "ready") return;
  const quad = window.liveDetect.lastQuad;
  const frames = window.liveDetect.stableFrames;

  // Re-arm only after the card has actually left the view.
  if (!quad) {
    emptyFrames += 1;
    if (emptyFrames >= AUTO_CLEAR_FRAMES) {
      autoArmed = true;
      // Taking the card away also dismisses an *already confirmed* result, so
      // scanning a stack is place / confirm / swap rather than place /
      // confirm / dismiss / swap. Pending candidates are never cleared this
      // way — that would throw away a match you hadn't acted on yet.
      if (autoEnabled && resultEl.querySelector(".alert-success")) {
        resultEl.innerHTML = "";
      }
    }
  } else {
    emptyFrames = 0;
  }

  if (autoEnabled && autoArmed && !capturing && !resultsOnScreen() && frames >= AUTO_FRAMES) {
    autoArmed = false;
    captureFrame();
    return;
  }

  if (autoEnabled && !autoArmed && quad) {
    setStatus("locked", "Captured — take the card away for the next one");
  } else if (window.liveDetect.isLocked()) {
    if (autoEnabled && !resultsOnScreen()) {
      const left = Math.max(0, AUTO_FRAMES - frames);
      setStatus("armed", left ? "Holding steady — capturing…" : "Capturing…");
    } else {
      setStatus("locked", "Card detected — ready to capture");
    }
  } else if (quad) {
    setStatus("searching", "Card found, hold steady…");
  } else {
    setStatus("searching", "Looking for a card…");
  }
});

captureBtn.addEventListener("click", captureFrame);
cameraSelect.addEventListener("change", () => startCamera(cameraSelect.value));

// Refresh the list if a camera is plugged in or unplugged while this page is open.
navigator.mediaDevices.addEventListener?.("devicechange", listCameras);

// Enter captures from anywhere on the page, except while the user is
// actually interacting with the camera picker or a result-panel control
// (like a Confirm button), where Enter should do its normal thing.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const active = document.activeElement;
  if (active === cameraSelect || resultEl.contains(active)) return;
  e.preventDefault();
  if (!captureBtn.disabled) captureFrame();
});

startCamera();
