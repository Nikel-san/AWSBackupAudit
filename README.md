# AWSBackupAudit

AWSBackupAudit audits AWS Backup coverage for production resources in a target AWS account.

The script discovers EC2 instances and RDS databases tagged `env=prod`, checks whether each resource is covered by AWS Backup plans (explicit ARN assignments and tag-based selections), and writes a CSV report.

## Options

| Option | Required | Description |
|---|---|---|
| `-a`, `--account-id` | Yes | AWS account ID to audit. |
| `-r`, `--region` | No | AWS region to query. Default: `us-east-1`. |
| `-o`, `--output` | No | Output CSV path. Default: `backup_audit_<account>_<timestamp>.csv`. |
| `-k`, `--aws-access-key-id` | No | AWS access key ID. Can be omitted when environment variables or default credential chain are used. |
| `-s`, `--aws-secret-access-key` | No | AWS secret access key. Must be paired with access key ID when provided. |
| `-t`, `--aws-session-token` | No | AWS session token (for temporary credentials). |

Credential input behavior:
- If `-k/--aws-access-key-id` and `-s/--aws-secret-access-key` are passed, the script uses those credentials.
- If CLI credentials are omitted, the script falls back to environment variables and then the default boto3 credential chain.
- If only one of access key or secret key is provided, the script exits with an error.

## Environment Variables

The script can use these environment variables for credential fallback:

- `AWS_ACCESS_KEY_ID` - AWS access key ID.
- `AWS_SECRET_ACCESS_KEY` - AWS secret access key.
- `AWS_SESSION_TOKEN` - Optional session token for temporary credentials.

At startup, credentials are validated with STS (`GetCallerIdentity`).
If the authenticated caller account differs from `--account-id`, the script prints a warning and continues.

## Usage

Basic usage with account ID only (uses default credential chain):

```bash
python AwsBackupAudit.py --account-id 123456789012
```

Specify region and output file:

```bash
python AwsBackupAudit.py \
	--account-id 123456789012 \
	--region us-west-2 \
	--output backup_audit_123456789012.csv
```

Pass credentials via short flags:

```bash
python AwsBackupAudit.py \
	-a 123456789012 \
	-r us-east-1 \
	-k "$AWS_ACCESS_KEY_ID" \
	-s "$AWS_SECRET_ACCESS_KEY" \
	-t "$AWS_SESSION_TOKEN"
```

Use environment variable fallback for credentials:

```bash
export AWS_ACCESS_KEY_ID=your_access_key
export AWS_SECRET_ACCESS_KEY=your_secret_key
export AWS_SESSION_TOKEN=your_session_token

python AwsBackupAudit.py -a 123456789012
```

## CSV Output

The CSV report includes one row per discovered resource with these columns:

- `Account ID` - Target account ID provided to the script.
- `Object Type` - Resource type (`EC2 Instance` or `RDS Database`).
- `Object Name` - EC2 Name tag value (or instance ID when Name is absent) or RDS DB identifier.
- `Backup Plan Name` - Backup plan name(s) covering the resource. Empty when no coverage is detected.