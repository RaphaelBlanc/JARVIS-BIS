FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY cloud/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home cloud && mkdir /data && chown cloud:cloud /data
COPY cloud /app/cloud
USER cloud
EXPOSE 8091
CMD ["uvicorn", "cloud.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8091", "--no-access-log", "--limit-concurrency", "32"]
