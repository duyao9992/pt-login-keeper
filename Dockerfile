ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    APP_HOST=0.0.0.0 \
    APP_PORT=9199

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

VOLUME ["/config"]
EXPOSE 9199

CMD ["python", "/app/app.py"]
