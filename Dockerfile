FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Clone PinnacleExport submodule.
# Requires a PINNACLE_EXPORT_PAT build secret with read access to the repo.
# If the repo is public, replace the RUN block with a plain git clone using the HTTPS URL.
RUN --mount=type=secret,id=pinnacle_token \
    TOKEN=$(cat /run/secrets/pinnacle_token) && \
    rm -rf backend/src/retrieve/PinnacleExport && \
    git clone "https://x-access-token:${TOKEN}@github.com/Christie-Scientific-Computing/PinnacleExport.git" \
    backend/src/retrieve/PinnacleExport
