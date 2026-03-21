# Provider Configuration — GCP Workload Root
# Same project as the security foundation.

provider "google" {
  project = var.project_id
  region  = var.region
}
