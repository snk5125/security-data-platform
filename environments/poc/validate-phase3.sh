#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Phase 3: Workload Account Infrastructure Validation
# -----------------------------------------------------------------------------
# Verifies that Phase 3 resources (VPC, networking, security groups, EC2
# instances) were created correctly in both workload accounts.
# Run from any directory: ./environments/poc/validate-phase3.sh
# Exit code 0 = all checks passed, non-zero = failure.
# -----------------------------------------------------------------------------
set -uo pipefail

SECURITY_ACCOUNT_ID="<SECURITY_ACCOUNT_ID>"
WORKLOAD_A_ACCOUNT_ID="<WORKLOAD_A_ACCOUNT_ID>"
WORKLOAD_B_ACCOUNT_ID="<WORKLOAD_B_ACCOUNT_ID>"
WORKLOAD_A_ROLE="arn:aws:iam::${WORKLOAD_A_ACCOUNT_ID}:role/OrganizationAccountAccessRole"
WORKLOAD_B_ROLE="arn:aws:iam::${WORKLOAD_B_ACCOUNT_ID}:role/OrganizationAccountAccessRole"
PASS=0
FAIL=0

check() {
  local description="$1"
  local result="$2"
  local expected="$3"

  if [[ "$result" == *"$expected"* ]]; then
    echo "  PASS: ${description}"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: ${description} (expected: ${expected}, got: ${result})"
    FAIL=$((FAIL + 1))
  fi
}

