# Architecture Diagram — Security Data Lakehouse

Generated from Terraform state (113 entries: 92 resources + 21 data sources)

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
            CAT --> SB
            CAT --> SS
            CAT --> SG
        end

        subgraph "Scheduled Jobs (5 Jobs, PAUSED)"
            J1["CloudTrail Job<br/>15min trigger · 1 task"]
            J2["bronze-vpc-flow-ingest<br/>10min trigger · 3 tasks<br/>ingest → gold_alerts → forward_alerts"]
            J3["GuardDuty Job<br/>6hr trigger · 1 task"]
            J4["Config Pipeline<br/>24hr trigger · 3 tasks<br/>bronze → silver CDC → gold EC2"]
            J5["Threat Intel Pipeline<br/>daily 01:00 UTC · 2 tasks<br/>bronze_ingest → silver_network"]
        end

        subgraph "Notebooks (12)"
            subgraph "Bronze (5)"
                N0["00_ocsf_common.py"]
                N1["01_bronze_cloudtrail.py"]
                N2["02_bronze_vpc_flow.py"]
                N3["03_bronze_guardduty.py"]
                N4["04_bronze_config.py"]
            end
            subgraph "Threat Intel (3)"
                NTI0["00_threat_intel_common.py"]
                NTI1["01_bronze_threat_intel_ingest.py"]
                NTI2["02_silver_threat_intel_network.py"]
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
            HC["lakehouse-hub-credential"]
            MC["lakehouse-managed-credential"]
        end

        subgraph "External Locations"
            EL1["workload-a-security-logs"]
            EL2["workload-b-security-logs"]
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
        N1 & N2 & N3 & N4 --> DW
        NTI1 & NTI2 --> DW
        N5 & N6 & N7 & N8 --> DW
        HC --> EL1 & EL2
        MC --> EL3
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

    %% IAM Chain-Assume Flow
    HC -.->|"sts:AssumeRole"| HUB
    MC -.->|"sts:AssumeRole"| MSR
    HUB -.->|"sts:AssumeRole"| ROA
    HUB -.->|"sts:AssumeRole"| ROB
    MSR -.->|"S3 read/write"| MSB
    EL3 -.-> MSB
    EL1 -.-> S3A
    EL2 -.-> S3B

    %% Data Source to S3 flows
    CTA -->|"JSON.gz"| S3A
    FLA -->|"text.gz"| S3A
    GDA -->|"JSONL.gz (KMS)"| S3A
    CFA -->|"JSON.gz"| S3A
    CTB -->|"JSON.gz"| S3B
    FLB -->|"text.gz"| S3B
    GDB -->|"JSONL.gz (KMS)"| S3B
    CFB -->|"JSON.gz"| S3B

    %% EC2 generates telemetry
    EC2AL -.- CTA & FLA & GDA & CFA
    EC2AW -.- CTA & FLA & GDA & CFA
    EC2BL -.- CTB & FLB & GDB & CFB
    EC2BW -.- CTB & FLB & GDB & CFB

    %% S3 read via roles
    ROA -.->|"S3 read"| S3A
    ROB -.->|"S3 read"| S3B
