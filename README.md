# Artifactory CodeArtifact Migrator (ACM)
## _Making it easy to move from Artifactory to AWS CodeArtifact_

Artifactory CodeArtifact Migrator (ACM) is a tool which enables you to easily move all
your artifacts from Artifactory to AWS CodeArtifact.

## Features

- Migrate packages individually either all versions or specify versions
- Migrate single or multiple repositories
- Migrate the entire Artifactory system
- Dryrun capabilities

## Installation

ACM requires Python 3.7 <= version < 3.13 to run. 

Using pipenv is the recommended way to install and run ACM.

Install the dependencies:

```sh
pipenv install
```

Install ACM:

```sh
pipenv run python setup.py install
```

Run ACM:

```sh
pipenv run artifactory-codeartifact-migrator -h
```

To simplify run operations, copy env.sh.template to env.sh, modify it per your
settings, and then execute run.sh instead.

Alternatively, you can install the required dependencies natively with pip, and
run the program using your locally installed python:

```sh
pip install -r requirements.txt
python setup.py install
artifactory-codeartifact-migrator -h
```

## Caching

ACM can cache requests and publishing status for packages and repositories with
the `--cache` option. This is handy in case there's communication issues. You
will not have to start all over again with fetching packages and publishing and
can just use the cache from the previous run.

For dryruns, `acm-dryrun.db` sqlite database will be used in the `.replication`
folder.

For production runs, `acm-prod.db` sqlite database will be used in the
`.replication` folder.

You can manage the databases manually with sqlite if you must.

If you have new packages added to Artifactory since the last run, and wish to
refresh the cached packages that were fetched, use `--refresh` option.

If you wish to start over with a clean cache use the `--clean` option.

Options `--refresh` or `--clean` will not do anything without the `--cache`
option set.

## DynamoDB

For a small amount of repositories, using the local sqlite caching is fine. 
However, if you're moving a lot of artifacts you may want to employ the power of
DynamoDB for rapid i/o and other features. We've included an option you can use:
`--dynamodb` which automatically creates DynamoDB tables on the same account 
your CodeArtifact exists. Keep in mind you should pay attention to permissions 
for the AWS account being used for the migrator.

If using `--dryrun` all DynamoDB options would happen on the following tables:

artifactory-codeartifact-migrator-dryrun-packages
artifactory-codeartifact-migrator-dryrun-repositories

Otherwise, the production DynamoDB tables will be:

artifactory-codeartifact-migrator-prod-packages
artifactory-codeartifact-migrator-prod-repositories

Keep in mind, the `--dynamodb` parameter will not work without the `--cache`
parameter specified in command line.

## Performance

ACM makes a lot of API calls to Artifactory and CodeArtifact.

We've included multi process option `--procs` to allow you to specify the
number of processes to use for all API operations. We've tested this up to 100
processes so far successfully in a Kubernetes cluster and found the migration
for millions of artifacts to go smoothly and quite fast.

There is also a known issue specifically with npm metadata. The way Artifactory
handles package queries with npm, is that it locks mysql repeatedly for every
single version of a package found. If there are a very large number of versions
of that artifact it could cause delays.

This can mean searching and executing can take some time between packages.

## Session

For small repositories or specific package replication, an AWS session is fine.
However, if you have a very large replication load you may find that the default
AWS codeartifact token refresh may be impacted by your session expiration time.
For those instances, it's recommended you use a service account with permanent
access key, or a permanent role for your instance or clusters to prevent such
token generation from failing eventually.

## Connectivity

If you have very large Artifactory repositories, and you are running this from
a local system which uses VPN and expires every day or so, you may want to
consider running this from a server unaffected by such restrictions.

## Cost Considerations

If your Artifactory system is already in AWS EC2 or Kubernetes,
it may pay for you to use a spot EC2 instance in AWS or even a Kubernetes
cronjob and run the migrator there, as you don't pay for internal AWS traffic.

## Running on EKS with Terraform

The `terraform/` directory contains a ready-made stack that builds a Docker
image, pushes it to ECR, and runs ACM as a one-shot Kubernetes Job on an
existing EKS cluster.  AWS credentials are handled entirely through **IRSA**
(IAM Roles for Service Accounts) so no long-lived keys are needed inside the
cluster.

### Architecture overview

```
┌─────────────────────────────────────────────────────────────┐
│  Developer workstation                                       │
│  make build-push  →  ECR (acm-migration:<version>)          │
│  terraform apply  →  EKS Job (acm-migration namespace)      │
└─────────────────────────────────────────────────────────────┘
         │                          │
         ▼                          ▼
   ECR image pull            IRSA IAM role
                         (CodeArtifact + DynamoDB)
                                    │
                         K8s Secret (Artifactory creds
                          sourced from Vault at plan time)
```

| Terraform file | Purpose |
|---|---|
| [`terraform/locals.tf`](terraform/locals.tf) | Single source of truth – all tuneable values live here |
| [`terraform/main.tf`](terraform/main.tf) | Providers (AWS, Vault, Kubernetes), S3 backend, data sources |
| [`terraform/ecr.tf`](terraform/ecr.tf) | ECR repository for the ACM Docker image |
| [`terraform/job.tf`](terraform/job.tf) | IAM policy, IRSA role, K8s namespace/ServiceAccount/Secret/Job |
| [`terraform/versions.tf`](terraform/versions.tf) | Provider and Terraform version constraints |

### Prerequisites

- Terraform ≥ 1.14
- An existing EKS cluster with an OIDC provider configured
- AWS CLI profile with permissions to create IAM roles and ECR repositories
- `kubectl` configured to reach the target cluster
- (Optional) HashiCorp Vault for Artifactory credential injection