# Helper: assume role into a workload account and export temp credentials.
# Usage: assume_role <role_arn> <session_name>
assume_role() {
  local role_arn="$1"
  local session_name="$2"
  local creds

  creds=$(aws sts assume-role --role-arn "$role_arn" --role-session-name "$session_name" --output json 2>/dev/null)
  if [[ -z "$creds" ]]; then
    echo "  FAIL: Could not assume role ${role_arn}"
    FAIL=$((FAIL + 1))
    return 1
  fi

  export AWS_ACCESS_KEY_ID=$(echo "$creds" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['AccessKeyId'])")
  export AWS_SECRET_ACCESS_KEY=$(echo "$creds" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SecretAccessKey'])")
  export AWS_SESSION_TOKEN=$(echo "$creds" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SessionToken'])")
  return 0
}

# Helper: clear assumed role credentials, revert to caller identity.
clear_role() {
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
}

# Helper: validate one workload account's resources.
# Usage: validate_account <alias> <role_arn> <vpc_cidr>
validate_account() {
  local alias="$1"
  local role_arn="$2"
  local vpc_cidr="$3"
  local name_prefix="lakehouse-${alias}"

  echo "--- ${alias} (via assume-role) ---"

  if ! assume_role "$role_arn" "phase3-validate-${alias}"; then
    return
  fi

  # 1. VPC exists with correct CIDR
  local vpc_id
  vpc_id=$(aws ec2 describe-vpcs \
    --filters "Name=tag:Name,Values=${name_prefix}-vpc" \
    --query 'Vpcs[0].VpcId' --output text 2>/dev/null)
  if [[ -n "$vpc_id" && "$vpc_id" != "None" ]]; then
    echo "  PASS: VPC exists (${vpc_id})"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: VPC ${name_prefix}-vpc not found"
    FAIL=$((FAIL + 1))
    clear_role
    return
  fi

  local actual_cidr
  actual_cidr=$(aws ec2 describe-vpcs --vpc-ids "$vpc_id" \
    --query 'Vpcs[0].CidrBlock' --output text 2>/dev/null)
  check "VPC CIDR is ${vpc_cidr}" "$actual_cidr" "$vpc_cidr"

  # 2. Internet gateway attached
  local igw_id
  igw_id=$(aws ec2 describe-internet-gateways \
    --filters "Name=attachment.vpc-id,Values=${vpc_id}" \
    --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null)
  if [[ -n "$igw_id" && "$igw_id" != "None" ]]; then
    echo "  PASS: Internet gateway attached (${igw_id})"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: No internet gateway attached to VPC"
    FAIL=$((FAIL + 1))
  fi

  # 3. Public subnet exists with auto-assign public IP
  local subnet_id
  subnet_id=$(aws ec2 describe-subnets \
    --filters "Name=vpc-id,Values=${vpc_id}" "Name=tag:Name,Values=${name_prefix}-public-subnet" \
    --query 'Subnets[0].SubnetId' --output text 2>/dev/null)
  if [[ -n "$subnet_id" && "$subnet_id" != "None" ]]; then
    echo "  PASS: Public subnet exists (${subnet_id})"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: Public subnet not found"
    FAIL=$((FAIL + 1))
  fi

  local auto_assign
  auto_assign=$(aws ec2 describe-subnets --subnet-ids "$subnet_id" \
    --query 'Subnets[0].MapPublicIpOnLaunch' --output text 2>/dev/null)
  check "Subnet auto-assigns public IP" "$auto_assign" "True"

  # 4. Route table has default route to IGW
  local rt_id
  rt_id=$(aws ec2 describe-route-tables \
    --filters "Name=vpc-id,Values=${vpc_id}" "Name=tag:Name,Values=${name_prefix}-public-rt" \
    --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null)
  if [[ -n "$rt_id" && "$rt_id" != "None" ]]; then
    local default_route
    default_route=$(aws ec2 describe-route-tables --route-table-ids "$rt_id" \
      --query "RouteTables[0].Routes[?DestinationCidrBlock=='0.0.0.0/0'].GatewayId" --output text 2>/dev/null)
    check "Route table default route points to IGW" "$default_route" "$igw_id"
  else
    echo "  FAIL: Route table not found"
    FAIL=$((FAIL + 1))
  fi

  # 5. Security group has SSH (22) and RDP (3389) from 0.0.0.0/0
  local sg_id
  sg_id=$(aws ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=${vpc_id}" "Name=group-name,Values=${name_prefix}-permissive-sg" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
  if [[ -n "$sg_id" && "$sg_id" != "None" ]]; then
    echo "  PASS: Security group exists (${sg_id})"
    PASS=$((PASS + 1))

    local ssh_rule
    ssh_rule=$(aws ec2 describe-security-groups --group-ids "$sg_id" \
      --query "SecurityGroups[0].IpPermissions[?FromPort==\`22\`].IpRanges[0].CidrIp" --output text 2>/dev/null)
    check "SG allows SSH from 0.0.0.0/0" "$ssh_rule" "0.0.0.0/0"

    local rdp_rule
    rdp_rule=$(aws ec2 describe-security-groups --group-ids "$sg_id" \
      --query "SecurityGroups[0].IpPermissions[?FromPort==\`3389\`].IpRanges[0].CidrIp" --output text 2>/dev/null)
    check "SG allows RDP from 0.0.0.0/0" "$rdp_rule" "0.0.0.0/0"
  else
    echo "  FAIL: Security group not found"
    FAIL=$((FAIL + 1))
  fi

  # 6. Linux EC2 instance is running with a public IP
  local linux_state
  linux_state=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=${name_prefix}-linux" "Name=instance-state-name,Values=running,pending" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null)
  check "Linux instance is running" "$linux_state" "running"

  local linux_public_ip
  linux_public_ip=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=${name_prefix}-linux" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null)
  if [[ -n "$linux_public_ip" && "$linux_public_ip" != "None" ]]; then
    echo "  PASS: Linux instance has public IP (${linux_public_ip})"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: Linux instance has no public IP"
    FAIL=$((FAIL + 1))
  fi

  # 7. Windows EC2 instance is running with a public IP
  local windows_state
  windows_state=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=${name_prefix}-windows" "Name=instance-state-name,Values=running,pending" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null)
  check "Windows instance is running" "$windows_state" "running"

  local windows_public_ip
  windows_public_ip=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=${name_prefix}-windows" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null)
  if [[ -n "$windows_public_ip" && "$windows_public_ip" != "None" ]]; then
    echo "  PASS: Windows instance has public IP (${windows_public_ip})"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: Windows instance has no public IP"
    FAIL=$((FAIL + 1))
  fi

  # 8. Key pair exists
  local key_name
  key_name=$(aws ec2 describe-key-pairs \
    --filters "Name=key-name,Values=${name_prefix}-key" \
    --query 'KeyPairs[0].KeyName' --output text 2>/dev/null)
  check "SSH key pair exists" "$key_name" "${name_prefix}-key"

  clear_role
  echo ""
}

echo "=== Phase 3: Workload Account Infrastructure Validation ==="
echo ""

# ── Validate each workload account ──────────────────────────────────────────

validate_account "workload-a" "$WORKLOAD_A_ROLE" "10.0.0.0/16"
validate_account "workload-b" "$WORKLOAD_B_ROLE" "10.1.0.0/16"

# ── Terraform State Consistency ─────────────────────────────────────────────

echo "--- Terraform State ---"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOURCE_COUNT=$(terraform -chdir="$SCRIPT_DIR" state list 2>/dev/null | wc -l | tr -d ' ')
# Phase 2 = 9 resources + Phase 3 = 20 resources (incl. tls keys) = 29 total
if [[ "$RESOURCE_COUNT" -ge 29 ]]; then
  echo "  PASS: Terraform state has ${RESOURCE_COUNT} resources (expected >= 29)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Terraform state has ${RESOURCE_COUNT} resources (expected >= 29)"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
exit "$FAIL"
