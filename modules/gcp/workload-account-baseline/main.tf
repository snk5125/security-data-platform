# -----------------------------------------------------------------------------
# GCP Workload Account Baseline Module
# -----------------------------------------------------------------------------
# Creates networking and compute infrastructure for a GCP workload:
#   1. VPC network (custom mode — no auto-subnets)
#   2. Subnet with VPC Flow Log collection enabled (log_config block)
#   3. Firewall rules (SSH/RDP inbound — intentionally permissive for demo)
#   4. 2 VMs: Linux (e2-micro) + Windows (e2-medium)
#   5. Cloud Router (for future NAT gateway if needed)
#   6. SSH key pair (via TLS provider, matches AWS/Azure pattern)
#
# Mirrors the Azure workload-account-baseline module. VMs and firewall rules
# are intentionally permissive to generate security events for the pipeline.
#
# VPC Flow Logs: Enabled at the subnet level via log_config. The data-sources
# module creates a log sink to export these from Cloud Logging to GCS.
#
# Prerequisites:
#   - GCP project exists with Compute Engine API enabled
#   - Security foundation applied (APIs enabled)
# -----------------------------------------------------------------------------

# ═════════════════════════════════════════════════════════════════════════════
# 1. NETWORKING — VPC, Subnet, Firewall
# ═════════════════════════════════════════════════════════════════════════════

resource "google_compute_network" "main" {
  project                 = var.project_id
  name                    = "${var.name_prefix}-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "public" {
  project       = var.project_id
  name          = "${var.name_prefix}-subnet-public"
  ip_cidr_range = var.vpc_cidr
  region        = var.region
  network       = google_compute_network.main.id

  # Enable VPC Flow Log collection into Cloud Logging.
  # The data-sources module creates a log sink to export these to GCS.
  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 1.0
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# Firewall — intentionally permissive to generate security events.
# Mirrors AWS/Azure pattern of allowing SSH (22) and RDP (3389) from 0.0.0.0/0.
resource "google_compute_firewall" "allow_ssh_rdp" {
  project = var.project_id
  name    = "${var.name_prefix}-allow-ssh-rdp"
  network = google_compute_network.main.name

  allow {
    protocol = "tcp"
    ports    = ["22", "3389", "5986"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["${var.name_prefix}-vm"]
}

# Allow ICMP for basic connectivity testing.
resource "google_compute_firewall" "allow_icmp" {
  project = var.project_id
  name    = "${var.name_prefix}-allow-icmp"
  network = google_compute_network.main.name

  allow {
    protocol = "icmp"
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["${var.name_prefix}-vm"]
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. CLOUD ROUTER (for future NAT gateway)
# ═════════════════════════════════════════════════════════════════════════════

resource "google_compute_router" "main" {
  project = var.project_id
  name    = "${var.name_prefix}-router"
  region  = var.region
  network = google_compute_network.main.id
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. SSH KEY PAIR — for Linux VM access
# ═════════════════════════════════════════════════════════════════════════════

resource "tls_private_key" "ssh" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

# ═════════════════════════════════════════════════════════════════════════════
# 4. COMPUTE INSTANCES
# ═════════════════════════════════════════════════════════════════════════════

# Linux VM — e2-micro (free-tier eligible), Debian 12.
resource "google_compute_instance" "linux" {
  project      = var.project_id
  name         = "${var.name_prefix}-linux"
  machine_type = "e2-micro"
  zone         = var.zone
  tags         = ["${var.name_prefix}-vm"]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.public.id

    access_config {
      # Ephemeral external IP
    }
  }

  metadata = {
    ssh-keys = "admin:${tls_private_key.ssh.public_key_openssh}"
  }
}

# ── Windows admin password ─────────────────────────────────────────────────
# Random password for the local Administrator account. Set via startup script
# so WinRM/Ansible can authenticate immediately after boot.
resource "random_password" "windows_admin" {
  length  = 20
  special = true
}

# Windows VM — e2-medium (Windows Server requires more resources than e2-micro).
resource "google_compute_instance" "windows" {
  project      = var.project_id
  name         = "${var.name_prefix}-windows"
  machine_type = "e2-medium"
  zone         = var.zone
  tags         = ["${var.name_prefix}-vm"]

  boot_disk {
    initialize_params {
      image = "windows-cloud/windows-2022"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.public.id

    access_config {
      # Ephemeral external IP
    }
  }

  # Bootstrap WinRM HTTPS listener so Ansible can connect immediately.
  # GCP Windows images do NOT enable the built-in Administrator account, and
  # [ADSI]"WinNT://./Administrator,User" fails silently. Instead we create a
  # dedicated "cribl_admin" local user, add it to Administrators, then configure
  # a WinRM HTTPS listener on port 5986 with Basic auth enabled.
  metadata = {
    windows-startup-script-ps1 = <<-STARTUP
      # Create admin user for WinRM/Ansible access.
      # GCP does not enable the built-in Administrator account — we must create
      # our own local admin. Idempotent: skips creation if user already exists.
      $username = "cribl_admin"
      $password = ConvertTo-SecureString "${random_password.windows_admin.result}" -AsPlainText -Force
      if (-not (Get-LocalUser -Name $username -ErrorAction SilentlyContinue)) {
          New-LocalUser -Name $username -Password $password -FullName "Cribl Admin" -PasswordNeverExpires
          Add-LocalGroupMember -Group "Administrators" -Member $username
      }
      Set-LocalUser -Name $username -Password $password

      # Enable WinRM HTTPS with a self-signed certificate
      $cert = New-SelfSignedCertificate -DnsName $env:COMPUTERNAME -CertStoreLocation Cert:\LocalMachine\My
      Get-ChildItem WSMan:\localhost\Listener | Where-Object { $_.Keys -contains "Transport=HTTPS" } | Remove-Item -Recurse -ErrorAction SilentlyContinue
      New-Item -Path WSMan:\localhost\Listener -Transport HTTPS -Address * -CertificateThumbPrint $cert.Thumbprint -Force
      Set-Item WSMan:\localhost\Service\Auth\Basic -Value $true
      Set-Item WSMan:\localhost\Service\AllowUnencrypted -Value $false

      # Open Windows Firewall for WinRM HTTPS
      New-NetFirewallRule -Name "WINRM-HTTPS" -DisplayName "WinRM HTTPS" -Direction Inbound -Protocol TCP -LocalPort 5986 -Action Allow -ErrorAction SilentlyContinue

      # Restart WinRM to apply changes
      Restart-Service WinRM
    STARTUP
  }
}
