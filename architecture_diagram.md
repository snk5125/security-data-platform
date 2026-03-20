# Architecture Diagram — Security Data Lakehouse

Generated from Terraform configuration across 7 independent roots

## High-Level Architecture

```mermaid
graph TB
    subgraph "Databricks (Free Edition)"
        direction TB
        DW["Serverless Starter Warehouse<br/>0a3fea1c53bea9c6"]

        subgraph "Unity Catalog"
            CAT["Catalog: security_poc"]
            SB["Schema: bronze"]
            SS["Schema: silver"]
            SG["Schema: gold"]
            SSEC["Schema: security"]
            CAT --> SB
            CAT --> SS
            CAT --> SG
            CAT --> SSEC
        end

        subgraph "Scheduled Jobs (7 Jobs, PAUSED)"
            J1["CloudTrail Job<br/>15min trigger · 1 task"]
            J2["bronze-vpc-flow-ingest<br/>10min trigger · 3 tasks<br/>ingest → gold_alerts → forward_alerts"]
            J3["GuardDuty Job<br/>6hr trigger · 1 task"]
            J4["Config Pipeline<br/>24hr trigger · 3 tasks<br/>bronze → silver CDC → gold EC2"]
            J5["Threat Intel Pipeline<br/>daily 01:00 UTC · 2 tasks<br/>bronze_ingest → silver_network"]
            J6["Activity Log Job<br/>15min trigger · 1 task"]
            J7["Azure VNet Flow Job<br/>10min trigger · 3 tasks<br/>ingest → gold_alerts → forward_alerts"]
        end

        subgraph "Notebooks (15)"
            subgraph "Bronze/AWS (5)"
                N0["00_ocsf_common.py"]
                N1["01_cloudtrail.py"]
                N2["02_vpc_flow.py"]
                N3["03_guardduty.py"]
                N4["04_config.py"]
            end
            subgraph "Bronze/Azure (3)"
                NA0["00_azure_common.py"]
                NA1["01_activity_log.py"]
                NA2["02_vnet_flow.py"]
            end
            subgraph "Threat Intel (3)"
                NTI0["00_threat_intel_common.py"]
                NTI1["01_bronze_ingest.py"]
                NTI2["02_silver_network.py"]
            end
            subgraph "Silver (1)"
                N5["01_silver_config_cdc.py"]
            end
            subgraph "Gold (3)"
                N6["01_gold_ec2_inventory.py"]
                N7["02_gold_alerts.py"]
                N8["03_gold_alerts_forward.py"]
            end
        end

        subgraph "Storage Credentials"
            HC["lakehouse-hub-credential<br/>(AWS IAM role)"]
            MC["lakehouse-managed-credential<br/>(AWS IAM role)"]
            AZC["lakehouse-azure-credential<br/>(Entra ID service principal)"]
        end

        subgraph "External Locations"
            EL1["security-logs-workload-a"]
            EL2["security-logs-workload-b"]
            ELA["security-logs-azure-workload-a"]
            EL3["managed-storage"]
        end

        J1 --> N1
        J2 --> N2
        J2 --> N7
        J2 --> N8
        J3 --> N3
        J4 --> N4
        J4 --> N5
        J4 --> N6
        J5 --> NTI1
        J5 --> NTI2
        J6 --> NA1
        J7 --> NA2
        J7 --> N7
        J7 --> N8
        N1 & N2 & N3 & N4 --> DW
        NA1 & NA2 --> DW
        NTI1 & NTI2 --> DW
        N5 & N6 & N7 & N8 --> DW
        HC --> EL1 & EL2
        MC --> EL3
        AZC --> ELA
    end

    subgraph "AWS: Security Account (<SECURITY_ACCOUNT_ID>)"
        direction TB
        subgraph "IAM Roles"
            HUB["lakehouse-hub-role<br/>Chain-assume into workloads"]
            MSR["lakehouse-managed-storage-role<br/>Databricks managed storage"]
        end

        subgraph "S3"
            MSB["security-lakehouse-managed-<SECURITY_ACCOUNT_ID><br/>Delta table storage"]
            STB["security-lakehouse-tfstate-<SECURITY_ACCOUNT_ID><br/>Terraform state"]
        end

        DDB["DynamoDB: security-lakehouse-tflock<br/>State locking"]

        subgraph "SNS Alerts"
            SNS["security-lakehouse-alerts<br/>SNS topic"]
            SNSU["lakehouse-sns-publisher<br/>IAM user · sns:Publish only"]
        end
    end

    subgraph "AWS: Workload A (<WORKLOAD_A_ACCOUNT_ID>)"
        direction TB
        subgraph "VPC-A [10.0.0.0/16]"
            subgraph "Public Subnet A [10.0.1.0/24]"
                EC2AL["Linux t2.micro<br/><INSTANCE_ID><br/><PUBLIC_IP>"]
                EC2AW["Windows t2.micro<br/><INSTANCE_ID><br/><PUBLIC_IP>"]
            end
            SGA["SG: 0.0.0.0/0 :22,:3389"]
            IGWA["Internet Gateway"]
        end

        subgraph "Data Sources A"
            CTA["CloudTrail"]
            FLA["VPC Flow Logs"]
            GDA["GuardDuty"]
            CFA["AWS Config"]
        end

        subgraph "Security A"
            S3A["S3: lakehouse-workload-a-<br/>security-logs-<WORKLOAD_A_ACCOUNT_ID>"]
            KMSA["KMS: <KMS_KEY_PREFIX>-...<br/>GuardDuty encryption"]
            ROA["lakehouse-workload-a-<br/>read-only-role"]
        end
    end

    subgraph "AWS: Workload B (<WORKLOAD_B_ACCOUNT_ID>)"
        direction TB
        subgraph "VPC-B [10.1.0.0/16]"
            subgraph "Public Subnet B [10.1.1.0/24]"
                EC2BL["Linux t2.micro<br/><INSTANCE_ID><br/><PUBLIC_IP>"]
                EC2BW["Windows t2.micro<br/><INSTANCE_ID><br/><PUBLIC_IP>"]
            end
            SGB["SG: 0.0.0.0/0 :22,:3389"]
            IGWB["Internet Gateway"]
        end

        subgraph "Data Sources B"
            CTB["CloudTrail"]
            FLB["VPC Flow Logs"]
            GDB["GuardDuty"]
            CFB["AWS Config"]
        end

        subgraph "Security B"
            S3B["S3: lakehouse-workload-b-<br/>security-logs-<WORKLOAD_B_ACCOUNT_ID>"]
            KMSB["KMS: <KMS_KEY_PREFIX>-...<br/>GuardDuty encryption"]
            ROB["lakehouse-workload-b-<br/>read-only-role"]
        end
    end

    subgraph "Azure: Foundation (Entra ID + Managed Storage)"
        direction TB
        AZSP["Entra ID Service Principal<br/>lakehouse-databricks-sp"]
        AZMGD["ADLS Gen2: <AZURE_MANAGED_STORAGE_ACCOUNT><br/>Managed storage (future use)"]
    end

    subgraph "Azure: Workload A (3bd01bbf-6efb-42cb-...)"
        direction TB
        subgraph "VNet-AZ [10.10.0.0/16]"
            subgraph "Public Subnet AZ [10.10.1.0/24]"
                VMAZL["Linux Standard_D2als_v7<br/>Ubuntu 22.04 LTS"]
                VMAZW["Windows Standard_D2als_v7<br/>Server 2022"]
            end
            NSGAZ["NSG: 0.0.0.0/0 :22,:3389"]
        end

        subgraph "Data Sources AZ"
            AZAL["Activity Log<br/>(Diagnostic Setting → ADLS)"]
            AZVFL["VNet Flow Logs<br/>(deferred to azurerm v4)"]
        end

        subgraph "Storage AZ"
            ADLSAZ["ADLS Gen2:<br/><AZURE_LOGS_STORAGE_ACCOUNT><br/>Containers: security-logs,<br/>vnet-flow-logs"]
            SPRA["SP Role Assignment<br/>Storage Blob Data Reader"]
        end
    end

    %% AWS IAM Chain-Assume Flow
    HC -.->|"sts:AssumeRole"| HUB
    MC -.->|"sts:AssumeRole"| MSR
    HUB -.->|"sts:AssumeRole"| ROA
    HUB -.->|"sts:AssumeRole"| ROB
    MSR -.->|"S3 read/write"| MSB
    EL3 -.-> MSB
    EL1 -.-> S3A
    EL2 -.-> S3B

    %% Azure Entra ID auth flow
    AZC -.->|"Entra ID auth"| AZSP
    AZSP -.->|"Storage Blob<br/>Data Reader"| ADLSAZ
    ELA -.-> ADLSAZ

    %% AWS Data Source to S3 flows
    CTA -->|"JSON.gz"| S3A
    FLA -->|"text.gz"| S3A
    GDA -->|"JSONL.gz (KMS)"| S3A
    CFA -->|"JSON.gz"| S3A
    CTB -->|"JSON.gz"| S3B
    FLB -->|"text.gz"| S3B
    GDB -->|"JSONL.gz (KMS)"| S3B
    CFB -->|"JSON.gz"| S3B

    %% Azure Data Source to ADLS flows
    AZAL -->|"JSON"| ADLSAZ
    AZVFL -.->|"deferred"| ADLSAZ

    %% EC2 generates telemetry
    EC2AL -.- CTA & FLA & GDA & CFA
    EC2AW -.- CTA & FLA & GDA & CFA
    EC2BL -.- CTB & FLB & GDB & CFB
    EC2BW -.- CTB & FLB & GDB & CFB

    %% Azure VMs generate telemetry
    VMAZL -.- AZAL & AZVFL
    VMAZW -.- AZAL & AZVFL

    %% S3 read via roles
    ROA -.->|"S3 read"| S3A
    ROB -.->|"S3 read"| S3B
```

