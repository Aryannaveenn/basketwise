# Deploying BasketWise (snapshot mode)

This guide puts BasketWise online at **https://basketwise.aryannaveen.com** on one AWS EC2 instance running **Ubuntu Server 26.04 LTS**:

- the **React app** and the **FastAPI backend** behind HTTPS (Caddy, automatic Let's Encrypt certificates)
- one fixed set of real prices: the crawl saved in `scraper/aryan_data/`, collected on **5–6 September 2026** from Coles, Woolworths, ALDI and Harris Farm

There is no scraping, no daily job and no Supabase. The prices never change, and the site says so: every page shows **"Prices as of 5 Sep 2026"** under the header.

Expect about 45 minutes, most of it waiting. **Every step is one command you paste into Terminal on your Mac**, from the repo root (`~/Desktop/basketwise`). Server work is sent over SSH: each server script is read in full before it runs, stops at the first error, and is safe to run again. The only manual parts are the AWS console clicks.

For prices that update every day, see [`deploy/LIVE-CRAWLING.md`](deploy/LIVE-CRAWLING.md) once this is running.

---

## Contents

1. [How it works](#1-how-it-works)
2. [Costs](#2-costs)
3. [Instance spec](#3-instance-spec)
4. [Before you start](#4-before-you-start)
5. [Phase 1 — Push the code to GitHub](#phase-1--push-the-code-to-github)
6. [Phase 2 — Launch the EC2 instance](#phase-2--launch-the-ec2-instance)
7. [Phase 3 — Point the subdomain at it](#phase-3--point-the-subdomain-at-it)
8. [Phase 4 — Prepare the server](#phase-4--prepare-the-server)
9. [Phase 5 — Install the app and start the site](#phase-5--install-the-app-and-start-the-site)
10. [Phase 6 — Load the snapshot](#phase-6--load-the-snapshot)
11. [Phase 7 — Budget alert and uptime check](#phase-7--budget-alert-and-uptime-check)
12. [Final checklist](#final-checklist)
13. [Day-2 operations](#day-2-operations)
14. [Troubleshooting](#troubleshooting)
15. [Security notes](#security-notes)
16. [Tearing it down](#tearing-it-down)
17. [Reference: files in deploy/](#reference-files-in-deploy)

---

## 1. How it works

```
                 https://basketwise.aryannaveen.com
                                 │
                 ┌───────────────▼────────────────┐   EC2 t3.micro (Sydney)
                 │  Caddy  (ports 80/443, HTTPS)  │   Ubuntu 26.04 + Docker
                 │   /        → React build       │
                 │   /api/*   → FastAPI container │
                 └───────┬────────────────────────┘
                         │ reads (read-only)
            /srv/basketwise/data/basketwise.db + warm/
                         ▲
                         │  daily.sh --no-crawl   (run once, in Phase 6)
                         │   1. build_master   hyper_*.db → master.db
                         │   2. build_api_db   → basketwise.db + warm/
                         │   3. validate       refuse a bad build
                         │   4. publish + restart the API
                         │
            ~/basketwise/scraper/data/hyper_*.db   ◄── copied from your Mac's scraper/aryan_data/
```

**Why the build runs on the server.** You copy the raw crawl files (`hyper_*.db`), not a finished `basketwise.db`, and the server rebuilds from them. That keeps `basketwise.db` and its pre-rendered `warm/` responses from the same build, and it applies the current build code to the old data. One fix matters here: crawls made before the fix in `scraper/internal/stores/aldi/aldi.go` marked every ALDI product unavailable, and the build now corrects that. Expected result: **39,101 products, 45,182 offers, 5,334 products comparable across stores, all 4 retailers**.

**Why the API serves SQLite.** It reads a 41 MB read-only file, with full-text search and everything pre-computed at build time, so it holds about 250 MB of RAM and stays fast on a free-tier instance.

**What visitors see:**

- Real prices from early September 2026, with the date on every page. The date comes from `/api/health`'s `data_as_of` field (when the prices were collected), not `built_at` (when the server ran the build).
- No price-trend chart. It only draws with two or more days of history from the last 30 days, and the snapshot has neither. The product page shows the price spread across stores instead.
- `was $X` specials as they were in September.

---

## 2. Costs

Free-tier terms change. **Check the linked pages on the day you launch.** This was correct as of November 2026.

AWS free tier depends on when your account was created:

| Account created | What's free |
|---|---|
| **Before 15 July 2025** | 750 hours/month of `t2.micro` or `t3.micro` for 12 months from sign-up, 30 GB of EBS storage, and 750 hours/month of public IPv4 |
| **On or after 15 July 2025** | The "Free plan": sign-up credits that pay for usage for a limited period. Eligible types include `t3.micro`, `t3.small`, `t4g.micro`, `t4g.small`. Storage and the public IP are paid from the credits. |

Costs to know about:

- **Public IPv4:** US$0.005/hour, about US$3.60/month, unless your free tier covers it.
- **An Elastic IP that isn't attached to a running instance is billed.** Release it if you stop using it.
- **T3 "Unlimited" CPU mode** (on by default for T3) can bill for CPU bursts. This guide switches it to **Standard**, so the instance slows down instead of charging you.
- **Route 53:** you already pay for the `aryannaveen.com` hosted zone. Adding a record costs nothing extra; DNS queries cost cents at this scale.

Sources: [AWS Free Tier](https://aws.amazon.com/free/), [EC2 free tier usage](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-free-tier-usage.html), [Route 53 pricing](https://aws.amazon.com/route53/pricing/).

---

## 3. Instance spec

| Setting | Value | Why |
|---|---|---|
| Region | **Asia Pacific (Sydney) `ap-southeast-2`** | closest to Australian shoppers |
| Instance type | **`t3.micro`** (2 vCPU, 1 GiB) | the API needs about 250 MB; swap covers the one-off build |
| Architecture | **x86_64** | |
| AMI | **Ubuntu Server 26.04 LTS (HVM), SSD Volume Type, 64-bit (x86)** | Canonical's official image, supported to April 2031 |
| Root volume | **20 GiB gp3** | the crawl files and build use under 1 GB; Docker images and the frontend build use a few more |
| Swap | **4 GiB** swap file (Phase 4) | headroom for the build and the frontend build on 1 GiB of RAM |
| CPU credits | **Standard** (not Unlimited) | no surprise CPU charges |
| Public IP | **Elastic IP** | stable address for DNS and HTTPS |
| Inbound ports | 22 from *your IP only*; 80 and 443 from anywhere | |

Memory at runtime: API ≤ 512 MB (hard cap), Caddy ≤ 128 MB, the OS ≈ 150 MB.

---

## 4. Before you start

You need:

- [ ] An **AWS account** with access to the **`aryannaveen.com` hosted zone in Route 53**
- [ ] **`scraper/aryan_data/`** on your Mac. It's gitignored, so this folder is the **only copy** of the snapshot. Back it up (an external drive or cloud storage) before you start.
- [ ] On your Mac: `ssh`, `rsync`, `git`, `dig`, `jq` (all built into current macOS)

---

## Phase 1 — Push the code to GitHub

```bash
cd ~/Desktop/basketwise && git add -A && if git diff --cached --name-only | grep -qE 'scraper/(data|aryan_data)/|\.db($|-)|node_modules/'; then echo "STOP: crawl data or node_modules staged -- fix .gitignore"; else git commit -m "Snapshot-mode deploy"; git push origin main; fi
```

`.gitignore` keeps out `node_modules`, the crawl databases, the `hyperscrape` binary, cookies and HAR captures. GitHub rejects files over 100 MB, so the command refuses to commit if any crawl data is staged. "nothing to commit" just means it's already pushed.

---

## Phase 2 — Launch the EC2 instance

### 2.1 Launch (AWS console)

In the [EC2 console](https://console.aws.amazon.com/ec2/), set the region selector (top right) to **Asia Pacific (Sydney) ap-southeast-2**, then **Instances → Launch instances**:

| Section | Value |
|---|---|
| **Name** | `basketwise` |
| **Application and OS Images** | *Ubuntu* → **Ubuntu Server 26.04 LTS (HVM), SSD Volume Type**, architecture **64-bit (x86)**. Check the publisher is Canonical and it says *Free tier eligible*. |
| **Instance type** | `t3.micro` |
| **Key pair** | **Create new key pair** → name `basketwise`, type **ED25519**, format **.pem** → it downloads `basketwise.pem` |
| **Network settings** → **Edit** | VPC: default. Auto-assign public IP: **Enable**. Firewall: **Create security group**, name `basketwise-sg`. |
| Inbound rule 1 | Type **SSH**, port 22, Source **My IP** |
| Inbound rule 2 | **Add security group rule** → Type **HTTP**, port 80, Source **Anywhere** (`0.0.0.0/0`) |
| Inbound rule 3 | **Add security group rule** → Type **HTTPS**, port 443, Source **Anywhere** (`0.0.0.0/0`) |
| **Configure storage** | **20** GiB, **gp3** |
| **Advanced details** → **Credit specification** | **Standard** |

Click **Launch instance**. Then attach a stable address: **Network & Security → Elastic IPs → Allocate Elastic IP address → Allocate**, select it → **Actions → Associate Elastic IP address** → the `basketwise` instance → **Associate**. Note the address.

### 2.2 SSH access (one command)

Put the Elastic IP in `EIP=` and run. It moves the key into `~/.ssh`, adds a `basketwise` SSH alias, and connects once to confirm the OS:

```bash
EIP=3.25.10.7; mv ~/Downloads/basketwise.pem ~/.ssh/ && chmod 400 ~/.ssh/basketwise.pem && printf '\nHost basketwise\n  HostName %s\n  User ubuntu\n  IdentityFile ~/.ssh/basketwise.pem\n  ServerAliveInterval 60\n' "$EIP" >> ~/.ssh/config && ssh -o StrictHostKeyChecking=accept-new basketwise 'grep PRETTY_NAME /etc/os-release'
```

It should print `PRETTY_NAME="Ubuntu 26.04 LTS"` (possibly with a point release). If your home IP changes later and SSH times out, edit `basketwise-sg` and set the SSH rule back to *My IP*.

---

## Phase 3 — Point the subdomain at it

Caddy needs `basketwise.aryannaveen.com` to point at the Elastic IP before it can get an HTTPS certificate. `aryannaveen.com` is hosted in **Route 53**, and the bare domain already points at another server, so add only the subdomain. Leave the existing records alone.

**Route 53 → Hosted zones → `aryannaveen.com` → Create record:**

| Field | Value |
|---|---|
| Record name | `basketwise` |
| Record type | **A** |
| Value | the Elastic IP |
| TTL | `300` |
| Routing policy | Simple routing |

Leave **Alias** off (an Elastic IP isn't an alias target), then **Create records**. This waits until the record is live (usually under a minute):

```bash
EIP=3.25.10.7; until [ "$(dig +short basketwise.aryannaveen.com)" = "$EIP" ]; do echo waiting for DNS...; sleep 10; done; echo "DNS ready"
```

The domain has no CAA records, so Let's Encrypt is allowed to issue for it. If you ever add CAA records, include `0 issue "letsencrypt.org"`.

---

## Phase 4 — Prepare the server

One command installs updates, sets the Sydney timezone, adds 4 GiB of swap, installs Docker from Docker's apt repository and `uv` (the Python tool the build uses), then reboots. It waits for the server to come back and prints a check of each part. About 5 minutes.

```bash
ssh basketwise 'bash -se' <<'SETUP'; sleep 20; until ssh -o ConnectTimeout=5 basketwise true 2>/dev/null; do sleep 5; done; ssh basketwise 'docker run --rm hello-world | grep -m1 Hello; docker compose version; ~/.local/bin/uv --version; swapon --show; timedatectl | grep "Time zone"'
{
set -euo pipefail
APT="sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get -yq"

# Updates, timezone, tools, automatic security patches
$APT update && $APT upgrade
sudo timedatectl set-timezone Australia/Sydney
$APT install git curl rsync sqlite3 jq tmux unattended-upgrades ca-certificates
sudo dpkg-reconfigure -f noninteractive unattended-upgrades

# 4 GiB swap (skipped if it already exists)
if [ ! -f /swapfile ]; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swap.conf >/dev/null && sudo sysctl --system >/dev/null

# Docker, from Docker's apt repository (lists Ubuntu 26.04 "resolute" as supported)
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
$APT update && $APT install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu

# uv (installs to ~/.local/bin)
curl -LsSf https://astral.sh/uv/install.sh | sh

echo "=== setup done; rebooting (applies updates and the docker group) ==="
sudo reboot
}
SETUP
```

Expected output, after a `Connection ... closed` line from the reboot: `Hello from Docker!`, a `Docker Compose version` line, a `uv` version, a 4G `/swapfile`, and `Time zone: Australia/Sydney`. If it stops earlier, the line above the error says which step failed; fix it and run the command again.

**Notes for Ubuntu 26.04:**

- It uses `sudo-rs` and the Rust `uutils` coreutils by default. The script avoids `sudo -E`, passing settings with `sudo env ...` instead, and uses only basic coreutils options, so nothing needs switching.
- `NEEDRESTART_MODE=a` stops apt pausing to ask which services to restart.
- Docker is installed from its own repository, as Docker recommends for servers, not from the `get.docker.com` convenience script. Sources: [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/), [Linux post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/).

---

## Phase 5 — Install the app and start the site

**If the GitHub repo is private,** first give the server a read-only deploy key. This prints a public key:

```bash
ssh basketwise 'ssh-keygen -q -t ed25519 -f ~/.ssh/github_deploy -N "" -C basketwise-ec2 && printf "Host github.com\n  IdentityFile ~/.ssh/github_deploy\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n" >> ~/.ssh/config && cat ~/.ssh/github_deploy.pub'
```

Add it in GitHub (the repo → **Settings → Deploy keys → Add deploy key**, *Allow write access* **off**), and in the command below change `REPO=` to `git@github.com:Aryannaveenn/basketwise.git`.

**Then one command** clones the repo, creates the server folders and the settings file, installs the build's Python packages, starts the API and Caddy, and builds and publishes the frontend. About 5–10 minutes; the frontend build is the slow part on a `t3.micro`.

```bash
ssh basketwise 'bash -se' <<'APP'
{
set -euo pipefail
REPO=https://github.com/Aryannaveenn/basketwise.git
export PATH="$HOME/.local/bin:$PATH"

# Code at /home/ubuntu/basketwise (the scripts expect this path)
[ -d ~/basketwise/.git ] || git clone -q "$REPO" ~/basketwise

# Folders: published data, website, validation state; crawl files go in scraper/data
sudo mkdir -p /srv/basketwise/data /srv/basketwise/www /srv/basketwise/state
sudo chown -R ubuntu:ubuntu /srv/basketwise
mkdir -p ~/basketwise/scraper/data

# Settings file: the example is already set up for snapshot mode
[ -f /srv/basketwise/basketwise.env ] || install -m 600 ~/basketwise/deploy/basketwise.env.example /srv/basketwise/basketwise.env
grep -qx 'DOMAIN=basketwise.aryannaveen.com' /srv/basketwise/basketwise.env || { echo "DOMAIN wrong in basketwise.env"; exit 1; }
grep -qx 'SUPABASE_URL=' /srv/basketwise/basketwise.env || { echo "SUPABASE_URL must be blank in snapshot mode"; exit 1; }

# Python packages for the build (ready-made wheels for any Python 3; no compiler needed)
(cd ~/basketwise/scraper && UV_PROJECT_ENVIRONMENT=.venv-hs uv sync -q)

# API + Caddy, then the React build
~/basketwise/deploy/scripts/up.sh
~/basketwise/deploy/scripts/frontend.sh

sleep 5
echo "=== Caddy certificate status ==="
docker logs basketwise-caddy 2>&1 | grep -iE 'certificate obtained|obtain.*error|challenge failed' | tail -3 || true
}
APP
```

What it starts:

- **`basketwise-api`**: FastAPI on the internal Docker network only (no host port). Until Phase 6 publishes a database, it restarts in a loop with `database not found`. That's expected.
- **`basketwise-caddy`**: listens on 80/443 and gets a Let's Encrypt certificate for `basketwise.aryannaveen.com` on first start. The last lines should include `certificate obtained successfully`. If it shows nothing yet, give it a minute, then see [Troubleshooting](#https-doesnt-work--caddy-cant-get-a-certificate).
- **The frontend** is built in a temporary `node:24-slim` container with `VITE_API_BASE=/api`, then copied to `/srv/basketwise/www/`. No Node install is needed on the server.

Open `https://basketwise.aryannaveen.com`. The page shell loads; products appear after Phase 6.

> **Faster frontend build on your Mac:** `(cd frontend && VITE_API_BASE=/api pnpm build) && rsync -a --delete frontend/dist/ basketwise:/srv/basketwise/www/`

---

## Phase 6 — Load the snapshot

One command copies the crawl files (about 365 MB), builds, validates and publishes on the server, then checks the live API:

```bash
rsync -aP scraper/aryan_data/hyper_*.db scraper/aryan_data/hyper_*.db-wal basketwise:basketwise/scraper/data/ && ssh basketwise 'set -a; . /srv/basketwise/basketwise.env; set +a; ~/basketwise/deploy/scripts/daily.sh --no-crawl' && curl -s https://basketwise.aryannaveen.com/api/health | jq '{data_as_of, product_count, offer_count, retailers}'
```

The `-wal` files matter: `hyper_woolworths.db-wal` holds about 7 MB of products not yet merged into the main file. The build takes a few minutes on a `t3.micro`. Its output includes:

```
products=39,101 offers=45,182 comparable=5,334 retailers=['aldi', 'coles', 'harrisfarm', 'woolworths']
validation ok
api healthy
SUPABASE_URL / SUPABASE_SECRET_KEY not set; skipping
```

and the final check prints:

```json
{ "data_as_of": "2026-09-05T03:49:40Z", "product_count": 39101, "offer_count": 45182,
  "retailers": ["aldi", "coles", "harrisfarm", "woolworths"] }
```

`validate.py` refuses to publish a build with fewer than 20,000 products or offers, fewer than 2,000 comparable products, fewer than 2 retailers, or a retailer with under half its offers available. If it fails, the site keeps serving whatever was there before (nothing, on the first run). See [Troubleshooting](#validation-fails).

Then open `https://basketwise.aryannaveen.com` (the "Prices as of 5 Sep 2026" strip shows under the header) and `https://basketwise.aryannaveen.com/api/docs` (Swagger).

Leave the `hyper_*.db` files on the server. Rebuilds after code changes need them.

---

## Phase 7 — Budget alert and uptime check

- **AWS budget alert:** **Billing and Cost Management → Budgets → Create budget → Use a template → Zero spend budget** → your email → **Create**. You get an email the moment anything costs money.
- **Uptime check (optional):** point a free monitor (UptimeRobot, Better Stack, healthchecks.io) at `https://basketwise.aryannaveen.com/api/health`.

Health check whenever you like:

```bash
ssh basketwise 'df -h / | tail -1; free -h | head -2; docker stats --no-stream --format "{{.Name}} {{.MemUsage}}"; ~/basketwise/deploy/scripts/up.sh ps --format "{{.Name}} {{.Status}}"'
```

Keep a few GB of disk free; the API should sit around 250 MB.

---

## Final checklist

- [ ] `https://basketwise.aryannaveen.com` loads over HTTPS, and Browse shows real products
- [ ] Every page shows "Prices as of 5 Sep 2026" under the header
- [ ] Search works, including a typo: `mlik` finds milk
- [ ] A basket on **Compare** includes ALDI where ALDI stocks the items
- [ ] `https://basketwise.aryannaveen.com/api/docs` loads Swagger, and `/api/` redirects to it
- [ ] An AWS zero-spend budget is set
- [ ] `scraper/aryan_data/` is backed up somewhere other than your Mac

---

## Day-2 operations

### Deploy new code

One command pushes from your Mac, resets the server to `origin/main`, and rebuilds the API and the frontend:

```bash
git push origin main && ssh basketwise 'cd ~/basketwise && git fetch -q origin && git reset -q --hard origin/main && deploy/scripts/up.sh && deploy/scripts/frontend.sh'
```

The server never has hand edits, so reset rather than merge. `scraper/data/` is gitignored, so the reset leaves the crawl files alone.

If the change touched **`scraper/tui/`, `backend/shapes.py`, `backend/data/essentials.txt` or `scraper/uv.lock`**, also refresh the packages and rebuild the data from the snapshot:

```bash
ssh basketwise 'export PATH="$HOME/.local/bin:$PATH"; cd ~/basketwise/scraper && UV_PROJECT_ENVIRONMENT=.venv-hs uv sync -q && set -a && . /srv/basketwise/basketwise.env && set +a && ~/basketwise/deploy/scripts/daily.sh --no-crawl'
```

The same snapshot goes in, so the counts and `data_as_of` stay the same; only `build_id` and `built_at` change.

### Logs and restarts

```bash
ssh basketwise '~/basketwise/deploy/scripts/up.sh logs --tail 50 api'        # API logs
ssh basketwise '~/basketwise/deploy/scripts/up.sh restart caddy'             # restart Caddy
```

### Rebuild onto a fresh instance

Repeat Phases 2–6. Update the Route 53 record if the Elastic IP changes, and remove the old `basketwise` entry from `~/.ssh/config` and `~/.ssh/known_hosts` first. Phase 6 needs your Mac's `scraper/aryan_data/` or your backup of it.

---

## Troubleshooting

### SSH times out

Your home IP changed. Edit `basketwise-sg` and set the SSH rule back to *My IP*.

### HTTPS doesn't work / Caddy can't get a certificate

```bash
dig +short basketwise.aryannaveen.com; ssh basketwise 'grep ^DOMAIN= /srv/basketwise/basketwise.env; docker logs basketwise-caddy 2>&1 | tail -30'
```

- The `dig` output must be the Elastic IP, and `DOMAIN` must be exactly `basketwise.aryannaveen.com`.
- The security group must allow **80 and 443 from anywhere**. Let's Encrypt connects from many IPs.
- Don't delete the `caddy_data` volume. Let's Encrypt rate-limits repeated requests for the same hostname.

### The site loads but shows no products

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://basketwise.aryannaveen.com/api/health; ssh basketwise 'docker logs basketwise-api 2>&1 | tail -20'
```

- A `502` means the API isn't running. `database not found at /data/basketwise.db` means nothing has been published yet: run Phase 6.
- DevTools → Network: requests should go to `/api/...`. If they go to `basket.taskglass.work`, the frontend was built without `VITE_API_BASE=/api`. Re-run `frontend.sh` (the Day-2 deploy command does).

### The "Prices as of" strip doesn't appear

`curl -s https://basketwise.aryannaveen.com/api/health | jq .data_as_of` returns `null`: the published `basketwise.db` was built by a version of `build_api_db.py` from before `data_as_of` existed. Run the Day-2 deploy command, then the data rebuild command.

### Validation fails

Read the reason in the `VALIDATION FAILED:` lines.

- `aldi: only 0/2627 offers available`: the server's code is older than the ALDI availability fix. Run the Day-2 deploy and rebuild commands.
- `only N products` with N far below 39,101: the crawl files didn't all copy. Check with `ssh basketwise 'ls -la ~/basketwise/scraper/data/hyper_*'`: you need all four `hyper_*.db` files plus `hyper_woolworths.db-wal`. Then re-run Phase 6.
- `products dropped ... (>20%)` after an intentional change: `ssh basketwise 'rm /srv/basketwise/state/last_build.json'` once to reset the baseline.

### The build was killed / out of memory

```bash
ssh basketwise 'sudo dmesg -T | grep -i -E "killed process|out of memory" | tail; swapon --show'
```

Check swap is on. If it still fails, move up to `t3.small`: stop the instance → **Actions → Instance settings → Change instance type** → start. The Elastic IP stays attached.

### A shell command in the publish step fails oddly

Ubuntu 26.04's default `uutils` coreutils are meant to be drop-in replacements, and `daily.sh` only uses basic `cp`, `mv`, `rm`, `chmod` and `seq` options. If one of them misbehaves, Ubuntu documents how to switch back to GNU coreutils (the `coreutils-from-gnu` package). Note the exact error first.

### Disk filling up

```bash
ssh basketwise 'du -sh ~/basketwise/scraper/data/* | sort -h | tail; docker system prune -f'
```

---

## Security notes

- **No secrets in git**, and in snapshot mode none on the server either. `basketwise.env` still lives only in `/srv/basketwise`, with mode `600`.
- **SSH is limited to your IP**, with key-only login (Ubuntu's AMI default).
- **The API container** runs as a non-root user, with a read-only filesystem, all Linux capabilities dropped, a 512 MB memory cap, and the database mounted read-only.
- **Only Caddy has published ports** (80/443). The API is reachable only through it.
- **Automatic security updates** are on (`unattended-upgrades`). Reboot now and then for kernel updates with `ssh basketwise sudo reboot`. The containers come back on their own (`restart: unless-stopped`).

---

## Tearing it down

1. **EC2 → Instances →** select `basketwise` → **Instance state → Terminate**. This also deletes the root volume.
2. **EC2 → Elastic IPs →** select the IP → **Actions → Release Elastic IP address**. An unattached Elastic IP is billed.
3. **EC2 → Security Groups →** delete `basketwise-sg`. **Key Pairs →** delete `basketwise`.
4. **Route 53 → `aryannaveen.com` →** delete only the `basketwise` A record. Don't delete the hosted zone; your main site depends on it.

---

## Reference: files in `deploy/`

Used in snapshot mode:

| File | Purpose |
|---|---|
| [`deploy/compose.yml`](deploy/compose.yml) | API + Caddy containers. The API runs with `--root-path /api`; Caddy strips the prefix. |
| [`deploy/Caddyfile`](deploy/Caddyfile) | HTTPS, `/api/*` → FastAPI, everything else → React build with SPA fallback |
| [`deploy/basketwise.env.example`](deploy/basketwise.env.example) | Template for `/srv/basketwise/basketwise.env`, set up for snapshot mode |
| [`deploy/scripts/up.sh`](deploy/scripts/up.sh) | `docker compose` wrapper that loads the env file |
| [`deploy/scripts/frontend.sh`](deploy/scripts/frontend.sh) | Builds the frontend in a Node container and publishes it to `/srv/basketwise/www` |
| [`deploy/scripts/daily.sh`](deploy/scripts/daily.sh) | With `--no-crawl`: build → validate → publish → restart API. Skips Supabase when its env vars are blank. |
| [`deploy/scripts/validate.py`](deploy/scripts/validate.py) | Publish gate |

Only for live crawling ([`deploy/LIVE-CRAWLING.md`](deploy/LIVE-CRAWLING.md)): `deploy/scripts/sync_supabase.py`, `deploy/supabase/schema.sql`, `deploy/systemd/`.

Related docs: [`backend/README.md`](backend/README.md) (API internals), [`scraper/README.md`](scraper/README.md) (crawler and build scripts), [`Source-of-truth.md`](Source-of-truth.md) (API contract).
