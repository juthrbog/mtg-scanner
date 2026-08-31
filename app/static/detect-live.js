// Live card detection for the scan preview.
//
// Runs the same pipeline as the server's detect.py — blur, Canny, dilate,
// external contours, card-shaped filter — against the camera feed, and draws
// the detected outline over the video. Deliberately the *same* algorithm and
// the same constants: an overlay that used different logic would happily show
// a lock on something the server then fails to find.
//
// Exposes window.liveDetect:
//   .state       "loading" | "ready" | "unsupported"
//   .lastQuad    most recent detected corners in video pixel coords, or null
//   .isLocked()  true when a card has been held steady for a few frames
//   .start(video, canvas) / .stop()

(function () {
  // --- must stay in sync with app/recognition/detect.py --------------------
  const CARD_ASPECT = 88 / 63;
  const ASPECT_TOLERANCE = 0.32;
  const MIN_AREA_FRACTION = 0.035;
  const MAX_CONTOURS = 8;

  // Detection runs on a downscaled copy — a 1280px frame costs far more per
  // pass than it adds in accuracy at this scale.
  const WORK_WIDTH = 480;
  const INTERVAL_MS = 120;

  // A quad has to persist across a few passes before we call it a lock, so a
  // single noisy frame doesn't flash the outline green.
  const LOCK_FRAMES = 3;
  const LOCK_TOLERANCE = 0.12; // max corner drift between frames, as a fraction of frame width

  const api = {
    state: "loading",
    lastQuad: null,
    // Consecutive passes the same quad has held still for. The overlay turns
    // green at LOCK_FRAMES; auto-capture waits for more than that, so a
    // glimpse of something card-shaped can't trip the shutter.
    stableFrames: 0,
    isLocked: () => stableCount >= LOCK_FRAMES,
    start,
    stop,
  };
  window.liveDetect = api;

  let video = null;
  let overlay = null;
  let timer = null;
  let work = null; // offscreen canvas holding the downscaled frame
  let stableCount = 0;
  let prevQuad = null;

  function ready() {
    return typeof cv !== "undefined" && cv.Mat;
  }

  // OpenCV.js signals readiness through Module.onRuntimeInitialized; when the
  // script is already parsed and initialised, cv.Mat exists immediately.
  function whenReady(cb) {
    if (ready()) return cb();
    const started = Date.now();
    const poll = setInterval(() => {
      if (ready()) {
        clearInterval(poll);
        cb();
      } else if (Date.now() - started > 15000) {
        // Served from our own host, so 15s means it isn't coming.
        clearInterval(poll);
        api.state = "unsupported";
        document.dispatchEvent(new CustomEvent("livedetect:state"));
      }
    }, 100);
  }

  function aspectIsCardLike(w, h) {
    if (w <= 1 || h <= 1) return false;
    const ratio = Math.max(w, h) / Math.min(w, h);
    return Math.abs(ratio - CARD_ASPECT) <= ASPECT_TOLERANCE * CARD_ASPECT;
  }

  function orderCorners(pts) {
    // top-left has the smallest x+y, bottom-right the largest; the other two
    // split on y-x. Same ordering the server uses.
    const bySum = [...pts].sort((a, b) => a.x + a.y - (b.x + b.y));
    const byDiff = [...pts].sort((a, b) => a.y - a.x - (b.y - b.x));
    return [bySum[0], byDiff[0], bySum[3], byDiff[3]];
  }

  function detect(srcMat, frameArea) {
    const gray = new cv.Mat();
    const blurred = new cv.Mat();
    const edges = new cv.Mat();
    const contours = new cv.MatVector();
    const hierarchy = new cv.Mat();
    let best = null;

    try {
      cv.cvtColor(srcMat, gray, cv.COLOR_RGBA2GRAY);
      cv.GaussianBlur(gray, blurred, new cv.Size(5, 5), 0);
      cv.Canny(blurred, edges, 40, 120);
      cv.dilate(edges, edges, new cv.Mat(), new cv.Point(-1, -1), 2);
      cv.findContours(edges, contours, hierarchy, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE);

      const ranked = [];
      for (let i = 0; i < contours.size(); i++) {
        const c = contours.get(i);
        ranked.push({ i, area: cv.contourArea(c) });
      }
      ranked.sort((a, b) => b.area - a.area);

      for (const { i, area } of ranked.slice(0, MAX_CONTOURS)) {
        if (area < frameArea * MIN_AREA_FRACTION) break;
        const c = contours.get(i);
        const rect = cv.minAreaRect(c);
        if (!aspectIsCardLike(rect.size.width, rect.size.height)) continue;

        const approx = new cv.Mat();
        cv.approxPolyDP(c, approx, 0.02 * cv.arcLength(c, true), true);
        if (approx.rows === 4) {
          best = [];
          for (let k = 0; k < 4; k++) {
            best.push({ x: approx.data32S[k * 2], y: approx.data32S[k * 2 + 1] });
          }
        } else {
          const box = cv.RotatedRect.points(rect);
          best = box.map((p) => ({ x: p.x, y: p.y }));
        }
        approx.delete();
        break;
      }
    } finally {
      gray.delete();
      blurred.delete();
      edges.delete();
      contours.delete();
      hierarchy.delete();
    }
    return best ? orderCorners(best) : null;
  }

  function quadsAreClose(a, b, frameWidth) {
    if (!a || !b) return false;
    const limit = frameWidth * LOCK_TOLERANCE;
    return a.every((p, i) => Math.hypot(p.x - b[i].x, p.y - b[i].y) <= limit);
  }

  // The <video> is object-fit:contain inside its box, so the picture is
  // letterboxed. Map video pixels to on-screen pixels through the same fit.
  function videoToDisplay(box, vw, vh) {
    const scale = Math.min(box.width / vw, box.height / vh);
    return {
      scale,
      dx: (box.width - vw * scale) / 2,
      dy: (box.height - vh * scale) / 2,
    };
  }

  function draw(quad, videoScale) {
    const ctx = overlay.getContext("2d");
    const box = overlay.getBoundingClientRect();
    if (overlay.width !== Math.round(box.width) || overlay.height !== Math.round(box.height)) {
      overlay.width = Math.round(box.width);
      overlay.height = Math.round(box.height);
    }
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    if (!quad) return;

    const fit = videoToDisplay(overlay, video.videoWidth, video.videoHeight);
    const pts = quad.map((p) => ({
      x: p.x * videoScale * fit.scale + fit.dx,
      y: p.y * videoScale * fit.scale + fit.dy,
    }));

    const locked = api.isLocked();
    ctx.lineWidth = locked ? 3 : 2;
    ctx.strokeStyle = locked ? "rgba(74, 222, 128, 0.95)" : "rgba(255, 255, 255, 0.75)";
    ctx.setLineDash(locked ? [] : [7, 6]);
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.closePath();
    ctx.stroke();

    if (locked) {
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(74, 222, 128, 0.95)";
      pts.forEach((p) => {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 4.5, 0, Math.PI * 2);
        ctx.fill();
      });
    }
  }

  function tick() {
    if (!video || video.readyState < 2 || !video.videoWidth) return;

    const scale = video.videoWidth / WORK_WIDTH; // video px per work px
    const w = WORK_WIDTH;
    const h = Math.round(video.videoHeight / scale);
    if (!work) work = document.createElement("canvas");
    if (work.width !== w || work.height !== h) {
      work.width = w;
      work.height = h;
    }
    work.getContext("2d", { willReadFrequently: true }).drawImage(video, 0, 0, w, h);

    let src = null;
    let quad = null;
    try {
      src = cv.imread(work);
      quad = detect(src, w * h);
    } catch (err) {
      // A transient failure shouldn't kill the loop; the next tick retries.
      quad = null;
    } finally {
      if (src) src.delete();
    }

    if (quad && quadsAreClose(quad, prevQuad, w)) {
      stableCount += 1;
    } else {
      stableCount = quad ? 1 : 0;
    }
    prevQuad = quad;
    api.stableFrames = stableCount;
    api.lastQuad = quad ? quad.map((p) => ({ x: p.x * scale, y: p.y * scale })) : null;

    draw(quad, scale);
    document.dispatchEvent(new CustomEvent("livedetect:frame"));
  }

  function start(videoEl, overlayEl) {
    video = videoEl;
    overlay = overlayEl;
    whenReady(() => {
      api.state = "ready";
      document.dispatchEvent(new CustomEvent("livedetect:state"));
      stop();
      timer = setInterval(tick, INTERVAL_MS);
    });
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }
})();
