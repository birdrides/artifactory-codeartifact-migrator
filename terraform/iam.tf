##############################################################################
# iam.tf – IAM policies and IRSA role
#
# All IAM resources for the ACM migration stack live here.
# The IRSA role is shared by both the migration job and the s3-archive job.
##############################################################################

# ---------------------------------------------------------------------------
# OIDC provider – look up the existing provider created by the svc stack
# ---------------------------------------------------------------------------

data "aws_eks_cluster" "default" {
  provider = aws.bird-svc
  name     = "default-eks-${terraform.workspace}"
}

data "aws_iam_openid_connect_provider" "eks" {
  provider = aws.bird-svc
  url      = data.aws_eks_cluster.default.identity[0].oidc[0].issuer
}

# ---------------------------------------------------------------------------
# IAM policy – CodeArtifact + DynamoDB access (migration job)
# ---------------------------------------------------------------------------

resource "aws_iam_policy" "acm_codeartifact" {
  provider    = aws.bird-svc
  name        = "${local.name_prefix}-codeartifact"
  description = "Allows the ACM migration job to publish packages to CodeArtifact and use DynamoDB for caching"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "STSServiceBearerToken"
        Effect   = "Allow"
        Action   = ["sts:GetServiceBearerToken"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "sts:AWSServiceName" = "codeartifact.amazonaws.com"
          }
        }
      },
      {
        Sid    = "CodeArtifactDomainAccess"
        Effect = "Allow"
        Action = [
          "codeartifact:GetAuthorizationToken",
          "codeartifact:GetRepositoryEndpoint",
        ]
        Resource = [local.codeartifact_domain_arn]
      },
      {
        Sid    = "CodeArtifactRepositoryAccess"
        Effect = "Allow"
        Action = [
          "codeartifact:GetRepositoryEndpoint",
          "codeartifact:ReadFromRepository",
          "codeartifact:ListPackages",
          "codeartifact:ListPackageVersions",
          "codeartifact:DescribeRepository",
          "codeartifact:CreateRepository",
        ]
        Resource = [local.codeartifact_repository_wildcard]
      },
      {
        # Package-level actions require the package ARN resource type
        Sid    = "CodeArtifactPackageAccess"
        Effect = "Allow"
        Action = [
          "codeartifact:PublishPackageVersion",
          "codeartifact:PutPackageMetadata",
          "codeartifact:DescribePackageVersion",
          "codeartifact:DeletePackageVersions",
          "codeartifact:UpdatePackageVersionsStatus",
        ]
        Resource = [local.codeartifact_package_wildcard]
      },
      {
        # ListRepositories is a list operation that requires Resource: "*"
        Sid      = "CodeArtifactListRepositories"
        Effect   = "Allow"
        Action   = ["codeartifact:ListRepositories"]
        Resource = "*"
      },
      # DynamoDB access for --dynamodb cache mode (both migration and s3-archive jobs)
      {
        Sid    = "DynamoDBCacheAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:CreateTable",
          "dynamodb:DeleteTable",
          "dynamodb:DescribeTable",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = [
          "arn:aws:dynamodb:${local.codeartifact_region}:${local.codeartifact_account}:table/artifactory-codeartifact-migrator-*",
          "arn:aws:dynamodb:${local.codeartifact_region}:${local.codeartifact_account}:table/artifactory-s3-archive-*",
        ]
      },
    ]
  })

  tags = local.tags
}

# ---------------------------------------------------------------------------
# IAM policy – S3 archive bucket access (s3-archive job)
# ---------------------------------------------------------------------------

# resource "aws_iam_policy" "acm_s3_archive" {
#   provider    = aws.bird-svc
#   name        = "${local.name_prefix}-s3-archive"
#   description = "Allows the S3 archive job to write artifacts to the Artifactory archive bucket"

#   policy = jsonencode({
#     Version = "2012-10-17"
#     Statement = [
#       {
#         Sid    = "S3ArchiveBucketAccess"
#         Effect = "Allow"
#         Action = [
#           "s3:PutObject",
#           "s3:GetObject",
#           "s3:HeadObject",
#           "s3:ListBucket",
#         ]
#         Resource = [
#           "arn:aws:s3:::${local.s3_archive_bucket}",
#           "arn:aws:s3:::${local.s3_archive_bucket}/*",
#         ]
#       },
#     ]
#   })

#   tags = local.tags
# }

# ---------------------------------------------------------------------------
# IRSA – IAM role for the K8s ServiceAccount (via community module)
#
# This single role is shared by both the migration job and the s3-archive job
# because they run under the same K8s ServiceAccount.
# ---------------------------------------------------------------------------

module "acm_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  version = "6.4.0"

  providers = {
    aws = aws.bird-svc
  }

  name            = "${local.name_prefix}-irsa"
  use_name_prefix = false

  policies = {
    acm        = aws_iam_policy.acm_codeartifact.arn
    # s3_archive = aws_iam_policy.acm_s3_archive.arn
  }

  oidc_providers = {
    main = {
      provider_arn               = data.aws_iam_openid_connect_provider.eks.arn
      namespace_service_accounts = ["${local.k8s_namespace}:${local.k8s_service_account_name}"]
    }
  }

  tags = local.tags

  depends_on = [
    aws_iam_policy.acm_codeartifact,
    # aws_iam_policy.acm_s3_archive,
  ]
}