```

## IAM Trust Chain Detail

```mermaid
graph LR
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
```

## Data Flow: S3 + Threat Intel Feeds → Bronze → Silver → Gold → SNS

```mermaid
graph LR
    subgraph "External Threat Intel Feeds (daily)"
        FEODO["Feodo Tracker<br/>C2 IPs"]
        ET["Emerging Threats<br/>IP blocklist"]
        IPSUM["IPsum<br/>IP reputation"]
    end

    subgraph "Workload A (<WORKLOAD_A_ACCOUNT_ID>)"
        S3A["S3 Bucket<br/>lakehouse-workload-a-security-logs"]
    end

    subgraph "Workload B (<WORKLOAD_B_ACCOUNT_ID>)"
        S3B["S3 Bucket<br/>lakehouse-workload-b-security-logs"]
    end

    subgraph "Ingestion"
        AL["Auto Loader<br/>cloudFiles · availableNow<br/>directory listing mode"]
        TI_FETCH["HTTP Fetch<br/>01_bronze_threat_intel_ingest<br/>daily · OCSF formatted"]
    end

    subgraph "Bronze Delta Tables"
        CT["cloudtrail_raw"]
        VF["vpc_flow_raw<br/>(OCSF network activity)"]
        GD["guardduty_raw"]
        CF["config_raw"]
        TIR["threat_intel_raw<br/>IOC feed rows · 14-day TTL"]
    end

    subgraph "Silver Delta Tables"
        CDC["config_cdc<br/>Normalized CDC rows<br/>per resource type"]
        TIN["threat_intel_network<br/>Deduplicated IOCs<br/>MERGE on ioc_id · TTL-managed"]
    end

    subgraph "Gold Delta Tables"
        EC2["ec2_inventory<br/>Current-state per instance<br/>ENIs, volumes, SGs joined"]
        ALERTS["alerts<br/>vpc_flow × threat_intel joins<br/>MERGE on alert_id"]
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

    S3A --> P1 & P2 & P3 & P4
    S3B --> P1 & P2 & P3 & P4
    P1 --> AL --> CT
    P2 --> AL --> VF
    P3 --> AL --> GD
    P4 --> AL --> CF
    FEODO & ET & IPSUM --> TI_FETCH --> TIR

    CF -->|"CDF / incremental"| CDC
    CDC -->|"window + MERGE"| EC2
    TIR -->|"MERGE on ioc_id · daily"| TIN
    VF & TIN -->|"watermark join<br/>10-min incremental<br/>MERGE on alert_id"| ALERTS
    ALERTS -->|"CDF read · inserts only"| AFWD
    ALERTS -->|"sns:Publish per new alert"| SNS
```

## Terraform Module Dependency Graph

```mermaid
graph TD
    BOOT["bootstrap/<br/>5 resources + 1 data source<br/>Local state"]

    SAB["module.security_account_baseline<br/>9 resources + 6 data sources<br/>Hub role, managed storage"]

    WAB["module.workload_a_baseline<br/>10 resources + 2 data sources<br/>VPC, EC2"]

    WBB["module.workload_b_baseline<br/>10 resources + 2 data sources<br/>VPC, EC2"]

    WADS["module.workload_a_data_sources<br/>18 resources + 4 data sources<br/>CloudTrail, Flow, GuardDuty, Config"]

    WBDS["module.workload_b_data_sources<br/>18 resources + 4 data sources<br/>CloudTrail, Flow, GuardDuty, Config"]

    CI["module.cloud_integration<br/>7 resources<br/>Storage creds, external locations"]

    UC["module.unity_catalog<br/>8 resources<br/>Catalog, schemas, grants"]

    WC["module.workspace_config<br/>1 resource + 3 data sources<br/>Cluster policy"]

    BI["module.bronze_ingestion<br/>21 resources<br/>12 notebooks, 3 directories, 5 jobs<br/>Bronze + Silver CDC + Gold EC2<br/>+ Threat Intel + Alerts + SNS"]

    BOOT --> SAB
    SAB --> WAB & WBB
    WAB --> WADS
    WBB --> WBDS
    SAB & WADS & WBDS --> CI
    CI -->|"Phase 5.5: update external IDs"| SAB
    CI --> UC
    UC --> WC
    WC --> BI
```

## Resource Count by Module

| Module | Resources | Data Sources | Total State Entries |
|--------|-----------|-------------|---------------------|
| `bootstrap/` (separate state) | 5 | 1 | 6 |
| `security_account_baseline` | 9 | 6 | 15 |
| `workload_a_baseline` | 10 | 2 | 12 |
| `workload_b_baseline` | 10 | 2 | 12 |
| `workload_a_data_sources` | 18 | 4 | 22 |
| `workload_b_data_sources` | 18 | 4 | 22 |
| `cloud_integration` | 7 | 0 | 7 |
| `unity_catalog` | 8 | 0 | 8 |
| `workspace_config` | 1 | 3 | 4 |
| `bronze_ingestion` + `sns_alerts` | 21 | 0 | 21 |
| **Total (`environments/poc/`)** | **102** | **21** | **123** |
