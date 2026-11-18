# Teachmint-CI
It will start the server from 9 AM–5 PM IST to run Gerrit, Jenkins and Artifactory

The workflow targets `android-aosp` in project `teachmint-uat`, zone `asia-south1-c`.
It uses a GitHub-hosted runner and Google Cloud IAP to reach the private VM.

## Schedule and manual controls

- Monday–Friday at 09:00 IST: start the VM and existing Gerrit, Jenkins, Artifactory and PostgreSQL containers, then start the Jenkins agent and check service health.
- Monday–Friday at 17:00 IST: put Jenkins into quiet-down, wait for running Jenkins jobs and detected command-line builds, then stop the services and power off the VM.
- Actions → Teachmint CI server → Run workflow provides `start`, `stop` and `status`.
- A pending shutdown is cancelled at the next working-day opening. A manual `start` also cancels it.
- The server handles deferred shutdown through a transient systemd service, so the GitHub job can finish while an existing build continues.

GitHub scheduled runs can be delayed. Scheduled events that arrive in the wrong working-hours window are skipped.

## One-time Google Cloud authentication

A Google Cloud IAM administrator must configure Workload Identity Federation for this repository and a dedicated service account. Add these repository variables under Settings → Secrets and variables → Actions → Variables.

| Variable | Value |
| --- | --- |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full provider resource name, such as `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL/providers/PROVIDER` |
| `GCP_SERVICE_ACCOUNT` | Email address of the dedicated Google Cloud service account |

Restrict the trust to repository ID `1462974434`, repository owner ID `338058186`, branch `refs/heads/main`, and this workflow.

```text
assertion.repository_id == '1462974434' &&
assertion.repository_owner_id == '338058186' &&
assertion.ref == 'refs/heads/main' &&
assertion.workflow_ref == 'vishnudeepak22/Teachmint-CI/.github/workflows/teachmint-ci.yml@refs/heads/main'
```

The GitHub identity needs permission to impersonate that service account. The service account needs the following access.

- Permission to get and start the selected VM and read its project metadata.
- IAP TCP tunnel access to `10.201.0.119:22`.
- Permission to add an expiring SSH key to this VM's instance metadata for user `uat`. This existing VM uses metadata SSH keys.
- Any service-account act-as permissions required by the project's SSH policy for the service account attached to the VM.

The IAP SSH firewall rule must allow TCP 22 from `35.235.240.0/20`. Scope the permissions and firewall access to this VM. Follow the [Google authentication action setup](https://github.com/google-github-actions/auth#setup) and [IAP SSH documentation](https://cloud.google.com/compute/docs/connect/ssh-using-iap).

## Existing server installation

The control helper uses `/home/uat/Teachmint-CI` and these existing containers.

```text
mt83x1-gerrit-migrated
teachmint-jenkins-server
teachmint-artifactory
teachmint-artifactory-db
```

It uses the existing `teachmint-server-jenkins-agent.service` and the existing local Jenkins API token at `/home/uat/.jenkins-agent/jenkins-api-token` for user `vishnu`. That token stays on the server. User `uat` needs the existing passwordless sudo access to manage Docker and systemd.

The `stop` action starts `teachmint-ci-scheduled-stop.service`. Inspect its progress on the server with this command.

```bash
sudo journalctl -u teachmint-ci-scheduled-stop.service
```

If the helper cannot confirm Jenkins is idle, it keeps waiting. `start` cancels the pending shutdown and cancels Jenkins quiet-down. Validate a manual `status`, then `start`, before relying on the schedule.

The repository contains workflow and control code. Runtime configuration, credentials, source trees and service data live on the VM. Stopping the VM continues to incur persistent-disk storage charges.
