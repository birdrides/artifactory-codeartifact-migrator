terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.31.0"
    }
    vault = {
      source  = "hashicorp/vault"
      version = ">= 4.4.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.27.0"
    }
  }
  # local-driven config; K8s Job command uses validated ACM flags from run.sh
  required_version = ">= 1.14.0"
}
