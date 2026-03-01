FROM python:3.11-slim

# System deps + Node.js 20 for Claude Code CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        fonts-dejavu-core \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user matching host uid=1000 (avoids --dangerously-skip-permissions root restriction)
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Create runtime directories and set ownership
RUN mkdir -p charts data && chown -R appuser:appuser /app

# Make scripts executable
RUN chmod +x script/long.py script/short.py

USER appuser

EXPOSE 8080

CMD ["python", "main.py"]
