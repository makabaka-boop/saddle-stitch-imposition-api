# Saddle-stitched booklet imposition — Python 3.12 runtime image.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY pytest.ini ./pytest.ini

EXPOSE 8000

# Default command runs the API; the one-off verify service overrides it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
