FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends bash util-linux && \
    pip install --no-cache-dir flask && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY app.py /app/app.py

WORKDIR /app

EXPOSE 8080

ENTRYPOINT ["python3", "/app/app.py"]
