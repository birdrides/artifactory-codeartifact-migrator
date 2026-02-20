##############################################################################
# job.tf – K8s namespace, ServiceAccount, Secret, and Jobs
#
# All locals (including CLI arg assembly) live in locals.tf.
# All IAM resources (policies, IRSA role) live in iam.tf.
#
# Auth model:
#   • AWS credentials   → IRSA (ServiceAccount annotation → IAM role via iam.tf)
#   • Artifactory creds → kubernetes_secret (sourced from Vault KV at plan time,
#                         stored as a K8s Secret, mounted as env vars in the pod)
#
# Jobs:
#   • kubernetes_job_v1.acm_migration  – Artifactory → CodeArtifact migration
#   • kubernetes_job_v1.s3_archive     – Artifactory → S3 full archive
##############################################################################

# ---------------------------------------------------------------------------
# K8s namespace
# ---------------------------------------------------------------------------

resource "kubernetes_namespace_v1" "acm" {
  metadata {
    name = local.k8s_namespace
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# ---------------------------------------------------------------------------
# K8s ServiceAccount – annotated for IRSA
# ---------------------------------------------------------------------------

resource "kubernetes_service_account_v1" "acm" {
  metadata {
    name      = local.k8s_service_account_name
    namespace = kubernetes_namespace_v1.acm.metadata[0].name

    annotations = {
      "eks.amazonaws.com/role-arn" = module.acm_irsa.arn
    }

    labels = {
      "app.kubernetes.io/name"       = local.name_prefix
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# ---------------------------------------------------------------------------
# K8s Secret – Artifactory credentials
#
# Values are read from Vault at `terraform plan/apply` time via the
# data.vault_kv_secret_v2.artifactory data source in main.tf.
# The secret is stored in the cluster and mounted as env vars in the pod.
# ---------------------------------------------------------------------------

resource "kubernetes_secret_v1" "artifactory_creds" {
  metadata {
    name      = "${local.name_prefix}-artifactory-creds"
    namespace = kubernetes_namespace_v1.acm.metadata[0].name

    labels = {
      "app.kubernetes.io/name"       = local.name_prefix
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  type = "Opaque"

  # data_wo is a write-only attribute that accepts ephemeral values.
  # The Vault provider (>= 4.x) returns vault_kv_secret_v2 data as ephemeral,
  # which cannot be stored in Terraform state via the regular `data` attribute.
  data_wo = {
    ARTIFACTORY_USERNAME = data.vault_kv_secret_v2.artifactory.data["USER"]
    ARTIFACTORY_PASSWORD = data.vault_kv_secret_v2.artifactory.data["API_KEY"]
  }
}

# ---------------------------------------------------------------------------
# K8s Job – Artifactory → CodeArtifact migration
# ---------------------------------------------------------------------------

resource "kubernetes_job_v1" "acm_migration" {
  metadata {
    name      = local.name_prefix
    namespace = kubernetes_namespace_v1.acm.metadata[0].name

    labels = {
      "app.kubernetes.io/name"       = local.name_prefix
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  # Do not block terraform apply waiting for the job to complete.
  # Monitor progress with: kubectl logs -n acm-migration -l app.kubernetes.io/name=acm-migration -f
  wait_for_completion = false

  spec {
    # One-shot migration job – do not restart on failure.
    # Increase backoff_limit if you want automatic retries.
    backoff_limit = 0

    template {
      metadata {
        labels = {
          "app.kubernetes.io/name" = local.name_prefix
        }
      }

      spec {
        service_account_name            = kubernetes_service_account_v1.acm.metadata[0].name
        automount_service_account_token = true
        restart_policy                  = "Never"

        container {
          name  = "acm"
          image = local.default_image_uri

          # CLI flags are assembled in locals.tf (acm_shell_command).
          # /bin/sh -c is required so the shell expands $ARTIFACTORY_USERNAME
          # and $ARTIFACTORY_PASSWORD from the K8s Secret env vars at runtime.
          # command = ["/bin/sh", "-c"]
          # args    = [local.acm_shell_command]

          # Inject Artifactory credentials from the K8s Secret
          env {
            name = "ARTIFACTORY_USERNAME"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.artifactory_creds.metadata[0].name
                key  = "ARTIFACTORY_USERNAME"
              }
            }
          }
          env {
            name = "ARTIFACTORY_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.artifactory_creds.metadata[0].name
                key  = "ARTIFACTORY_PASSWORD"
              }
            }
          }

          env {
            name  = "DOCKER"
            value = "1"
          }
          env {
            name  = "PYTHONUNBUFFERED"
            value = "1"
          }

          # Repo name mapping: Artifactory repo → CodeArtifact repo.
          # Read by get_repo_mapping() in replicator.py via os.environ.get("ACM_REPO_MAPPING").
          env {
            name  = "ACM_REPO_MAPPING"
            value = local.acm_repo_mapping
          }

          # IRSA automatically injects AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE.
          # Set region explicitly so boto3 doesn't need to discover it.
          env {
            name  = "AWS_REGION"
            value = local.codeartifact_region
          }
          env {
            name  = "AWS_DEFAULT_REGION"
            value = local.codeartifact_region
          }
          # Do NOT set AWS_PROFILE here. IRSA works via AWS_ROLE_ARN +
          # AWS_WEB_IDENTITY_TOKEN_FILE injected by the EKS pod identity webhook.
          # Setting AWS_PROFILE="" causes botocore to look for a profile named ""
          # which doesn't exist and raises ProfileNotFound at import time.

          resources {
            requests = {
              cpu    = "1"
              memory = "1Gi"
            }
            limits = {
              cpu    = "2"
              memory = "4Gi"
            }
          }

          # Ephemeral scratch space for the .replication working directory
          volume_mount {
            name       = "replication-scratch"
            mount_path = "/app/.replication"
          }
        }

        volume {
          name = "replication-scratch"
          empty_dir {}
        }
      }
    }
  }

  # Force job replacement on every apply so re-running `terraform apply`
  # re-triggers the migration. Remove this block if you want idempotent applies.
  lifecycle {
    replace_triggered_by = [
      kubernetes_service_account_v1.acm,
    ]
  }
}

