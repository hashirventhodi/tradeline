---
status: accepted
date: 2026-10-21
---
# One EC2 instance in AWS Mumbai with a static IP, Docker Compose, Terraform, SSM

SEBI's 2026 algo framework requires API orders to originate from a static IP whitelisted with the broker, so the deployment is one EC2 instance in ap-south-1 with an Elastic IP. Services run under Docker Compose; Postgres runs in a container with nightly backups to S3; secrets live in SSM Parameter Store and are read through the instance role, never from files on disk. Terraform describes the whole stack so it can be recreated with one command, and GitHub Actions deploys on tag. ECS, Kubernetes, serverless, and managed databases were rejected as cost and complexity without benefit at one operator and a few orders a day. Running from the Operator's laptop was rejected because the IP is not static and the machine is not always on.
