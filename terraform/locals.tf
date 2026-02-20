##############################################################################
# locals.tf – single source of truth for all local values
#
# This stack is intentionally local-driven (no terraform.tfvars required).
# All configuration lives here + Vault/AWS data sources in main.tf.
##############################################################################

locals {
  # ---------------------------------------------------------------------------
  # Naming
  # ---------------------------------------------------------------------------
  name_prefix       = "acm-migration"
  image_tag = "0.0.8"
  default_image_uri = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.region}.amazonaws.com/acm-migration:${local.image_tag}"

  log_retention_days = 14

  # ---------------------------------------------------------------------------
  # Kubernetes
  # ---------------------------------------------------------------------------
  k8s_namespace            = "acm-migration"
  k8s_service_account_name = "acm-migration"


  # ---------------------------------------------------------------------------
  # Artifactory connection
  # ---------------------------------------------------------------------------
  # Hostname only – protocol is passed separately via --artifactoryprotocol
  artifactory_host        = "artifactory.svc.bird.co"
  artifactory_host_prefix = "artifactory"

  # ---------------------------------------------------------------------------
  # AWS CodeArtifact
  # ---------------------------------------------------------------------------
  codeartifact_domain  = "bird"
  codeartifact_account = data.aws_caller_identity.current.account_id
  codeartifact_region  = data.aws_region.current.region

  codeartifact_domain_arn          = "arn:aws:codeartifact:${local.codeartifact_region}:${local.codeartifact_account}:domain/${local.codeartifact_domain}"
  codeartifact_repository_wildcard = "arn:aws:codeartifact:${local.codeartifact_region}:${local.codeartifact_account}:repository/${local.codeartifact_domain}/*"
  codeartifact_package_wildcard    = "arn:aws:codeartifact:${local.codeartifact_region}:${local.codeartifact_account}:package/${local.codeartifact_domain}/*"

  # ---------------------------------------------------------------------------
  # Migration scope
  # Repositories to migrate (space-separated list passed to --repositories).
  # Set to [] to replicate all repositories.
  # ---------------------------------------------------------------------------
  artifactory_repositories = ["internal"]

  # ---------------------------------------------------------------------------
  # Specific packages to migrate (space-separated list passed to --packages).
  # Set to [] to replicate all packages in the repository.
  # Used here to target Gradle plugin marker artifacts that were skipped during
  # the full repo migration.
  # ---------------------------------------------------------------------------
  artifactory_packages = [
    "co/bird/gradle/clientmodule/co.bird.gradle.clientmodule.gradle.plugin",
    "co/bird/gradle/dependency/co.bird.gradle.dependency.gradle.plugin",
    "co/bird/gradle/deployable/co.bird.gradle.deployable.gradle.plugin",
    "co/bird/gradle/factoring/co.bird.gradle.factoring.gradle.plugin",
    "co/bird/gradle/flyway/co.bird.gradle.flyway.gradle.plugin",
    "co/bird/gradle/metrics/co.bird.gradle.metrics.gradle.plugin",
    "co/bird/gradle/version/co.bird.gradle.version.gradle.plugin",
  ]

  # ---------------------------------------------------------------------------
  # Repo name mapping: Artifactory repo → CodeArtifact repo
  # Format: "artifactory_repo:codeartifact_repo,..."
  # Set to null to disable (repo names are used as-is).
  # ---------------------------------------------------------------------------
  acm_repo_mapping = "internal:maven-private,npm-local:npm-private,bird-pip:pypi-private"

  # ---------------------------------------------------------------------------
  # ACM runtime flags – mirror the env vars consumed by run.sh
  #
  # Toggle acm_dryrun to switch between dryrun and production:
  #   true  → no real publishing, no cache/DynamoDB
  #   false → real publishing, cache enabled, DynamoDB used as cache backend
  # ---------------------------------------------------------------------------
  acm_dryrun = false # ← set to false for production run

  acm_verbose = true  # enable INFO-level logs (shows package/version progress)
  acm_debug   = false # set to true for full debug output (very verbose)
  acm_clean   = false
  acm_refresh = false
  acm_output  = null # set to a file path string to redirect logs to a file
  acm_procs   = null # set to an integer string to override default parallelism

  # Cache and DynamoDB are automatically enabled for production runs only.
  # In K8s, SQLite cache is useless (pod is ephemeral), so DynamoDB is required.
  acm_cache    = true
  acm_dynamodb = true
  # acm_dynamodb = !local.acm_dryrun

  # ---------------------------------------------------------------------------
  # CLI args assembly (mirrors run.sh flag construction)
  #
  # Artifactory credentials are injected as env vars by the K8s Secret
  # (kubernetes_secret.artifactory_creds in job.tf).
  #
  # The container runs via `/bin/sh -c "<shell_command>"` so that the shell
  # expands $ARTIFACTORY_USERNAME / $ARTIFACTORY_PASSWORD at runtime.
  # ---------------------------------------------------------------------------

  # Static flags (no shell expansion needed)
  _acm_args_static = join(" ", compact([
    "--artifactoryhost ${local.artifactory_host}",
    "--artifactoryprefix ${local.artifactory_host_prefix}",
    "--artifactoryprotocol https",
    # Credentials expanded by the shell at runtime from K8s Secret env vars
    "--artifactoryuser $ARTIFACTORY_USERNAME",
    "--artifactorypass $ARTIFACTORY_PASSWORD",
    "--codeartifactdomain ${local.codeartifact_domain}",
    "--codeartifactaccount ${local.codeartifact_account}",
    "--codeartifactregion ${local.codeartifact_region}",
    length(local.artifactory_repositories) > 0 ? "--repositories ${join(" ", local.artifactory_repositories)}" : "",
    length(local.artifactory_packages) > 0 ? "--packages '${join(" ", local.artifactory_packages)}'" : "",
    local.acm_dryrun ? "--dryrun" : "",
    local.acm_verbose ? "-v" : "",
    local.acm_debug ? "--debug" : "",
    local.acm_cache ? "--cache" : "",
    local.acm_clean ? "--clean" : "",
    local.acm_refresh ? "--refresh" : "",
    local.acm_dynamodb ? "--dynamodb" : "",
    local.acm_output != null ? "--output ${local.acm_output}" : "",
    local.acm_procs != null ? "--procs ${local.acm_procs}" : "",
  ]))

  # Full shell command passed to /bin/sh -c
  acm_shell_command = "artifactory-codeartifact-migrator ${local._acm_args_static}"



  # ---------------------------------------------------------------------------
  # Resource tags
  # ---------------------------------------------------------------------------
  tags = {
    Project   = "acm-migration"
    ManagedBy = "terraform"
  }
}
