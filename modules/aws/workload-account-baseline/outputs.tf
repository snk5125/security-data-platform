# -----------------------------------------------------------------------------
# Outputs — Workload Account Baseline Module
# -----------------------------------------------------------------------------
# These outputs are consumed downstream:
#   - vpc_id → Phase 4 (VPC Flow Log attachment)
#   - instance_ids → reference/validation
#   - security_group_id → reference/validation
#   - ssh_private_key → emergency access (sensitive, stored in state only)
# -----------------------------------------------------------------------------

output "vpc_id" {
  description = "VPC ID — consumed by Phase 4 data-sources module for VPC Flow Log attachment"
  value       = aws_vpc.main.id
}

output "subnet_id" {
  description = "Public subnet ID"
  value       = aws_subnet.public.id
}

output "security_group_id" {
  description = "Security group ID — intentionally permissive for demo event generation"
  value       = aws_security_group.permissive.id
}

output "linux_instance_id" {
  description = "EC2 instance ID of the Linux instance"
  value       = aws_instance.linux.id
}

output "linux_public_ip" {
  description = "Public IP of the Linux instance"
  value       = aws_instance.linux.public_ip
}

output "windows_instance_id" {
  description = "EC2 instance ID of the Windows instance"
  value       = aws_instance.windows.id
}

output "windows_public_ip" {
  description = "Public IP of the Windows instance"
  value       = aws_instance.windows.public_ip
}

output "ssh_private_key" {
  description = "SSH private key for the Linux instance (stored in state only, not written to disk)"
  value       = tls_private_key.ssh.private_key_pem
  sensitive   = true
}
