FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*
COPY ops/parser-requirements.lock /tmp/parser-requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/parser-requirements.lock
WORKDIR /app
COPY folio/parser.py folio/formats.py folio/intake_limits.py /app/folio/
RUN touch /app/folio/__init__.py
ENV PYTHONDONTWRITEBYTECODE=1 OMP_THREAD_LIMIT=1
USER 65534:65534
ENTRYPOINT ["python", "-m", "folio.formats"]
