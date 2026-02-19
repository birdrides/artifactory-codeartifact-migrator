import os
import boto3

# In Docker/K8s the container gets AWS credentials via IRSA (environment variables
# AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE injected by the EKS pod identity webhook).
# Using a named profile in that environment would override IRSA and cause auth failures.
#
# Locally (DOCKER is unset) we use the "bird-svc" named profile from ~/.aws/credentials.
if not os.environ.get("DOCKER"):
    boto3.setup_default_session(profile_name="bird-svc")
