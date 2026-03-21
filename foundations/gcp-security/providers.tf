# Provider Configuration — GCP Security Foundation
# Uses gcloud application-default credentials by default. For CI/CD, set
# GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_CREDENTIALS environment variables.

provider "google" {
  project = var.project_id
  region  = var.region
}
