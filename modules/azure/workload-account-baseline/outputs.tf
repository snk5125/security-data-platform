output "resource_group_name" {
  description = "Name of the workload resource group."
  value       = azurerm_resource_group.workload.name
}

output "vnet_id" {
  description = "ID of the workload VNet."
  value       = azurerm_virtual_network.main.id
}

output "vnet_name" {
  description = "Name of the workload VNet."
  value       = azurerm_virtual_network.main.name
}

output "subnet_id" {
  description = "ID of the public subnet."
  value       = azurerm_subnet.public.id
}

output "nsg_id" {
  description = "ID of the permissive NSG."
  value       = azurerm_network_security_group.permissive.id
}

output "linux_public_ip" {
  description = "Public IP of the Linux VM."
  value       = azurerm_public_ip.linux.ip_address
}

output "windows_public_ip" {
  description = "Public IP of the Windows VM."
  value       = azurerm_public_ip.windows.ip_address
}

output "ssh_private_key" {
  description = "SSH private key for the Linux VM (stored in state only)."
  value       = tls_private_key.ssh.private_key_pem
  sensitive   = true
}

output "windows_admin_password" {
  description = "Admin password for the Windows VM (passed via variable, demo only)."
  value       = var.windows_admin_password
  sensitive   = true
}
