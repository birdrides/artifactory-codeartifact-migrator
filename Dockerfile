# Use official Python image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install minimal system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY . /app

# Install dependencies and package
RUN pip install --no-cache-dir --upgrade pip && \
    if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi && \
    pip install --no-cache-dir .

# Keep compatibility if run.sh is present
RUN chmod +x run.sh || true

# DOCKER=1 disables the boto3 named-profile setup in boto_setup.py so that
# IRSA (AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE) is used instead.
ENV DOCKER=1
ENV PYTHONUNBUFFERED=1

# Runtime flags are passed by the K8s Job spec (see terraform/job.tf).
ENTRYPOINT ["artifactory-codeartifact-migrator"]
CMD ["-h"]