## Access Chain Detail (IAM + Entra ID)

```mermaid
graph LR
    subgraph "AWS IAM Role Chain"
        UCM["Databricks UC Master Role<br/>arn:aws:iam::<DATABRICKS_AWS_ACCOUNT_ID>:role/<br/>unity-catalog-prod-UCMasterRole-..."]

        HUB["lakehouse-hub-role<br/>(<SECURITY_ACCOUNT_ID>)"]
        MSR["lakehouse-managed-storage-role<br/>(<SECURITY_ACCOUNT_ID>)"]
        ROA["lakehouse-workload-a-read-only-role<br/>(<WORKLOAD_A_ACCOUNT_ID>)"]
        ROB["lakehouse-workload-b-read-only-role<br/>(<WORKLOAD_B_ACCOUNT_ID>)"]

        S3A["S3: workload-a-security-logs"]
        S3B["S3: workload-b-security-logs"]
        S3M["S3: managed-storage"]
        KMSA["KMS Key A"]
        KMSB["KMS Key B"]

        UCM -->|"AssumeRole<br/>ExternalID: ede6..."| HUB
        UCM -->|"AssumeRole<br/>ExternalID: ede6..."| MSR
        HUB -->|"self-assume"| HUB
        MSR -->|"self-assume"| MSR
        HUB -->|"AssumeRole<br/>(org-scoped)"| ROA
        HUB -->|"AssumeRole<br/>(org-scoped)"| ROB
        ROA -->|"s3:GetObject<br/>s3:ListBucket"| S3A
        ROB -->|"s3:GetObject<br/>s3:ListBucket"| S3B
        MSR -->|"s3:GetObject<br/>s3:PutObject<br/>s3:DeleteObject"| S3M
        HUB -->|"kms:Decrypt"| KMSA
        HUB -->|"kms:Decrypt"| KMSB
    end

    subgraph "Azure Entra ID Service Principal"
        AZCRED["lakehouse-azure-credential<br/>(Databricks storage credential)"]
        AZSP["Entra ID SP:<br/>lakehouse-databricks-sp<br/>(directory_id + application_id<br/>+ client_secret)"]
        ADLSWK["ADLS Gen2:<br/><AZURE_LOGS_STORAGE_ACCOUNT><br/>(workload-a security logs)"]
        ADLSMGD["ADLS Gen2:<br/><AZURE_MANAGED_STORAGE_ACCOUNT><br/>(managed storage — future)"]

        AZCRED -->|"azure_service_principal<br/>auth"| AZSP
        AZSP -->|"Storage Blob<br/>Data Reader"| ADLSWK
        AZSP -->|"Storage Blob<br/>Data Contributor"| ADLSMGD
    end
```

