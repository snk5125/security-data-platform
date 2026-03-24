# -----------------------------------------------------------------------------
# Azure Workload Account Baseline Module
# -----------------------------------------------------------------------------
# Creates the networking and compute infrastructure for an Azure workload:
#   1. Resource Group
#   2. VNet + public subnet
#   3. NSG (SSH/RDP inbound — intentionally permissive for demo purposes)
#   4. 2 VMs: Linux (Standard_B1s) + Windows (Standard_B1s)
#   5. Public IPs for both VMs
#
# Mirrors the AWS workload-account-baseline module pattern. VMs and NSG rules
# are intentionally permissive to generate security events for the data
# pipeline (same rationale as the AWS security group allowing 0.0.0.0/0).
#
# Network Watcher is NOT explicitly created — Azure auto-creates one per
# region. The data-sources module references it for VNet Flow Logs.
# -----------------------------------------------------------------------------

locals {
  module_tags = merge(var.tags, {
    Component = "workload-baseline"
    ManagedBy = "terraform"
  })
}

# ═════════════════════════════════════════════════════════════════════════════
# 1. RESOURCE GROUP
# ═════════════════════════════════════════════════════════════════════════════

resource "azurerm_resource_group" "workload" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.module_tags
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. NETWORKING — VNet, Subnet, NSG
# ═════════════════════════════════════════════════════════════════════════════

resource "azurerm_virtual_network" "main" {
  name                = "${var.name_prefix}-vnet"
  address_space       = [var.vnet_cidr]
  location            = azurerm_resource_group.workload.location
  resource_group_name = azurerm_resource_group.workload.name
  tags                = local.module_tags
}

resource "azurerm_subnet" "public" {
  name                 = "${var.name_prefix}-subnet-public"
  resource_group_name  = azurerm_resource_group.workload.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.subnet_cidr]
}

# NSG — intentionally permissive to generate security events.
# Mirrors AWS pattern of allowing SSH (22) and RDP (3389) from 0.0.0.0/0.
resource "azurerm_network_security_group" "permissive" {
  name                = "${var.name_prefix}-nsg-permissive"
  location            = azurerm_resource_group.workload.location
  resource_group_name = azurerm_resource_group.workload.name
  tags                = local.module_tags

  security_rule {
    name                       = "AllowSSH"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowRDP"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "3389"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  # WinRM HTTPS — required for Ansible management of Windows VMs.
  # Intentionally open to 0.0.0.0/0 for demo; production should restrict.
  security_rule {
    name                       = "AllowWinRM"
    priority                   = 120
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "5986"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "public" {
  subnet_id                 = azurerm_subnet.public.id
  network_security_group_id = azurerm_network_security_group.permissive.id
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. COMPUTE — Linux + Windows VMs (Standard_B1s, free-tier eligible)
# ═════════════════════════════════════════════════════════════════════════════

# SSH key for Linux VM — generated and stored in Terraform state only
# (same pattern as AWS TLS key pair).
resource "tls_private_key" "ssh" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

# ── Public IPs ──
resource "azurerm_public_ip" "linux" {
  name                = "${var.name_prefix}-pip-linux"
  location            = azurerm_resource_group.workload.location
  resource_group_name = azurerm_resource_group.workload.name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.module_tags
}

resource "azurerm_public_ip" "windows" {
  name                = "${var.name_prefix}-pip-windows"
  location            = azurerm_resource_group.workload.location
  resource_group_name = azurerm_resource_group.workload.name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.module_tags
}

# ── Network Interfaces ──
resource "azurerm_network_interface" "linux" {
  name                = "${var.name_prefix}-nic-linux"
  location            = azurerm_resource_group.workload.location
  resource_group_name = azurerm_resource_group.workload.name
  tags                = local.module_tags

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.public.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.linux.id
  }
}

resource "azurerm_network_interface" "windows" {
  name                = "${var.name_prefix}-nic-windows"
  location            = azurerm_resource_group.workload.location
  resource_group_name = azurerm_resource_group.workload.name
  tags                = local.module_tags

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.public.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.windows.id
  }
}

# ── Linux VM (Ubuntu 22.04 LTS, Standard_B1s) ──
resource "azurerm_linux_virtual_machine" "main" {
  name                = "${var.name_prefix}-vm-linux"
  resource_group_name = azurerm_resource_group.workload.name
  location            = azurerm_resource_group.workload.location
  size                = "Standard_D2als_v7"
  admin_username      = var.admin_username

  network_interface_ids = [azurerm_network_interface.linux.id]

  admin_ssh_key {
    username   = var.admin_username
    public_key = tls_private_key.ssh.public_key_openssh
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }

  tags = local.module_tags
}

# ── Windows VM (Windows Server 2022, Standard_B1s) ──
resource "azurerm_windows_virtual_machine" "main" {
  name = "${var.name_prefix}-vm-win"
  # Windows computer_name max 15 chars — truncate prefix and append "win".
  computer_name       = "${substr(replace(var.name_prefix, "-", ""), 0, 12)}win"
  resource_group_name = azurerm_resource_group.workload.name
  location            = azurerm_resource_group.workload.location
  size                = "Standard_D2als_v7"
  admin_username      = var.admin_username
  admin_password      = var.windows_admin_password

  network_interface_ids = [azurerm_network_interface.windows.id]

  # NOTE: WinRM bootstrap is handled by azurerm_virtual_machine_extension below.
  # Azure Windows VMs do NOT execute PowerShell via custom_data — that mechanism
  # only works on Linux (cloud-init). The CustomScriptExtension is the correct
  # approach for running PowerShell at first boot on Azure Windows VMs.

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "MicrosoftWindowsServer"
    offer     = "WindowsServer"
    sku       = "2022-datacenter-azure-edition"
    version   = "latest"
  }

  tags = local.module_tags
}

# ── WinRM Bootstrap via CustomScriptExtension ──────────────────────────────
# Azure Windows VMs cannot execute PowerShell via custom_data (that path only
# works for Linux/cloud-init). The CustomScriptExtension is the supported
# mechanism for running PowerShell at first boot. This creates a self-signed
# TLS certificate, configures the WinRM HTTPS listener on port 5986, enables
# Basic auth, and opens the Windows Firewall — so Ansible can connect
# immediately using the admin_username/admin_password defined on the VM.
resource "azurerm_virtual_machine_extension" "winrm_setup" {
  name                 = "${var.name_prefix}-winrm-setup"
  virtual_machine_id   = azurerm_windows_virtual_machine.main.id
  publisher            = "Microsoft.Compute"
  type                 = "CustomScriptExtension"
  type_handler_version = "1.10"

  settings = jsonencode({
    commandToExecute = "powershell -ExecutionPolicy Unrestricted -Command \"$cert = New-SelfSignedCertificate -DnsName $env:COMPUTERNAME -CertStoreLocation Cert:\\LocalMachine\\My; winrm create winrm/config/Listener?Address=*+Transport=HTTPS '@{Hostname=\\\"'$env:COMPUTERNAME'\\\";CertificateThumbprint=\\\"'$($cert.Thumbprint)'\\\"}'; Set-Item WSMan:\\localhost\\Service\\Auth\\Basic -Value $true; Set-Item WSMan:\\localhost\\Service\\AllowUnencrypted -Value $false; New-NetFirewallRule -Name WINRM-HTTPS -DisplayName 'WinRM HTTPS' -Direction Inbound -Protocol TCP -LocalPort 5986 -Action Allow\""
  })
}
