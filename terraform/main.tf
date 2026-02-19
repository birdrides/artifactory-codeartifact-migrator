##############################################################################
# main.tf – providers, backend, data sources, and shared module
#
# All locals live in locals.tf.
# All K8s / IAM / Vault resources live in job.tf.
##############################################################################

terraform {
  backend "s3" {
    bucket       = "bird-terraform-states"
    key          = "instantiations/acm-migration/terraform"
    region       = "us-west-2"
    encrypt      = true
    profile      = "bird-svc"
    use_lockfile = true
  }
}

# ---------------------------------------------------------------------------
# Remote state – pull shared infrastructure outputs (VPC, EKS cluster, etc.)
# ---------------------------------------------------------------------------

data "terraform_remote_state" "svc" {
  backend = "s3"

  config = {
    bucket       = "bird-terraform-states"
    key          = "env:/svc-us-west-2/instantiations/bird-svc"
    region       = "us-west-2"
    profile      = "bird-svc"
    use_lockfile = true
  }
}

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

provider "aws" {
  alias   = "bird-svc"
  profile = "bird-svc"
  region  = "us-west-2"
}

provider "vault" {
  address = "https://vault.svc.bird.co"
}

provider "kubernetes" {
  config_context = data.terraform_remote_state.svc.outputs.aws_eks_cluster-default.arn
  config_path    = "~/.kube/config"
}

# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

data "aws_caller_identity" "current" {
  provider = aws.bird-svc
}

data "aws_region" "current" {
  provider = aws.bird-svc
}

# Vault KV v2 – Artifactory credentials
# Path: kv/services/jenkins/artifactory  (keys: USER, API_KEY)
data "vault_kv_secret_v2" "artifactory" {
  mount = "kv"
  name  = "services/jenkins/artifactory"
}

# ECR repository – created by module.ecr_acm_migration in ecr.tf
# Looked up here so outputs.tf can reference the repository URL.
data "aws_ecr_repository" "acm_migration" {
  provider = aws.bird-svc
  name     = local.name_prefix

  depends_on = [module.ecr_acm_migration]
}

# ---------------------------------------------------------------------------
# Shared module
# ---------------------------------------------------------------------------

module "generic_data" {
  source = "boring-registry.svc.bird.co/bird/generic-data/generic"
}