## Data Flow: S3 + ADLS + Threat Intel Feeds → Bronze → Silver → Gold → SNS

```mermaid
graph LR
    subgraph "External Threat Intel Feeds (daily)"
        FEODO["Feodo Tracker<br/>C2 IPs"]
        ET["Emerging Threats<br/>IP blocklist"]
        IPSUM["IPsum<br/>IP reputation"]
    end

    subgraph "AWS Workload A (<WORKLOAD_A_ACCOUNT_ID>)"
        S3A["S3 Bucket<br/>lakehouse-workload-a-security-logs"]
    end

    subgraph "AWS Workload B (<WORKLOAD_B_ACCOUNT_ID>)"
        S3B["S3 Bucket<br/>lakehouse-workload-b-security-logs"]
    end

    subgraph "Azure Workload A (3bd01bbf-...)"
        ADLSA["ADLS Gen2<br/><AZURE_LOGS_STORAGE_ACCOUNT>"]
    end

    subgraph "Ingestion"
        AL["Auto Loader<br/>cloudFiles · availableNow<br/>directory listing mode"]
        ALAZ["Auto Loader<br/>cloudFiles · ADLS Gen2<br/>directory listing mode"]
        TI_FETCH["HTTP Fetch<br/>01_bronze_ingest<br/>daily · OCSF formatted"]
    end

    subgraph "Bronze Delta Tables"
        CT["cloudtrail_raw"]
        VF["vpc_flow_raw<br/>(OCSF network activity)"]
        GD["guardduty_raw"]
        CF["config_raw"]
        AZACT["activity_log_raw<br/>(OCSF — Azure Activity Log)"]
        AZVF["azure_vnet_flow_raw<br/>(OCSF network activity)"]
        TIR["threat_intel_raw<br/>IOC feed rows · 14-day TTL"]
    end

    subgraph "Silver Delta Tables"
        CDC["config_cdc<br/>Normalized CDC rows<br/>per resource type"]
        TIN["threat_intel_network<br/>Deduplicated IOCs<br/>MERGE on ioc_id · TTL-managed"]
    end

    subgraph "Gold Delta Tables"
        EC2["ec2_inventory<br/>Current-state per instance<br/>ENIs, volumes, SGs joined"]
        ALERTS["alerts<br/>vpc_flow + azure_vnet_flow<br/>× threat_intel joins<br/>MERGE on alert_id"]
        AFWD["alerts_forwarded<br/>CDF version watermark<br/>dedup + delivery tracking"]
    end

    subgraph "AWS SNS (Security Account)"
        SNS["security-lakehouse-alerts<br/>SNS topic · sns:Publish"]
    end

    subgraph "S3 Paths"
        P1["cloudtrail/AWSLogs/ → JSON.gz"]
        P2["vpc-flow-logs/AWSLogs/ → text.gz"]
        P3["AWSLogs/.../GuardDuty/ → JSONL.gz"]
        P4["config/AWSLogs/ → JSON.gz"]
    end

    subgraph "ADLS Paths"
        PA1["insights-activity-logs/ → JSON"]
        PA2["vnet-flow-logs/ → JSON (deferred)"]
    end

    S3A --> P1 & P2 & P3 & P4
    S3B --> P1 & P2 & P3 & P4
    ADLSA --> PA1 & PA2
    P1 --> AL --> CT
    P2 --> AL --> VF
    P3 --> AL --> GD
    P4 --> AL --> CF
    PA1 --> ALAZ --> AZACT
    PA2 -.->|"deferred"| ALAZ -.-> AZVF
    FEODO & ET & IPSUM --> TI_FETCH --> TIR

    CF -->|"CDF / incremental"| CDC
    CDC -->|"window + MERGE"| EC2
    TIR -->|"MERGE on ioc_id · daily"| TIN
    VF & AZVF & TIN -->|"watermark join<br/>10-min incremental<br/>MERGE on alert_id"| ALERTS
    ALERTS -->|"CDF read · inserts only"| AFWD
    ALERTS -->|"sns:Publish per new alert"| SNS
```

