# Headless Linux image with Chromium and all system libraries already installed.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY harvest.py cases.yaml ./
# Output and logs land in /app/output; mount it to keep results between runs.
ENTRYPOINT ["python", "harvest.py"]