### Step 1 – Build and push the Docker image

```sh
# Authenticate Docker to ECR (replace <account-id> and <region> as needed)
aws --region us-west-2 --profile bird-prod ecr get-login-password | docker login --username AWS --password-stdin 168995956934.dkr.ecr.us-west-2.amazonaws.com

# Build, tag, and push (reads version from version.json)
make build-push
```

The `Makefile` target:
1. Builds a `linux/amd64` image from the [`Dockerfile`](Dockerfile).
2. Tags it `<account-id>.dkr.ecr.<region>.amazonaws.com/acm-migration:<version>`.
3. Pushes it to ECR.

After pushing, keep [`terraform/locals.tf`](terraform/locals.tf) in sync:

```sh
make update-image-tag   # updates image_tag in locals.tf to match version.json
```

### Step 2 – Configure the migration in `locals.tf`

All tuneable values are in [`terraform/locals.tf`](terraform/locals.tf).
Edit the file directly – no `terraform.tfvars` is required.

| Local | Default | Description |
|---|---|---|
| `name_prefix` | `"acm-migration"` | Prefix for all AWS and K8s resources |
| `image_tag` | `"0.0.2"` | ECR image tag to deploy |
| `k8s_namespace` | `"acm-migration"` | Kubernetes namespace for the Job |
| `artifactory_host` | _(your host)_ | Artifactory hostname (no protocol) |
| `codeartifact_domain` | _(your domain)_ | Target CodeArtifact domain name |
| `artifactory_repositories` | `["internal"]` | Repos to migrate; `[]` = all repos |
| `acm_dryrun` | `true` | `true` = dry-run, `false` = live migration |
| `acm_verbose` | `true` | Enable INFO-level log output |
| `acm_procs` | `null` | Parallelism override (e.g. `"50"`) |
| `acm_cache` | `true` | Enable caching |
| `acm_dynamodb` | `true` | Use DynamoDB as the cache backend |

> **Tip:** Start with `acm_dryrun = true` to validate the configuration before
> committing to a live migration.

### Step 3 – Initialize and apply Terraform

```sh
cd terraform

# Initialise providers and remote state backend
terraform init

# (Optional) use a workspace to isolate state per environment
terraform workspace select svc-us-west-2   # or `new` to create one

# Preview the plan
terraform plan

# Apply – creates ECR repo, IAM role, K8s namespace/SA/Secret/Job
terraform apply
```

`terraform apply` does **not** wait for the Job to finish
(`wait_for_completion = false`).  Monitor progress with:

```sh
kubectl logs -n acm-migration \
  -l app.kubernetes.io/name=acm-migration \
  --follow
```

### Step 4 – Re-running the migration

Every `terraform apply` replaces the Job (via `replace_triggered_by` on the
ServiceAccount), so re-running the migration is as simple as:

```sh
terraform apply
```

To run a fresh migration without replacing infrastructure, delete the Job
manually and re-apply:

```sh
kubectl delete job acm-migration -n acm-migration
terraform apply
```

### IAM permissions granted to the Job

The IRSA role attached to the K8s ServiceAccount is granted the following
permissions (see [`terraform/job.tf`](terraform/job.tf)):

| Service | Actions |
|---|---|
| STS | `GetServiceBearerToken` (scoped to `codeartifact.amazonaws.com`) |
| CodeArtifact | `GetAuthorizationToken`, `GetRepositoryEndpoint`, `PublishPackageVersion`, `ListPackages`, `ListPackageVersions`, `DescribePackageVersion`, `DescribeRepository`, `CreateRepository`, `DeletePackageVersions`, `UpdatePackageVersionsStatus`, `PutPackageMetadata`, `ReadFromRepository`, `ListRepositories` |
| DynamoDB | `CreateTable`, `DeleteTable`, `DescribeTable`, `GetItem`, `PutItem`, `UpdateItem`, `Query`, `Scan` (scoped to `artifactory-codeartifact-migrator-*` tables) |

### Credential injection

**AWS credentials** are provided automatically by IRSA – the EKS pod identity
webhook injects `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` into the pod.
The `DOCKER=1` environment variable tells ACM to skip named-profile setup and
rely on these injected credentials.

**Artifactory credentials** are read from Vault at `terraform plan/apply` time
via `data.vault_kv_secret_v2.artifactory` (path `kv/services/jenkins/artifactory`,
keys `USER` and `API_KEY`) and stored as a write-only Kubernetes Secret.  The
Secret is mounted as `ARTIFACTORY_USERNAME` / `ARTIFACTORY_PASSWORD` environment
variables in the Job pod.

If you are not using Vault, replace the `data.vault_kv_secret_v2.artifactory`
data source in [`terraform/main.tf`](terraform/main.tf) with another secret
source (e.g. AWS Secrets Manager) and update the `data_wo` block in
[`terraform/job.tf`](terraform/job.tf:178) accordingly.

### Switching between dry-run and production

```hcl
# terraform/locals.tf

acm_dryrun = false   # ← flip to false for a live migration run
```

Then re-apply:

```sh
terraform apply
```

When `acm_dryrun = false`, ACM publishes packages for real and uses the
DynamoDB tables `artifactory-codeartifact-migrator-prod-packages` and
`artifactory-codeartifact-migrator-prod-repositories` as its cache backend.

## Development

Want to contribute? Great!

We recommend using the --dryrun option to validate your code executes as desired
and test on a real CodeArtifact instance for success.

Please update the version in __init__.py and tag a release when updating, based on semver.

## License

Apache License

**Free Software**
