# -----------------------------------------------------------------------------
# Local Values
# -----------------------------------------------------------------------------
# Centralizes naming conventions, tags, and derived values used across the
# PoC environment. All resources share a common tag set for cost tracking and
# ownership identification.
# -----------------------------------------------------------------------------

locals {
  # Common tags applied to every resource via provider default_tags.
  # Individual resources can add extra tags; these are the baseline.
  common_tags = {
    Project     = "security-lakehouse"
    Environment = "poc"
    ManagedBy   = "terraform"
  }
}
