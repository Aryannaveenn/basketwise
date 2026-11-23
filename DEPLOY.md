# Deploying BasketWise (snapshot mode)

This guide puts BasketWise online at **https://basketwise.aryannaveen.com** on one AWS EC2 instance running **Ubuntu Server 26.04 LTS**:

- the **React app** and the **FastAPI backend** behind HTTPS (Caddy, automatic Let's Encrypt certificates)
- one fixed set of real prices: the crawl saved in `scraper/aryan_data/`, collected on **5–6 September 2026** from Coles, Woolworths, ALDI and Harris Farm

There is no scraping, no daily job and no Supabase. The prices never change, and the site says so: every page shows **"Prices as of 5 Sep 2026"** under the header.

Expect about 45–60 minutes. Commands marked **Mac** run on your laptop from the repo root (`~/Desktop/basketwise`). Commands marked **server** run over SSH on the instance.

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
9. [Phase 5 — Code and settings on the server](#phase-5--code-and-settings-on-the-server)
10. [Phase 6 — Start the API and the website](#phase-6--start-the-api-and-the-website)
11. [Phase 7 — Load the snapshot](#phase-7--load-the-snapshot)
12. [Phase 8 — Budget alert and uptime check](#phase-8--budget-alert-and-uptime-check)
13. [Final checklist](#final-checklist)
14. [Day-2 operations](#day-2-operations)
15. [Troubleshooting](#troubleshooting)
16. [Security notes](#security-notes)
17. [Tearing it down](#tearing-it-down)
18. [Reference: files in deploy/](#reference-files-in-deploy)

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
                         │  daily.sh --no-crawl   (run by hand, once)
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
- [ ] The repo pushed to **GitHub** (Phase 1)
- [ ] **`scraper/aryan_data/`** on your Mac. It's gitignored, so this folder is the **only copy** of the snapshot. Back it up (an external drive or cloud storage) before you start.
- [ ] On your Mac: `ssh`, `rsync`, `git`, `dig` (all built into macOS)

Placeholders:

| Placeholder | Meaning | Example |
|---|---|---|
| `<EIP>` | the instance's Elastic IP | `3.25.10.7` |

---

## Phase 1 — Push the code to GitHub

**Mac:**

```bash
cd ~/Desktop/basketwise
git status --short | head        # check nothing unexpected is listed
git add .
git commit -m "Snapshot-mode deploy"
git push origin main
```

`.gitignore` keeps out `node_modules`, the crawl databases, the `hyperscrape` binary, cookies and HAR captures. A dry run before this guide was written staged about 430 files and nothing over 5 MB. If `git add` picks up anything from `scraper/data/` or `scraper/aryan_data/`, stop and fix `.gitignore` first: GitHub rejects files over 100 MB.

---

## Phase 2 — Launch the EC2 instance

All of this happens in the [AWS console](https://console.aws.amazon.com/ec2/).

### 2.1 Pick the region

Top-right region selector → **Asia Pacific (Sydney) ap-southeast-2**.

### 2.2 Launch

**EC2 → Instances → Launch instances**, then fill in:

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

Click **Launch instance**.

> Your home IP may change. If SSH later times out, edit `basketwise-sg` and set the SSH rule back to *My IP*.

### 2.3 Attach an Elastic IP

1. **EC2 → Network & Security → Elastic IPs → Allocate Elastic IP address** → **Allocate**.
2. Select the new IP → **Actions → Associate Elastic IP address** → choose the `basketwise` instance → **Associate**.
3. Note the address. This is `<EIP>`.

### 2.4 SSH key on your Mac

**Mac:**

```bash
mv ~/Downloads/basketwise.pem ~/.ssh/basketwise.pem
chmod 400 ~/.ssh/basketwise.pem

cat >> ~/.ssh/config <<'EOF'

Host basketwise
  HostName <EIP>
  User ubuntu
  IdentityFile ~/.ssh/basketwise.pem
  ServerAliveInterval 60
EOF
```

Open `~/.ssh/config` and replace `<EIP>` with the real address, then:

```bash
ssh basketwise 'grep PRETTY_NAME /etc/os-release && free -h'     # Ubuntu 26.04 LTS
```

Type `yes` at the fingerprint prompt the first time.

---

## Phase 3 — Point the subdomain at it

Caddy needs `basketwise.aryannaveen.com` to point at `<EIP>` before it can get an HTTPS certificate. `aryannaveen.com` is hosted in **Route 53**, and the bare domain already points at another server, so add only the subdomain. Leave the existing records alone.

1. AWS console → **Route 53 → Hosted zones → `aryannaveen.com` → Create record**.
2. Fill in:

   | Field | Value |
   |---|---|
   | Record name | `basketwise` |
   | Record type | **A** |
   | Value | `<EIP>` |
   | TTL | `300` |
   | Routing policy | Simple routing |

   Leave **Alias** off. An Elastic IP isn't an alias target.
3. **Create records**, then check from your **Mac** until it prints `<EIP>` (usually under a minute):

   ```bash
   dig +short basketwise.aryannaveen.com
   ```

The domain has no CAA records, so Let's Encrypt is allowed to issue for it. If you ever add CAA records, include `0 issue "letsencrypt.org"`.

---

## Phase 4 — Prepare the server

**Server** (`ssh basketwise`).

> Ubuntu 26.04 uses `sudo-rs` and the Rust `uutils` coreutils by default. Every command in this guide works with them, so nothing needs switching.

### 4.1 Updates, timezone, basic tools

```bash
sudo apt update && sudo DEBIAN_FRONTEND=noninteractive apt -y upgrade
sudo timedatectl set-timezone Australia/Sydney
sudo apt install -y git curl rsync sqlite3 jq tmux unattended-upgrades
sudo dpkg-reconfigure -f noninteractive unattended-upgrades   # automatic security patches
```

If the upgrade needs a restart, reboot now, wait a minute, and reconnect:

```bash
[ -f /var/run/reboot-required ] && sudo reboot
```

### 4.2 Swap (4 GiB)

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swap.conf
sudo sysctl --system >/dev/null
free -h        # Swap: 4.0Gi
```

### 4.3 Docker

Install from Docker's own apt repository, which lists Ubuntu 26.04 (Resolute) as supported. The `Suites:` line reads the release name from the system, so it picks up `resolute` on its own.

```bash
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit
```

Reconnect so the `docker` group takes effect:

```bash
ssh basketwise
docker run --rm hello-world     # prints "Hello from Docker!"
docker compose version          # v2.x or later
```

Source: [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/), [Linux post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/).

### 4.4 uv (Python tooling for the build)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv --version
```

The installer puts `uv` in `~/.local/bin`. New SSH sessions pick it up automatically, and `daily.sh` adds that folder to its own `PATH`.

---

## Phase 5 — Code and settings on the server

### 5.1 Clone the repo

The repo must end up at **`/home/ubuntu/basketwise`**.

**If the repo is public:**

```bash
cd ~ && git clone https://github.com/Aryannaveenn/basketwise.git
```

**If it's private:** create a read-only deploy key.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N '' -C basketwise-ec2
cat ~/.ssh/github_deploy.pub
```

In GitHub, go to the repo → **Settings → Deploy keys → Add deploy key**, paste the key and leave *Allow write access* **off**. Then:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
cd ~ && git clone git@github.com:Aryannaveenn/basketwise.git
```

### 5.2 Server folders

```bash
sudo mkdir -p /srv/basketwise/{data,www,state}
sudo chown -R ubuntu:ubuntu /srv/basketwise
mkdir -p ~/basketwise/scraper/data
```

| Path | Contents |
|---|---|
| `~/basketwise/scraper/data/` | the snapshot's `hyper_*.db` files (Phase 7) and the build's working files |
| `/srv/basketwise/data/` | the published `basketwise.db` + `warm/`, mounted read-only into the API |
| `/srv/basketwise/www/` | the built React app |
| `/srv/basketwise/state/` | `last_build.json`, used by validation |
| `/srv/basketwise/basketwise.env` | settings |

### 5.3 Settings file

```bash
cp ~/basketwise/deploy/basketwise.env.example /srv/basketwise/basketwise.env
chmod 600 /srv/basketwise/basketwise.env
cat /srv/basketwise/basketwise.env
```

The example is already set up for snapshot mode. Check it reads:

```ini
DOMAIN=basketwise.aryannaveen.com
SUPABASE_URL=
SUPABASE_SECRET_KEY=
HEALTHCHECK_URL=
BASKETWISE_CORS=*
```

**Keep the Supabase and healthcheck lines blank.** A placeholder value there makes every product page wait on a Supabase timeout. If you edit the file: no quotes, no spaces around `=`, and no comments on the same line as a value. Docker Compose and the scripts both read it.

### 5.4 Python packages for the build

```bash
cd ~/basketwise/scraper
UV_PROJECT_ENVIRONMENT=.venv-hs uv sync
```

This installs the locked packages into `scraper/.venv-hs`, the environment `daily.sh` uses. Every locked package ships a ready-made Linux build, so no compiler is needed, whichever Python 3 version uv picks.

---

## Phase 6 — Start the API and the website

### 6.1 Start the containers

```bash
~/basketwise/deploy/scripts/up.sh
```

This builds the API image from `backend/Dockerfile` and starts two containers:

- **`basketwise-api`**: FastAPI on the internal Docker network only (no host port). Until Phase 7 publishes a database it restarts in a loop with `database not found`. That's expected.
- **`basketwise-caddy`**: listens on 80/443 and gets a Let's Encrypt certificate for `basketwise.aryannaveen.com` on first start.

Check Caddy got its certificate:

```bash
~/basketwise/deploy/scripts/up.sh logs caddy | grep -iE 'certificate obtained|error'
```

### 6.2 Build and publish the frontend

```bash
~/basketwise/deploy/scripts/frontend.sh
```

This runs `pnpm install && pnpm build` inside a temporary `node:24-slim` container, with `VITE_API_BASE=/api` built into the bundle, then copies `frontend/dist/` to `/srv/basketwise/www/`. The first run takes a few minutes and leans on swap. No Node install is needed on the server.

Open `https://basketwise.aryannaveen.com`. The page shell loads; products appear after Phase 7.

> **Building on your Mac instead** (faster on a `t3.micro`):
> ```bash
> # Mac
> cd frontend && VITE_API_BASE=/api pnpm build
> rsync -a --delete dist/ basketwise:/srv/basketwise/www/
> ```

---

## Phase 7 — Load the snapshot

### 7.1 Copy the crawl files

**Mac** (from the repo root). This copies about 365 MB. Include the `-wal` files: `hyper_woolworths.db-wal` holds about 7 MB of products not yet merged into the main file.

```bash
rsync -avP scraper/aryan_data/hyper_*.db scraper/aryan_data/hyper_*.db-wal \
  basketwise:basketwise/scraper/data/
```

### 7.2 Build, validate and publish

**Server:**

```bash
set -a; source /srv/basketwise/basketwise.env; set +a
~/basketwise/deploy/scripts/daily.sh --no-crawl
```

It takes a few minutes on a `t3.micro`. The output ends with:

```
products=39,101 offers=45,182 comparable=5,334 retailers=['aldi', 'coles', 'harrisfarm', 'woolworths']
validation ok
...
api healthy
...
SUPABASE_URL / SUPABASE_SECRET_KEY not set; skipping
...
=== <time>  done ===
```

`validate.py` refuses to publish a build with fewer than 20,000 products or offers, fewer than 2,000 comparable products, fewer than 2 retailers, or a retailer with under half its offers available. If it fails, the site keeps serving whatever was there before (nothing, on the first run). See [Troubleshooting](#validation-fails).

### 7.3 Check it worked

```bash
curl -s https://basketwise.aryannaveen.com/api/health | jq '{data_as_of, product_count, offer_count, retailers}'
# "data_as_of": "2026-09-05T03:49:40Z", "product_count": 39101, "offer_count": 45182, 4 retailers
curl -s 'https://basketwise.aryannaveen.com/api/products?limit=2' | jq '.[].name'
```

Then open:

- `https://basketwise.aryannaveen.com/`: the site, with the "Prices as of 5 Sep 2026" strip under the header
- `https://basketwise.aryannaveen.com/api/docs`: Swagger

Keep the `hyper_*.db` files in `~/basketwise/scraper/data/`. Rebuilds after code changes need them ([Day-2](#rebuild-the-data-after-a-build-code-change)).

---

## Phase 8 — Budget alert and uptime check

### AWS budget alert

**Billing and Cost Management → Budgets → Create budget → Use a template → Zero spend budget** → your email → **Create**. You get an email the moment anything costs money.

### Uptime check (optional)

Point a free monitor (UptimeRobot, Better Stack, healthchecks.io) at `https://basketwise.aryannaveen.com/api/health`.

### Now and then

```bash
df -h /                         # keep a few GB free
free -h
docker stats --no-stream        # api should sit around 250 MB
~/basketwise/deploy/scripts/up.sh ps
```

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

**Mac:** commit and push. **Server:**

```bash
cd ~/basketwise
git fetch origin && git reset --hard origin/main
```

The server never has hand edits, so reset rather than merge. `scraper/data/` is gitignored, so the reset leaves the crawl files alone. Then run whichever applies:

| What changed | Run |
|---|---|
| `backend/` | `~/basketwise/deploy/scripts/up.sh` (rebuilds and restarts the API) |
| `frontend/` | `~/basketwise/deploy/scripts/frontend.sh` |
| `scraper/tui/`, `backend/shapes.py` or `backend/data/essentials.txt` | the rebuild below |
| `deploy/compose.yml` or `deploy/Caddyfile` | `~/basketwise/deploy/scripts/up.sh` |
| `scraper/pyproject.toml` or `uv.lock` | `cd ~/basketwise/scraper && UV_PROJECT_ENVIRONMENT=.venv-hs uv sync` |

### Rebuild the data after a build-code change

```bash
set -a; source /srv/basketwise/basketwise.env; set +a
~/basketwise/deploy/scripts/daily.sh --no-crawl
```

The same snapshot goes in, so the counts and `data_as_of` stay the same; only `build_id` and `built_at` change.

### Useful commands

```bash
~/basketwise/deploy/scripts/up.sh ps                 # container status
~/basketwise/deploy/scripts/up.sh logs -f api        # API logs
~/basketwise/deploy/scripts/up.sh restart caddy
```

### Rebuild onto a fresh instance

Do Phases 2–6 (update the Route 53 record if the Elastic IP changes), then Phase 7 again from your Mac's `scraper/aryan_data/` or your backup of it.

---

## Troubleshooting

### SSH times out

Your home IP changed. Edit `basketwise-sg` and set the SSH rule back to *My IP*.

### HTTPS doesn't work / Caddy can't get a certificate

```bash
~/basketwise/deploy/scripts/up.sh logs caddy | tail -50
```

- `dig +short basketwise.aryannaveen.com` must print `<EIP>`.
- The security group must allow **80 and 443 from anywhere**. Let's Encrypt connects from many IPs.
- `DOMAIN` in `/srv/basketwise/basketwise.env` must be exactly `basketwise.aryannaveen.com`.
- Don't delete the `caddy_data` volume. Let's Encrypt rate-limits repeated requests for the same hostname.

### The site loads but shows no products

- `curl -s https://basketwise.aryannaveen.com/api/health`. A 502 means the API isn't running: `up.sh logs api`.
- `database not found at /data/basketwise.db` means nothing has been published yet. Run Phase 7.
- DevTools → Network: requests should go to `/api/...`. If they go to `basket.taskglass.work`, the frontend was built without `VITE_API_BASE=/api`. Re-run `frontend.sh`.

### The "Prices as of" strip doesn't appear

`curl -s https://basketwise.aryannaveen.com/api/health | jq .data_as_of` returns `null`: the published `basketwise.db` was built by a version of `build_api_db.py` from before `data_as_of` existed. Pull the latest code and run the [rebuild](#rebuild-the-data-after-a-build-code-change).

### Validation fails

Read the reason in the `VALIDATION FAILED:` lines.

- `aldi: only 0/2627 offers available`: the server's code is older than the ALDI availability fix. Pull the latest code and rebuild.
- `only N products` with N far below 39,101: the crawl files didn't all copy. Re-run the rsync in 7.1 and check all four `hyper_*.db` files are in `~/basketwise/scraper/data/`, plus `hyper_woolworths.db-wal`.
- `products dropped ... (>20%)` after an intentional change: delete `/srv/basketwise/state/last_build.json` once to reset the baseline.

### The build was killed / out of memory

```bash
sudo dmesg -T | grep -i -E 'killed process|out of memory' | tail
swapon --show
```

Check swap is on. If it still fails, move up to `t3.small`: stop the instance → **Actions → Instance settings → Change instance type** → start. The Elastic IP stays attached.

### A shell command in the publish step fails oddly

Ubuntu 26.04's default `uutils` coreutils are meant to be drop-in replacements, and `daily.sh` only uses basic `cp`, `mv`, `rm`, `chmod` and `seq` options. If one of them misbehaves, Ubuntu documents how to switch back to GNU coreutils (the `coreutils-from-gnu` package). Note the exact error first.

### Disk filling up

```bash
du -sh ~/basketwise/scraper/data/* | sort -h | tail
docker system prune -f        # old images and build cache
```

---

## Security notes

- **No secrets in git**, and in snapshot mode none on the server either. `basketwise.env` still lives only in `/srv/basketwise`, with mode `600`.
- **SSH is limited to your IP**, with key-only login (Ubuntu's AMI default).
- **The API container** runs as a non-root user, with a read-only filesystem, all Linux capabilities dropped, a 512 MB memory cap, and the database mounted read-only.
- **Only Caddy has published ports** (80/443). The API is reachable only through it.
- **Automatic security updates** are on (`unattended-upgrades`). Reboot now and then for kernel updates: `sudo reboot`. The containers come back on their own (`restart: unless-stopped`).

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
