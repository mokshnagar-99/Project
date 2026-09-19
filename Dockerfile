FROM python:3.12-slim

# Install git for git_publisher and system tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source files
COPY . .

# Create generated directory mount point
RUN mkdir -p /app/generated

EXPOSE 8000 9090

CMD ["python", "orchestrator.py"]
