FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY marshal_ab ./marshal_ab
# Settings come from the environment: FLAGSMITH_API_URL, FLAGSMITH_API_KEY, EXPERIMENT_ENV_KEY, EXPERIMENT_ID, MARSHAL_WAREHOUSE_DSN
EXPOSE 8765
CMD ["python", "-m", "marshal_ab", "serve", "--host", "0.0.0.0"]
