output "network_name" {
  description = "Name of the VPC network."
  value       = google_compute_network.main.name
}

output "network_id" {
  description = "ID of the VPC network."
  value       = google_compute_network.main.id
}

output "subnet_name" {
  description = "Name of the public subnet."
  value       = google_compute_subnetwork.public.name
}

output "linux_vm_ip" {
  description = "External IP of the Linux VM."
  value       = google_compute_instance.linux.network_interface[0].access_config[0].nat_ip
}

output "windows_vm_ip" {
  description = "External IP of the Windows VM."
  value       = google_compute_instance.windows.network_interface[0].access_config[0].nat_ip
}

output "ssh_private_key" {
  description = "SSH private key for the Linux VM (stored in state only)."
  value       = tls_private_key.ssh.private_key_pem
  sensitive   = true
}

output "windows_admin_password" {
  description = "Password for the 'cribl_admin' user on the Windows VM (set via startup script at boot). GCP does not enable the built-in Administrator account."
  value       = random_password.windows_admin.result
  sensitive   = true
}
