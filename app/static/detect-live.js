// Live card detection for the scan preview.
//
// Finds the card in the camera feed and draws its outline over the video.
//
// It shares detect.py's *gates* — the aspect range, the size bounds, the
// corner-regularity test — and its constants, so the overlay cannot claim a
// lock on something the server will reject out of hand.
//
// It runs the same two searches — contours for a closed outline, Hough lines
// for the broken one a borderless card gives — and scores candidates with the
// same weights, measured at ~40ms a pass against the 120ms budget.
//
// It is not identical, in two places. The server pads the frame before
// detecting, so a card at the edge of view still closes its outline; here that
// is replaced by rejecting quads that rest on the picture's edge, since
// without padding the image boundary is itself a perfect straight edge that
// attracts lines. And the gradient is scaled against a multiple of the mean
// rather than a percentile, because OpenCV.js has no percentile reduction.
//
// So the overlay stays a hint: the server re-detects on the captured frame,
// and its corners are only one of several candidates the matcher scores — a
// hint that is slightly off cannot spoil the result.
//
// Exposes window.liveDetect:
//   .state       "loading" | "ready" | "unsupported"
//   .lastQuad    most recent detected corners in video pixel coords, or null
//   .isLocked()  true when a card has been held steady for a few frames
//   .start(video, canvas) / .stop()

