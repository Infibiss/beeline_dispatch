FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home app
COPY data/demo-road/ ./data/demo-road/
USER app
EXPOSE 10000
CMD ["python", "-m", "field_router.cloud_demo"]
