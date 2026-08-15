FROM python:3.12-slim

# Install OpenSSH client so Python can execute SSH commands to remote DGX nodes
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy remaining codebase
COPY . .

EXPOSE 5001

CMD ["python3", "dgx-orchestrator.py", "daemon", "--port", "5001"]