(function () {
  // --- must stay in sync with app/recognition/detect.py --------------------
  const CARD_ASPECT = 88 / 63;
  // Mirrors detect.py. Stated as a range rather than a tolerance band because
  // the band form hid a bug: written as +/-32% around 1.397 it worked out to
  // 0.95-1.84, and since the ratio is max/min it can never be under 1.0 — so
  // the gate really accepted 1.00-1.84 and called a square, a 4:3 frame or a
  // forearm a card. Combined with "take the biggest contour", the overlay
  // reliably drew a quad round the whole scene instead of the card.
  const ASPECT_MIN = 1.12;
  const ASPECT_MAX = 1.85;
  const MIN_AREA_FRACTION = 0.035;
  // A card held to a webcam fills perhaps a third of the view; more than this
  // is the frame, the wall, or the person holding it.
  const MAX_AREA_FRACTION = 0.72;
  const MAX_CONTOURS = 12;
  // Deliberately higher than detect.py's 12, which is the one constant here
  // that is not a mirror. The server pads the frame and so has a slightly
  // cleaner edge map; without padding this pass needs more lines before the
  // card's own faint top edge survives the length ranking. Measured on a real
  // borderless capture: at 12 the overlay settles on the card's text box, at
  // 14 it finds the card. The server gains nothing above 12 and costs 116ms
  // more, so the two are tuned separately rather than kept artificially equal.
  const HOUGH_MAX_LINES = 14;
  const HOUGH_MIN_LENGTH_FRACTION = 0.18;
  // How much of a quad's outline must rest on a real edge, on its weakest
  // side. detect.py scales the gradient against its 99th percentile; OpenCV.js
  // has no percentile reduction, so the scale here is a multiple of the mean
  // (see gradientFor) and the threshold is calibrated to that.
  const EDGE_SUPPORT_MIN = 0.12;

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

  function orderCorners(pts) {
    // top-left has the smallest x+y, bottom-right the largest; the other two
    // split on y-x. Same ordering the server uses.
    const bySum = [...pts].sort((a, b) => a.x + a.y - (b.x + b.y));
    const byDiff = [...pts].sort((a, b) => a.y - a.x - (b.y - b.x));
    return [bySum[0], byDiff[0], bySum[3], byDiff[3]];
  }

  // Aspect measured from the quad's own sides rather than its bounding box: a
  // tilted card's bounding box is nearly square even when the card plainly
  // isn't, which throws away the signal the gate depends on.
  function quadAspect(p) {
    const d = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
    const w = (d(p[0], p[1]) + d(p[3], p[2])) / 2;
    const h = (d(p[0], p[3]) + d(p[1], p[2])) / 2;
    if (w < 2 || h < 2) return 0;
    return Math.max(w, h) / Math.min(w, h);
  }

  // 1 when every corner is square, falling to 0 by 45 degrees of skew.
  function cornerRegularity(p) {
    let worst = 0;
    for (let i = 0; i < 4; i++) {
      const a = p[(i + 3) % 4], b = p[i], c = p[(i + 1) % 4];
      const v1 = { x: a.x - b.x, y: a.y - b.y };
      const v2 = { x: c.x - b.x, y: c.y - b.y };
      const n1 = Math.hypot(v1.x, v1.y), n2 = Math.hypot(v2.x, v2.y);
      if (n1 < 2 || n2 < 2) return 0;
      const cos = Math.max(-1, Math.min(1, (v1.x * v2.x + v1.y * v2.y) / (n1 * n2)));
      worst = Math.max(worst, Math.abs((Math.acos(cos) * 180) / Math.PI - 90));
    }
    return Math.max(0, 1 - worst / 45);
  }

  // How well the quad's weakest side is backed by an actual intensity edge.
  //
  // The test that separates a card from everything else that is roughly
  // rectangular. A spurious quad — a shadow, a text box, a rectangle resting
  // partly on the edge of the image — is supported on the sides that happen to
  // follow something and unsupported on the rest; a card is supported all the
  // way round. Scoring the *weakest* side is what tells them apart: an average
  // lets three strong sides carry a fourth that rests on nothing.
  //
  // `grad` is a single-channel Uint8 gradient magnitude and `gradScale` the
  // value that counts as full support.
  function edgeSupport(p, grad, gradScale) {
    const w = grad.cols, h = grad.rows, data = grad.data;
    let weakest = 1;
    for (let i = 0; i < 4; i++) {
      const a = p[i], b = p[(i + 1) % 4];
      let sum = 0;
      const N = 24;
      for (let s = 0; s < N; s++) {
        // Skip the corners: least reliably placed part of a quad.
        const t = 0.08 + (0.84 * s) / (N - 1);
        const x = Math.round(a.x + (b.x - a.x) * t);
        const y = Math.round(a.y + (b.y - a.y) * t);
        if (x < 1 || y < 1 || x >= w - 1 || y >= h - 1) continue;
        // Strongest response in a 3x3 window, so an edge a pixel or two off
        // the fitted line still counts as support.
        let best = 0;
        for (let dy = -1; dy <= 1; dy++) {
          const row = (y + dy) * w;
          for (let dx = -1; dx <= 1; dx++) {
            const v = data[row + x + dx];
            if (v > best) best = v;
          }
        }
        sum += Math.min(best / gradScale, 1);
      }
      weakest = Math.min(weakest, sum / N);
    }
    return weakest;
  }

  // How card-like a quad is, or null if it isn't plausibly a card.
  // Mirrors _score_quad in detect.py, including its weights.
  //
  function scoreQuad(quad, frameArea, grad, gradScale) {
    const area = Math.abs(polygonArea(quad));
    if (area < frameArea * MIN_AREA_FRACTION) return null;
    if (area > frameArea * MAX_AREA_FRACTION) return null;
    // Ordered once and reused: this runs on thousands of candidate quads per
    // frame, and ordering sorts the corners twice over.
    const p = orderCorners(quad);
    const aspect = quadAspect(p);
    if (aspect < ASPECT_MIN || aspect > ASPECT_MAX) return null;
    const regularity = cornerRegularity(p);
    if (regularity < 0.25) return null;
    // Reject anything resting on the edge of the picture. detect.py pads the
    // frame before detecting and drops quads that turn out to be the padded
    // border; there is no padding here, so the same nuisance shows up as a
    // quad with a side lying along the image boundary — the boundary is a
    // perfect straight edge, so it attracts Hough lines and pairs happily with
    // a real card edge opposite it.
    if (grad) {
      const m = 2;
      for (const c of quad) {
        if (c.x < m || c.y < m || c.x > grad.cols - 1 - m || c.y > grad.rows - 1 - m) return null;
      }
    }
    const support = grad ? edgeSupport(p, grad, gradScale) : 1;
    if (support < EDGE_SUPPORT_MIN) return null;
    const closeness = 1 - Math.min(Math.abs(aspect - CARD_ASPECT) / 0.45, 1);
    // Prefer the larger candidate, saturating at a plausible card size: a card
    // is full of smaller rectangles (text box, art window) that are *more*
    // perfectly card-shaped than the card itself.
    const size = Math.min(area / (frameArea * 0.3), 1);
    return 2.0 * support + 1.2 * closeness + 1.0 * size + 0.8 * regularity;
  }

  function polygonArea(q) {
    let a = 0;
    for (let i = 0; i < q.length; i++) {
      const p = q[i], n = q[(i + 1) % q.length];
      a += p.x * n.y - n.x * p.y;
    }
    return a / 2;
  }

  function quadsFrom(channel, frameArea, out, union, grad, gradScale) {
    const blurred = new cv.Mat();
    const edges = new cv.Mat();
    const contours = new cv.MatVector();
    const hierarchy = new cv.Mat();
    try {
      cv.GaussianBlur(channel, blurred, new cv.Size(5, 5), 0);
      cv.Canny(blurred, edges, 40, 120);
      // A second thresholding derived from the image's own level. Fixed
      // thresholds find the crisp outline of a black-bordered card; these are
      // what pick up the low-contrast boundary of a borderless card against
      // skin or a pale wall. (detect.py uses the median; OpenCV.js has no
      // median reduction and the mean serves the same purpose here.)
      const level = cv.mean(blurred)[0];
      const auto = new cv.Mat();
      cv.Canny(blurred, auto, Math.max(0, 0.66 * level), Math.min(255, 1.33 * level));
      cv.bitwise_or(edges, auto, edges);
      auto.delete();
      if (union) {
        if (union.empty()) edges.copyTo(union);
        else cv.bitwise_or(union, edges, union);
      }
      // One dilation, not two: two closes the gap between a card and the hand
      // holding it, merging them into a blob whose outline is neither.
      cv.dilate(edges, edges, new cv.Mat(), new cv.Point(-1, -1), 1);
      // RETR_LIST, not RETR_EXTERNAL — a borderless card whose edge merges
      // with the background is not an outermost contour.
      cv.findContours(edges, contours, hierarchy, cv.RETR_LIST, cv.CHAIN_APPROX_SIMPLE);

      const ranked = [];
      for (let i = 0; i < contours.size(); i++) {
        ranked.push({ i, area: cv.contourArea(contours.get(i)) });
      }
      ranked.sort((a, b) => b.area - a.area);

      for (const { i, area } of ranked.slice(0, MAX_CONTOURS)) {
        if (area < frameArea * MIN_AREA_FRACTION) break;
        const c = contours.get(i);
        const approx = new cv.Mat();
        cv.approxPolyDP(c, approx, 0.02 * cv.arcLength(c, true), true);
        let quad;
        if (approx.rows === 4) {
          quad = [];
          for (let k = 0; k < 4; k++) {
            quad.push({ x: approx.data32S[k * 2], y: approx.data32S[k * 2 + 1] });
          }
        } else {
          quad = cv.RotatedRect.points(cv.minAreaRect(c)).map((p) => ({ x: p.x, y: p.y }));
        }
        approx.delete();
        const score = scoreQuad(quad, frameArea, grad, gradScale);
        if (score !== null) out.push({ score, quad });
      }
    } finally {
      blurred.delete();
      edges.delete();
      contours.delete();
      hierarchy.delete();
    }
  }

  function toLine(x1, y1, x2, y2) {
    const a = y2 - y1, b = x1 - x2;
    return { a, b, c: a * x1 + b * y1 };
  }

  function intersect(l1, l2) {
    const det = l1.a * l2.b - l2.a * l1.b;
    if (Math.abs(det) < 1e-6) return null;
    return { x: (l2.b * l1.c - l1.b * l2.c) / det, y: (l1.a * l2.c - l2.a * l1.c) / det };
  }

  // Rebuild quads from long straight lines.
  //
  // Contour following needs the card's boundary to be *closed*, and a
  // borderless card against skin has a boundary that is straight but broken —
  // so contours find the text box and the art window, which are closed, and
  // never the card. A Hough transform doesn't care about closure: every
  // fragment votes for the same infinite line, so four broken sides still give
  // four strong lines to intersect.
  function lineQuads(edges, frameArea, out, grad, gradScale) {
    const lines = new cv.Mat();
    try {
      const minLen = Math.round(HOUGH_MIN_LENGTH_FRACTION * Math.min(edges.rows, edges.cols));
      cv.HoughLinesP(edges, lines, 1, Math.PI / 180, 55, minLen, 18);
      if (!lines.rows) return;

      const segs = [];
      for (let i = 0; i < lines.rows; i++) {
        const d = lines.data32S, o = i * 4;
        const x1 = d[o], y1 = d[o + 1], x2 = d[o + 2], y2 = d[o + 3];
        const angle = ((Math.atan2(y2 - y1, x2 - x1) * 180) / Math.PI + 180) % 180;
        segs.push({ x1, y1, x2, y2, angle, len: Math.hypot(x2 - x1, y2 - y1) });
      }

      // Length-weighted dominant direction, on doubled angles so that 1 and
      // 179 degrees — the same direction — average to 0 and not to 90.
      let sx = 0, sy = 0;
      for (const s of segs) {
        const r = (s.angle * 2 * Math.PI) / 180;
        sx += s.len * Math.cos(r);
        sy += s.len * Math.sin(r);
      }
      const dominant = ((((Math.atan2(sy, sx) * 180) / Math.PI) / 2) + 180) % 180;

      const offset = (a, ref) => {
        const d = Math.abs(a - ref) % 180;
        return Math.min(d, 180 - d);
      };
      const family = (ref) =>
        segs
          .filter((s) => offset(s.angle, ref) < 30)
          .sort((a, b) => b.len - a.len)
          .slice(0, HOUGH_MAX_LINES)
          .map((s) => toLine(s.x1, s.y1, s.x2, s.y2));

      const A = family(dominant);
      const B = family((dominant + 90) % 180);

      for (let i = 0; i < A.length; i++) {
        for (let j = i + 1; j < A.length; j++) {
          for (let k = 0; k < B.length; k++) {
            for (let l = k + 1; l < B.length; l++) {
              const c = [
                intersect(A[i], B[k]), intersect(A[i], B[l]),
                intersect(A[j], B[k]), intersect(A[j], B[l]),
              ];
              if (c.some((p) => !p || !isFinite(p.x) || !isFinite(p.y))) continue;
              const quad = [c[0], c[1], c[3], c[2]];
              const score = scoreQuad(quad, frameArea, grad, gradScale);
              if (score !== null) out.push({ score, quad });
            }
          }
        }
      }
    } finally {
      lines.delete();
    }
  }

  // Gradient magnitude, plus the value that should count as full support.
  // detect.py divides by the 99th percentile so one specular highlight can't
  // set the scale for the whole image; OpenCV.js has no percentile reduction,
  // so a multiple of the mean stands in for it — same intent, and the
  // threshold is calibrated against this scale rather than inherited blindly.
  function gradientFor(channel, out) {
    const gx = new cv.Mat(), gy = new cv.Mat(), ax = new cv.Mat(), ay = new cv.Mat();
    try {
      cv.Sobel(channel, gx, cv.CV_16S, 1, 0, 3);
      cv.Sobel(channel, gy, cv.CV_16S, 0, 1, 3);
      cv.convertScaleAbs(gx, ax);
      cv.convertScaleAbs(gy, ay);
      cv.addWeighted(ax, 0.5, ay, 0.5, 0, out);
      return Math.max(8, cv.mean(out)[0] * 5);
    } finally {
      gx.delete(); gy.delete(); ax.delete(); ay.delete();
    }
  }

  function detect(srcMat, frameArea) {
    const gray = new cv.Mat();
    const hsv = new cv.Mat();
    const rgb = new cv.Mat();
    const lab = new cv.Mat();
    const planes = new cv.MatVector();
    const labPlanes = new cv.MatVector();
    const union = new cv.Mat();
    const grad = new cv.Mat();
    const scored = [];

    try {
      cv.cvtColor(srcMat, gray, cv.COLOR_RGBA2GRAY);
      const gradScale = gradientFor(gray, grad);
      quadsFrom(gray, frameArea, scored, union, grad, gradScale);

      // Saturation carries the card when brightness cannot: glare adds white
      // light, which is nearly colourless, so a reflection that erases the
      // card's edges in grey leaves them intact here.
      cv.cvtColor(srcMat, rgb, cv.COLOR_RGBA2RGB);
      cv.cvtColor(rgb, hsv, cv.COLOR_RGB2HSV);
      cv.split(hsv, planes);
      quadsFrom(planes.get(1), frameArea, scored, union, grad, gradScale);

      // Lab a and b separate by hue where brightness and saturation see
      // nothing. A borderless card in the hand is the hard case precisely
      // because it has no dark rim, but skin against green or blue card art is
      // a large clean step in a/b even when the other channels run continuous
      // across the boundary.
      cv.cvtColor(rgb, lab, cv.COLOR_RGB2Lab);
      cv.split(lab, labPlanes);
      quadsFrom(labPlanes.get(1), frameArea, scored, union, grad, gradScale);
      quadsFrom(labPlanes.get(2), frameArea, scored, union, grad, gradScale);

      // Lines are searched over the union of both channels: a card whose left
      // edge only shows in saturation and whose top only shows in brightness
      // still has four sides here, and it is the four together that make it.
      if (!union.empty()) lineQuads(union, frameArea, scored, grad, gradScale);
    } finally {
      gray.delete();
      hsv.delete();
      rgb.delete();
      lab.delete();
      planes.delete();
      labPlanes.delete();
      union.delete();
      grad.delete();
    }

    if (!scored.length) return null;
    scored.sort((a, b) => b.score - a.score);
    return orderCorners(scored[0].quad);
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
