FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SEMANTIC_CACHE_DIR=/opt/spec2500-models

WORKDIR /app

COPY requirements.txt .
# Use prebuilt wheels, including bundled PostgreSQL and XML libraries.
# No compiler toolchain or Debian development packages are required.
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='/opt/spec2500-models', threads=2)"

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
