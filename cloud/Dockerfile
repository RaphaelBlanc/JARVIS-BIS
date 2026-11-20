FROM docker.io/library/python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY cloud/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --no-deps -r requirements.lock && pip check && useradd --uid 10001 --create-home cloud && mkdir /data && chown cloud:cloud /data
COPY cloud /app/cloud
USER cloud
EXPOSE 8091
CMD ["uvicorn", "cloud.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8091", "--no-access-log", "--limit-concurrency", "32"]
