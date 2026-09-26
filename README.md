# Oracle Always Free A1 hunter

GitHub Actions checks existing Oracle Cloud capacity every 15 minutes without leaving a PC on. It requests only `VM.Standard.A1.Flex`, 2 OCPUs, 12 GB RAM, Canonical Ubuntu 22.04 ARM64, and a 50 GB Balanced boot volume. The instance name is `lt-screen` in `ap-singapore-1`, with a public IPv4 address on an **existing** subnet.

## Setup

1. In Oracle Cloud Singapore, prepare an existing VCN and subnet that permits public IP addresses, and an API signing key with permissions to list compartments, ADs, instances, shapes, images, boot and block volumes and to create an instance in the target compartment. The API user needs visibility across the compartments whose storage and A1 instances should count toward the allowance. If it cannot list them, the run stops without creating a VM.
2. In GitHub open **Settings → Secrets and variables → Actions → New repository secret** and add all seven secrets:

   | Secret | Value |
   | --- | --- |
   | `OCI_TENANCY_OCID` | Oracle tenancy OCID |
   | `OCI_USER_OCID` | API user OCID |
   | `OCI_FINGERPRINT` | Fingerprint of the uploaded API public key |
   | `OCI_PRIVATE_KEY` | Full PEM **API signing private key**, including BEGIN/END lines (never an SSH private key) |
   | `OCI_COMPARTMENT_OCID` | Compartment in which to create the VM |
   | `OCI_SUBNET_OCID` | Existing subnet OCID, regional or within an eligible AD |
   | `OCI_SSH_PUBLIC_KEY` | Public SSH key line, e.g. `ssh-ed25519 ...` |

3. Once all seven secrets are present, open **Actions → Oracle Always Free A1 hunter → Run workflow** to try immediately. The cron schedule also runs every 15 minutes (GitHub may delay scheduled runs). **Do not add secrets until you are ready for the next scheduled run to attempt VM creation.** A manual run before that point stops at the secret check and makes no OCI request.

The workflow checks all accessible compartments for existing active A1 instances or `lt-screen`, then counts visible non-terminated boot and block volumes. If another 50 GB could exceed 200 GB, it stops. It checks each AD for A1 and tries capacity in order. OCI capacity errors end a run successfully when all eligible ADs are full. Other API and permission errors fail closed. After creation, subsequent scheduled runs detect the existing instance and skip launch. To stop scheduled runs entirely, disable the workflow from its Actions page; the script does not request a GitHub write token to disable itself.

Only seven repository secrets are read. The API private key is written to a temporary file on the GitHub runner with mode `0600` and removed at process exit. Never commit either private key. The script does not create a VCN, subnet, reserved IP, load balancer, database, or extra data volume. Confirm the tenancy's complete storage usage and Always Free eligibility in Oracle Cloud: the script can count only resources visible to the API user in the configured region.
