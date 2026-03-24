# -----------------------------------------------------------------------------
# Workload Account Baseline Module
# -----------------------------------------------------------------------------
# Deploys a VPC with public networking, a permissive security group, and two
# EC2 instances (Linux + Windows) into a workload account. These instances
# exist to generate security events (CloudTrail API calls, VPC Flow Logs,
# GuardDuty findings) that feed the security data lakehouse.
#
# This module is invoked once per workload account with a different provider
# alias. Terraform does not support for_each on provider aliases, so the root
# module declares one module block per account.
#
# Prerequisites:
#   - Phase 2 complete (provider aliases verified)
#   - OrganizationAccountAccessRole exists in the target account
#
# Resources created: 9 per invocation
# -----------------------------------------------------------------------------

locals {
  name_prefix = "lakehouse-${var.account_alias}"

  module_tags = merge(var.tags, {
    Component = "workload-account-baseline"
    Account   = var.account_alias
  })
}

# ═════════════════════════════════════════════════════════════════════════════
# 1. VPC AND NETWORKING
# ═════════════════════════════════════════════════════════════════════════════
# A simple single-subnet VPC with internet access. This is sufficient for a
# demo — production would use private subnets with NAT gateways.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-vpc"
  })
}

# ── Public subnet ───────────────────────────────────────────────────────────
# Single subnet in one AZ. Instances get public IPs automatically so they can
# reach the internet (and generate interesting CloudTrail/Flow Log events).
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-public-subnet"
  })
}

# ── Internet gateway ───────────────────────────────────────────────────────
# Required for public internet access from the subnet.
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-igw"
  })
}

# ── Route table ─────────────────────────────────────────────────────────────
# Default route sends all traffic to the internet gateway.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-public-rt"
  })
}

# Associate the route table with the public subnet.
resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. SECURITY GROUP
# ═════════════════════════════════════════════════════════════════════════════
# Intentionally permissive — allows SSH (22) and RDP (3389) from anywhere.
# This is a DEMO configuration designed to trigger GuardDuty findings and
# Config rule violations. Production environments should restrict sources.

resource "aws_security_group" "permissive" {
  name        = "${local.name_prefix}-permissive-sg"
  description = "Demo SG: SSH and RDP open to 0.0.0.0/0 (intentionally permissive for security event generation)"
  vpc_id      = aws_vpc.main.id

  # SSH access from anywhere — triggers GuardDuty and Config findings.
  ingress {
    description = "SSH from anywhere (demo only)"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # RDP access from anywhere — triggers GuardDuty and Config findings.
  ingress {
    description = "RDP from anywhere (demo only)"
    from_port   = 3389
    to_port     = 3389
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # WinRM HTTPS — required for Ansible management of Windows instances.
  # Intentionally open to 0.0.0.0/0 for demo; production should restrict.
  ingress {
    description = "WinRM HTTPS from anywhere (demo - intentionally permissive for Ansible)"
    from_port   = 5986
    to_port     = 5986
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Allow all outbound traffic so instances can reach AWS APIs and generate
  # CloudTrail events.
  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-permissive-sg"
  })
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. SSH KEY PAIR
# ═════════════════════════════════════════════════════════════════════════════
# Generates an SSH key pair for the Linux instance. The private key is stored
# in Terraform state only — not written to disk. This is acceptable for a
# short-lived demo; production should use AWS Systems Manager Session Manager.

resource "tls_private_key" "ssh" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "main" {
  key_name   = "${local.name_prefix}-key"
  public_key = tls_private_key.ssh.public_key_openssh

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-key"
  })
}

# ═════════════════════════════════════════════════════════════════════════════
# 4. AMI DATA SOURCES
# ═════════════════════════════════════════════════════════════════════════════
# Dynamic AMI lookup ensures we always get the latest base images without
# hardcoding region-specific AMI IDs.

# Amazon Linux 2023 — latest x86_64 AMI owned by Amazon.
data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

# Windows Server 2022 — latest base AMI owned by Amazon.
data "aws_ami" "windows" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["Windows_Server-2022-English-Full-Base-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ═════════════════════════════════════════════════════════════════════════════
# 5. EC2 INSTANCES
# ═════════════════════════════════════════════════════════════════════════════
# Two instances per account generate a variety of security events:
#   - CloudTrail: API calls from instance metadata, SSM, etc.
#   - VPC Flow Logs: network traffic from/to the instances
#   - GuardDuty: findings from permissive security groups, public IPs
#   - Config: rule evaluations against the instances and their configuration

# ── Linux instance ──────────────────────────────────────────────────────────
resource "aws_instance" "linux" {
  ami                    = data.aws_ami.amazon_linux.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.permissive.id]
  key_name               = aws_key_pair.main.key_name

  # Enable detailed monitoring for richer CloudWatch metrics (small extra cost).
  monitoring = true

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-linux"
    OS   = "amazon-linux-2023"
  })
}

# ── Windows admin password ─────────────────────────────────────────────────
# Random password for the local Administrator account. Set via EC2 user_data
# so WinRM/Ansible can authenticate immediately after boot.
resource "random_password" "windows_admin" {
  length  = 20
  special = true
}

# ── Windows instance ────────────────────────────────────────────────────────
resource "aws_instance" "windows" {
  ami                    = data.aws_ami.windows.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.permissive.id]

  # Windows instances don't use SSH key pairs — RDP password is retrieved via
  # the AWS console or CLI using the key pair. We associate the key anyway so
  # the password can be decrypted if needed.
  key_name = aws_key_pair.main.key_name

  # Enable detailed monitoring for richer CloudWatch metrics.
  monitoring = true

  # Bootstrap WinRM HTTPS listener so Ansible can connect immediately.
  # Sets the Administrator password, creates a self-signed TLS certificate,
  # configures a WinRM HTTPS listener on port 5986, enables NTLM auth,
  # and opens the Windows Firewall for inbound WinRM traffic.
  user_data = <<-USERDATA
    <powershell>
    # Set Administrator password
    $admin = [ADSI]"WinNT://./Administrator,User"
    $admin.SetPassword("${random_password.windows_admin.result}")

    # Create self-signed certificate for WinRM HTTPS
    $cert = New-SelfSignedCertificate -DnsName $env:COMPUTERNAME `
      -CertStoreLocation Cert:\LocalMachine\My

    # Remove any existing WinRM HTTPS listener, then create a new one
    Get-ChildItem WSMan:\localhost\Listener | Where-Object {
      $_.Keys -contains "Transport=HTTPS"
    } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    New-Item -Path WSMan:\localhost\Listener -Transport HTTPS `
      -Address * -CertificateThumbPrint $cert.Thumbprint -Force

    # Enable NTLM authentication
    Set-Item WSMan:\localhost\Service\Auth\Basic -Value $false
    Set-Item WSMan:\localhost\Service\Auth\Negotiate -Value $true

    # Allow unencrypted only over HTTPS (transport-level encryption)
    Set-Item WSMan:\localhost\Service\AllowUnencrypted -Value $false

    # Open Windows Firewall for WinRM HTTPS
    New-NetFirewallRule -DisplayName "WinRM HTTPS" -Direction Inbound `
      -LocalPort 5986 -Protocol TCP -Action Allow

    # Restart WinRM to apply changes
    Restart-Service WinRM
    </powershell>
  USERDATA

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-windows"
    OS   = "windows-server-2022"
  })
}
