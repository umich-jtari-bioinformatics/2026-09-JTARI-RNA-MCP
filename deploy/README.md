# Deploying to AWS EC2: runbook

One small EC2 instance runs two containers: the MCP server and Caddy, which provides
HTTPS with an automatic Let's Encrypt certificate. Lab members connect with their own
API key. Each step ends with a **check**. Follows the JTARI DSS3 deployment pattern, with
Docker as the kickoff asked.

```
Lab member's Claude ──https──▶ security group (443, 80) ──▶ EC2 t3.medium / t3.large (x86, as DSS3)
                                                             ├─ caddy  :443  (certificate, /mcp → mcp:8000)
                                                             └─ mcp    :8000 (key check → tools → data/jtari.duckdb, read-only)
You (admin) ──ssh, your IP only──▶ port 22
```

Cost: about $30/month for `t3.medium` (or about $60 for `t3.large`, DSS3's size) on demand,
plus about $2.40 for a 30 GB disk and about $3.60 for a public IPv4 address. Stopping the
instance stops the compute charge.

---

## 0. Before you start

| Need | Why |
|---|---|
| Lab members use the lab's **Anthropic enterprise account** | Tool results (including unpublished LOCAL values) are processed by Claude under that account's terms |
| AWS console access in a U-M account | Launch the instance |
| This Mac: `conda activate jtari-mcp` | Build the database, copy files |

## 1. Build the database to deploy (on this Mac)

```bash
conda activate jtari-mcp
cd ~/2026-09-JTARI-RNA-MCP
export JTARI_DATA_DIR="/path/to/raw/data"   # sample_metadata.tsv + the five result folders

# EITHER public datasets only (no PI sign-off yet):
uv run jtari-build-db --exclude-dataset LOCAL --out data/deploy/jtari.duckdb
# OR everything (after sign-off):
uv run jtari-build-db --out data/deploy/jtari.duckdb
```

**Check:** the summary line shows `samples` = 445 (public only) or 679 (all).

## 2. Launch the instance (AWS console)

EC2 → **Launch instance**:

| Setting | Value |
|---|---|
| Name | `jtari-mcp` |
| AMI | **Amazon Linux 2023**, architecture **64-bit (x86)**, the current default in the list (as DSS3; don't reuse DSS3's old AMI ID) |
| Instance type | **t3.medium** (2 vCPU, 4 GB; enough) or **t3.large** (8 GB, as DSS3) |
| Credit specification (Advanced) | **Unlimited** (as DSS3) |
| Key pair | **Create new** → `jtari-mcp-pair`, type ED25519, `.pem`. A new key, not DSS3's. Save it to `~/.ssh/jtari-mcp-pair.pem` (not inside the repo folder), then `chmod 600` it |
| Network → security group | **Create**, named `jtari-mcp-sg`, with 3 inbound rules: **SSH 22 from My IP**; **HTTPS 443 from Anywhere**; **HTTP 80 from Anywhere** (80 is only for the certificate check) |
| Storage | 30 GiB gp3 (as DSS3) |
| Advanced → IAM instance profile | DSS3's existing **InstanceRole** (AmazonSSMManagedInstanceCore + CloudWatchAgentServerPolicy): lets you open a shell from the console via Session Manager if SSH or your IP changes |
| Advanced → Termination protection | **Enable** (as DSS3): prevents an accidental delete |

Then **Elastic IPs → Allocate → Associate** it with `jtari-mcp`. A fixed IP keeps the
hostname stable across reboots.

**Your hostname:** take the Elastic IP, e.g. `18.191.1.2`, and write it with dashes:
`18-191-1-2.sslip.io`. sslip.io maps that name to the IP for free, so Caddy can get a
real certificate. Written below as `HOST`.

**Check:** `ssh -i ~/.ssh/jtari-mcp-pair.pem ec2-user@<elastic-ip> 'uname -m'` prints `x86_64`.

## 3. Install Docker on the instance

```bash
ssh -i ~/.ssh/jtari-mcp-pair.pem ec2-user@<elastic-ip>
sudo dnf install -y docker rsync
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
# compose plugin (not packaged in AL2023)
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -sSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
  https://github.com/docker/compose/releases/download/v2.39.4/docker-compose-linux-x86_64
sudo curl -sSL -o /usr/local/lib/docker/cli-plugins/docker-buildx \
  https://github.com/docker/buildx/releases/download/v0.29.1/buildx-v0.29.1.linux-amd64
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-buildx
exit   # log out and back in so the docker group applies
```

**Check:** `docker compose version` prints v2.39.4 and `docker buildx version` v0.29.1.

## 4. Get the code (from GitHub) and the database (from this Mac)

The repo is public, so the server clones it directly; updates are then `git pull`.
On the instance:

```bash
sudo dnf install -y git
git clone https://github.com/umich-jtari-bioinformatics/2026-09-JTARI-RNA-MCP.git ~/jtari-mcp
mkdir -p ~/jtari-mcp/data
```

The database is not in git (it holds unpublished data). Copy it from this Mac:

```bash
cd ~/2026-09-JTARI-RNA-MCP
shasum -a 256 data/deploy/jtari.duckdb
rsync -av --progress -e "ssh -i <path-to>/jtari-mcp-pair.pem" data/deploy/jtari.duckdb ec2-user@<elastic-ip>:~/jtari-mcp/data/
```

**Check:** on the instance, `sha256sum ~/jtari-mcp/data/jtari.duckdb` prints the same
fingerprint as on the Mac.

## 5. Configure, create keys, start

On the instance:

```bash
cd ~/jtari-mcp
echo "JTARI_PUBLIC_HOST=HOST" > .env              # e.g. 18-191-1-2.sslip.io
mkdir -p keys && sudo chown 10001:10001 keys && sudo chmod 700 keys
docker compose build
docker compose run --rm mcp jtari-keys add gourab  # prints the key ONCE; store it (step 7)
docker compose up -d
docker compose ps                                   # both "running", mcp "healthy"
```

**Issue keys only to people who use the lab's Anthropic enterprise account.** Through a
personal Claude account, LOCAL results would be processed under personal-account terms.

The server **refuses to start without a keys file** (fail closed). Each lab member gets
their own key: `docker compose run --rm mcp jtari-keys add <name>`. Revoke one with
`jtari-keys remove <name>`; it takes effect immediately, without a restart.

**Check:** from this Mac, `curl -s https://HOST/health` prints `{"status":"ok"}`, and the
browser shows a valid lock icon at `https://HOST/`.

## 6. Test the deployed server (from this Mac)

```bash
JTARI_REMOTE_URL=https://HOST/mcp JTARI_REMOTE_KEY=<your key> \
  uv run pytest -q -s tests/test_deployed.py
```

**Check:** 2 passed, plus a line listing the datasets served. Then follow the logs:
`docker compose logs -f mcp` shows `user=gourab method=tools/call tool=…` per call.

## 7. Connect clients

Store each key in the macOS Keychain, never in a config file or a chat:

```bash
security add-generic-password -U -s jtari-mcp -a "$USER" -w     # paste ONLY the jtari_… key at the prompt
security find-generic-password -s jtari-mcp -w | cut -c1-6          # check: prints jtari_
security find-generic-password -s jtari-mcp -w | tr -d '\n' | wc -c  # check: prints 49
```

**Claude Code** (any folder):

```bash
claude mcp add -t http -s user jtari-remote https://HOST/mcp \
  -H "Authorization: Bearer $(security find-generic-password -s jtari-mcp -w)"
```

**Claude Desktop:** custom connectors in Settings expect OAuth, so use the `mcp-remote`
bridge (needs Node.js) in `claude_desktop_config.json`:

```json
"jtari-remote": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "https://HOST/mcp", "--header", "Authorization:Bearer ${JTARI_KEY}"],
  "env": {"JTARI_KEY": "<key>"}
}
```

**Claude Science:** Connectors → Add → Remote, URL `https://HOST/mcp`, headers helper:
`printf '{"Authorization": "Bearer %s"}' "$(security find-generic-password -s jtari-mcp -w)"`.

## Updating

| Change | Do |
|---|---|
| Code | `cd ~/jtari-mcp && git pull && docker compose up -d --build` |
| Database | rsync the new file over the old one, then `docker compose restart mcp` |
| Add LOCAL after PI sign-off | build the full DB (step 1), rsync, restart |

After every update, rerun step 6.

## Rollback and shutdown

- **Previous code:** `git checkout <previous-commit>`, then `docker compose up -d --build`.
- **Pause:** EC2 → Instance state → **Stop**. Compute charges stop; the disk and IP still
  cost about $5/month.
- **Remove:** turn off termination protection, terminate the instance, **release the Elastic
  IP**, delete the security group.
- **Your home IP changed** (SSH stops working): edit the security group's SSH rule to the
  new IP, or open a shell from the console with **Connect → Session Manager**.

## Security checklist

- [ ] Keys file exists; the server refuses to start without it (tested)
- [ ] SSH only from your IP; 443/80 open (every request needs a key)
- [ ] `JTARI_PUBLIC_HOST` set, otherwise Caddy-forwarded requests get HTTP 421 (tested)
- [ ] The database is mounted read-only; the container runs as a non-root user with a read-only filesystem
- [ ] No keys in git, configs or chats; send each key 1-on-1
- [ ] Keys only for users of the lab's Anthropic enterprise account
