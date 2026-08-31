# syntax=docker/dockerfile:1

# ---- Stage 1: front-end assets -----------------------------------------
# Fetching third-party assets and compiling Tailwind happens here so the
# runtime image carries neither curl nor the 100MB Tailwind binary.
FROM python:3.12-slim AS assets

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Only what the asset scripts touch, so editing Python doesn't bust this layer.
COPY fetch-assets.sh build-css.sh ./
COPY app/static/src.css ./app/static/src.css
COPY app/templates ./app/templates
COPY app/static/scan.js ./app/static/scan.js

RUN chmod +x fetch-assets.sh build-css.sh \
    && ./fetch-assets.sh \
    && ./build-css.sh


# ---- Stage 2: runtime ---------------------------------------------------
FROM python:3.12-slim AS runtime

# opencv-python-headless still links against libglib even without a GUI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: they change far less often than the application code.
COPY requirements.txt ./
#
# rapidocr-onnxruntime depends on `opencv-python` — the desktop build, which
# links against X libraries that a slim image has no reason to carry. It
# installs over the same cv2/ directory as opencv-python-headless, so whichever
# lands last wins and `import cv2` fails on libxcb.
#
# Drop the desktop build and reinstate headless afterwards: the two share that
# directory, so uninstalling one takes files the other still needs. rapidocr
# only needs `cv2` importable, which headless satisfies.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y opencv-python \
    && pip install --no-cache-dir --force-reinstall "opencv-python-headless>=4.10"

COPY app ./app

# The compiled stylesheet and vendored assets from stage 1.
COPY --from=assets /build/app/static/ ./app/static/

# Run as a non-root user, and give it ownership of the data directory so the
# named volume is writable when Docker creates it.
RUN useradd --create-home --uid 1000 scanner \
    && mkdir -p /app/data \
    && chown -R scanner:scanner /app
USER scanner

VOLUME ["/app/data"]
EXPOSE 8000

# Fails until the card index is loaded, which is the honest readiness signal:
# the app answers requests but can't match anything before the first sync.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/collection/', timeout=4).status < 400 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
