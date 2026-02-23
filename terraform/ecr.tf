module "ecr_acm_migration" {
  source  = "boring-registry.svc.bird.co/bird/ecr-repo/aws"
  version = "0.3.0"

  providers = {
    aws.svc = aws.bird-svc
  }

  repo_name = local.name_prefix
}