## Terraform Root Dependency Graph

```mermaid
graph TD
    BOOT["<b>bootstrap/</b><br/>(local state)<br/>S3 state bucket · DynamoDB lock table"]

    FOUNDA["<b>foundations/aws-security/</b><br/>module.security_foundation<br/>Managed S3 bucket · SNS topic · IAM publisher"]

    FOUNDAZ["<b>foundations/azure-security/</b><br/>module.security_foundation<br/>Entra ID SP · ADLS managed storage"]

    WKA["<b>workloads/aws-workload-a/</b><br/>module.baseline + module.data_sources<br/>VPC · EC2 · CloudTrail · VPC Flow<br/>GuardDuty · Config · S3 · KMS · IAM"]

    WKB["<b>workloads/aws-workload-b/</b><br/>module.baseline + module.data_sources<br/>VPC · EC2 · CloudTrail · VPC Flow<br/>GuardDuty · Config · S3 · KMS · IAM"]

    WKAZA["<b>workloads/azure-workload-a/</b><br/>module.baseline + module.data_sources<br/>VNet · VMs · Activity Log<br/>ADLS security logs · SP role assignment"]

    ASM["<b>scripts/assemble-workloads.sh</b><br/>Collects workload_manifest outputs →<br/>hub/workloads.auto.tfvars.json"]

    subgraph HUB ["hub/ (Databricks integration layer)"]
        direction TB
        IAM["iam.tf<br/>lakehouse-hub-role<br/>lakehouse-managed-storage-role"]
        CI["module.cloud_integration<br/>3 storage creds (hub + managed + azure)<br/>4 external locations (for_each over workloads)"]
        UC["module.unity_catalog<br/>Catalog · schemas · grants"]
        WC["module.workspace_config<br/>Cluster policy"]
        JOBS["module.jobs<br/>15 notebooks · 5 directories<br/>7 jobs · secret scope + secrets"]
        CI --> IAM
        CI --> UC
        UC --> WC
        WC --> JOBS
    end

    BOOT -->|"S3 backend"| FOUNDA
    BOOT -->|"S3 backend"| FOUNDAZ
    FOUNDA -->|"managed S3 exists"| WKA & WKB
    FOUNDAZ -->|"SP ID for role<br/>assignments"| WKAZA
    WKA & WKB & WKAZA -->|"terraform output"| ASM
    ASM -->|"workloads.auto.tfvars.json"| CI
```

## Resource Count by Root

| Terraform Root | Modules | Resources | Data Sources |
|----------------|---------|-----------|--------------|
| `bootstrap/` (local state) | — | 5 | 1 |
| `foundations/aws-security/` | `security_foundation` | 10 | 5 |
| `foundations/azure-security/` | `security_foundation` | 6 | 1 |
| `workloads/aws-workload-a/` | `baseline` + `data_sources` | ~28 | ~6 |
| `workloads/aws-workload-b/` | `baseline` + `data_sources` | ~28 | ~6 |
| `workloads/azure-workload-a/` | `baseline` + `data_sources` | ~19 | ~1 |
| `hub/` | `iam.tf` (inline) + `cloud_integration` + `unity_catalog` + `workspace_config` + `jobs` | ~56 | ~8 |
| **Total (across 7 roots)** | | **~152** | **~28** |
