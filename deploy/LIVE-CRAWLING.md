# Live crawling: upgrading BasketWise to daily prices

The site is deployed in **snapshot mode** ([`DEPLOY.md`](../DEPLOY.md)): one fixed crawl, no scraping. This guide is the full setup for prices that update every day. It was written as a from-scratch deploy, so on an existing snapshot server, skip what's already done (EC2, hostname, Docker, uv, the env file, `up.sh`, `frontend.sh`) and do the parts snapshot mode skipped: Phase 1 (Supabase), Go in 4.4, the rest of Phase 6, Phase 9 and Phase 10. Fill in the Supabase and healthcheck values in the env file, then run `up.sh` so the API picks them up.

It takes the project to a live, self-updating site:

- the **React app** and the **FastAPI backend** on one AWS EC2 free-tier instance, behind HTTPS
- **Supabase** (free plan) as the system of record: every store's catalogue, every price change, and a backup of the built API database
- a **daily scrape** of Coles, Woolworths, ALDI and Harris Farm that rebuilds the data, publishes it, and syncs it to Supabase

Expect about 1.5–2 hours for the first deploy, most of it waiting on the first crawl.

Every file this guide uses is in [`deploy/`](.). Nothing outside it changes.

---

## Contents

1. [Architecture](#1-architecture)
2. [Costs and free-tier limits](#2-costs-and-free-tier-limits)
3. [Instance spec](#3-instance-spec)
4. [Before you start](#4-before-you-start)
5. [Phase 1 — Supabase](#phase-1--supabase)
6. [Phase 2 — Launch the EC2 instance](#phase-2--launch-the-ec2-instance)
7. [Phase 3 — Hostname](#phase-3--hostname)
8. [Phase 4 — Prepare the server](#phase-4--prepare-the-server)
9. [Phase 5 — Get the code and secrets onto the server](#phase-5--get-the-code-and-secrets-onto-the-server)
10. [Phase 6 — Build the scraper toolchain](#phase-6--build-the-scraper-toolchain)
11. [Phase 7 — Start the API and the website](#phase-7--start-the-api-and-the-website)
12. [Phase 8 — Load the first data](#phase-8--load-the-first-data)
13. [Phase 9 — Schedule the daily scrape](#phase-9--schedule-the-daily-scrape)
14. [Phase 10 — Monitoring and alerts](#phase-10--monitoring-and-alerts)
15. [Final checklist](#final-checklist)
16. [Day-2 operations](#day-2-operations)
17. [Troubleshooting](#troubleshooting)
18. [Security notes](#security-notes)
19. [Tearing it down](#tearing-it-down)
20. [Reference: files in deploy/](#reference-files-in-deploy)

---

## 1. Architecture

```
                        https://<your-domain>
                                 │
                 ┌───────────────▼────────────────┐   EC2 t3.micro / t3.small (Sydney)
                 │  Caddy  (ports 80/443, HTTPS)  │   Ubuntu 26.04 + Docker
                 │   /        → React build       │
                 │   /api/*   → FastAPI container │
                 └───────┬────────────────────────┘
                         │ reads (read-only)
            /srv/basketwise/data/basketwise.db + warm/      ◄── published nightly
                         ▲
                         │  daily.sh (systemd timer, 03:30 Sydney)
                         │   1. crawl 4 stores  → scraper/data/hyper_<store>.db
                         │   2. build_master    → master.db
                         │   3. build_api_db    → basketwise.db + warm/
                         │   4. validate        → refuse a bad build
                         │   5. publish + restart API
                         │   6. sync            ──────────────┐
                         │                                    ▼
   GET /products/{id}/price-trend ───────────────► Supabase (free plan, Sydney)
   (API reads multi-day history)                    • stores / store_products
                                                    • price_history (changes only)
                                                    • Storage: artifacts/latest.tar.gz
```

### Why the API still serves SQLite

The backend runs queries against a 52 MB read-only SQLite file, including FTS5 full-text search, with everything pre-computed at build time. That keeps it at about 250 MB of RAM and fast on a free-tier box. Supabase holds **all** the data, but as the durable store, not the request path:

| Data | Where it lives | Who reads it |
|---|---|---|
| Raw grocery catalogue per store (names, brands, sizes, images, barcodes, URLs) | Supabase `store_products` | you, in the Supabase dashboard or SQL |
| Every price change since day one | Supabase `price_history` | API → `/products/{id}/price-trend` |
| Built API database (canonical products, offers, categories) | Supabase Storage `artifacts/latest.tar.gz`, plus a local copy | API (local copy); restore uses Storage |
| Crawl working files (`hyper_*.db`, `master.db`) | EC2 disk only | the nightly build |

Pointing every API route at Postgres would mean rewriting `backend/db.py` and each route, including the FTS5 search. That's a separate project and isn't needed to deploy.

### What runs where

| Component | Runs on | How |
|---|---|---|
| React app | EC2 | static files in `/srv/basketwise/www`, served by Caddy |
| FastAPI | EC2 | Docker container `basketwise-api`, built from `backend/Dockerfile` |
| HTTPS | EC2 | Caddy container, automatic Let's Encrypt certificates |
| Scraper and build | EC2 | `deploy/scripts/daily.sh` under a systemd timer |
| Database of record | Supabase | Postgres + Storage |

---

## 2. Costs and free-tier limits

Free-tier terms change. **Check the linked pages on the day you launch.** This was correct as of November 2026.

### AWS

AWS free tier depends on when your account was created:

| Account created | What's free |
|---|---|
| **Before 15 July 2025** | 750 hours/month of `t2.micro` or `t3.micro` for 12 months from sign-up, 30 GB of EBS storage, and 750 hours/month of public IPv4 |
| **On or after 15 July 2025** | The "Free plan": sign-up credits that pay for usage for a limited period. Eligible types include `t3.micro`, `t3.small`, `t4g.micro`, `t4g.small`. Storage and the public IP are paid from the credits. |

Costs to know about:

- **Public IPv4:** US$0.005/hour, about US$3.60/month, unless your free tier covers it.
- **An Elastic IP that isn't attached to a running instance is billed.** Release it if you stop using it.
- **T3 "Unlimited" CPU mode** (on by default for T3) can bill for CPU bursts. This guide switches it to **Standard**, so the box slows down instead of charging you.

Sources: [AWS Free Tier](https://aws.amazon.com/free/), [EC2 free tier usage](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-free-tier-usage.html), [Legacy free tier FAQ](https://aws.amazon.com/free/legacy/free-tier-faqs).

### Supabase (Free plan)

| Limit | Value | How this deploy stays under it |
|---|---|---|
| Database size | 0.5 GB per project | Marketplace listings aren't uploaded; price history stores only *changes*; products re-upload only when they change |
| File storage | 1 GB | one artifact file, overwritten daily (~20 MB compressed) |
| Max upload size | 50 MB per file | the sync script checks and skips if over |
| Egress | 5 GB/month | the API fetches small history slices, cached for 5 minutes |
| Inactivity pause | after 7 days with no activity | the daily sync writes every day, so it never goes idle |

Rough sizing: about 65k grocery products (≈ 60 MB with indexes), plus about 65k initial price rows, plus a few thousand changes a day (≈ 0.5 MB/day). That's well under 0.5 GB for about a year. [Phase 10](#phase-10--monitoring-and-alerts) shows how to check.

Sources: [Supabase pricing](https://supabase.com/pricing), [Free plan database size](https://supabase.com/changelog/33121-relaxing-database-size-limit-on-free-plan-0-5-gb-database-size-per-project).

---

## 3. Instance spec

| Setting | Value | Why |
|---|---|---|
| Region | **Asia Pacific (Sydney) `ap-southeast-2`** | the retailers are Australian; closest network path |
| Instance type | **`t3.small`** (2 vCPU, 2 GiB) if your account offers it free, otherwise **`t3.micro`** (2 vCPU, 1 GiB) | the nightly build and headless Chromium need memory; swap covers `t3.micro` |
| Architecture | **x86_64** | headless Chromium (Playwright) and the cgo SQLite build are least fussy on x86 |
| AMI | **Ubuntu Server 26.04 LTS (HVM), SSD, 64-bit (x86)** | |
| Root volume | **30 GiB gp3** | crawl files and `master.db` reach several GB |
| Swap | **4 GiB** swap file (set up in Phase 4) | headroom for the build and Chromium |
| CPU credits | **Standard** (not Unlimited) | no surprise CPU charges |
| Public IP | **Elastic IP** | stable address for DNS and HTTPS |
| Inbound ports | 22 from *your IP only*; 80 and 443 from anywhere | |

Memory budget at runtime: API ≤ 512 MB (hard cap), Caddy ≤ 128 MB, the OS ≈ 150 MB. The nightly job runs on top of that, which is what the swap is for.

---

## 4. Before you start

You need:

- [ ] An **AWS account**, with a [budget alert](#set-an-aws-budget-alert) planned
- [ ] A **Supabase account** (GitHub login works)
- [ ] The repo on **GitHub** under your account (fork or push a copy). If it's private, you'll need a deploy key (Phase 5).
- [ ] On your Mac: `ssh`, `rsync`, `git`
- [ ] Access to the **`aryannaveen.com` hosted zone in Route 53** (for the `basketwise` subdomain, Phase 3)
- [ ] Optional: a [healthchecks.io](https://healthchecks.io) account for failure emails

Throughout this guide:

| Placeholder | Meaning | Example |
|---|---|---|
| `<EIP>` | the instance's Elastic IP | `3.25.10.7` |
| `<DOMAIN>` | hostname the site is served on | `basketwise.aryannaveen.com` |
| `<REPO_URL>` | your GitHub clone URL | `git@github.com:you/basketwise.git` |

Commands marked **Mac** run on your laptop. Commands marked **server** run over SSH on the instance.

---

## Phase 1 — Supabase

### 1.1 Create the project

1. Go to <https://supabase.com/dashboard> → **New project**.
2. **Name:** `basketwise`. **Region:** **Sydney (ap-southeast-2)**.
3. **Database password:** generate one and save it in your password manager. This guide doesn't use it, but you'll need it for direct `psql` access.
4. Plan: **Free**. Click **Create new project** and wait about 2 minutes.

> If you were using a teammate's Supabase project before, create your own anyway. The schema below is self-contained.

### 1.2 Create the schema

1. Left sidebar → **SQL Editor** → **New query**.
2. Paste the whole contents of [`deploy/supabase/schema.sql`](../deploy/supabase/schema.sql) and click **Run**.
3. You should see `Success. No rows returned`.

This creates:

- `stores`: the 4 retailers, already inserted
- `store_products`: one row per retailer product, unique on `(store_id, external_product_id)`
- `price_history`: one row per price change, unique on `(store_product_id, captured_at)`, so re-running a sync never duplicates rows
- Row Level Security on all three tables with **no policies**, so the public key can't read or write anything
- a private Storage bucket called `artifacts`

Check it: **Table Editor** shows the three tables, `stores` has 4 rows, and **Storage** shows the `artifacts` bucket marked *Private*.

### 1.3 Copy the URL and the secret key

1. **Project Settings** (gear icon) → **API Keys** (or **Data API** for the URL).
2. Copy the **Project URL**: `https://<ref>.supabase.co`.
3. Under **Secret keys**, copy a secret key (`sb_secret_…`). If your project only shows legacy keys, copy the **`service_role`** key instead.

> The secret key bypasses Row Level Security. It goes **only** into `/srv/basketwise/basketwise.env` on the server. Never commit it, never put it in the frontend, never paste it into chat.

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
| **Application and OS Images** | *Ubuntu* → **Ubuntu Server 26.04 LTS (HVM), SSD Volume Type**, architecture **64-bit (x86)**. Check it says *Free tier eligible*. |
| **Instance type** | `t3.small` if it shows *Free tier eligible*, otherwise `t3.micro` |
| **Key pair** | **Create new key pair** → name `basketwise`, type **ED25519**, format **.pem** → it downloads `basketwise.pem` |
| **Network settings** → **Edit** | VPC: default. Auto-assign public IP: **Enable**. Firewall: **Create security group**, name `basketwise-sg`. |
| Inbound rule 1 | Type **SSH**, port 22, Source **My IP** |
| Inbound rule 2 | **Add security group rule** → Type **HTTP**, port 80, Source **Anywhere** (`0.0.0.0/0`) |
| Inbound rule 3 | **Add security group rule** → Type **HTTPS**, port 443, Source **Anywhere** (`0.0.0.0/0`) |
| **Configure storage** | **30** GiB, **gp3** |
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

ssh basketwise 'uname -a && free -h'
```

Replace `<EIP>` in `~/.ssh/config` with the real address. Type `yes` at the fingerprint prompt the first time.

---

## Phase 3 — Hostname

Caddy needs a hostname that points at `<EIP>` before it can get an HTTPS certificate.

This deploy uses **`<DOMAIN>` = `basketwise.aryannaveen.com`**. `aryannaveen.com` is hosted in **AWS Route 53**, and the bare domain already points at another server, so only the `basketwise` subdomain is added. Leave the existing records alone.

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
3. **Create records**, then check from your Mac until it prints `<EIP>` (usually under a minute):

   ```bash
   dig +short basketwise.aryannaveen.com
   ```

The domain has no CAA records, so Let's Encrypt is allowed to issue for it. If you ever add CAA records, include `0 issue "letsencrypt.org"`.

**Fallback: no domain.** Use [sslip.io](https://sslip.io), which turns an IP into a hostname. For `<EIP>` `3.25.10.7`, `<DOMAIN>` is `3-25-10-7.sslip.io`. Nothing to configure: `dig +short 3-25-10-7.sslip.io` prints `3.25.10.7`.

---

## Phase 4 — Prepare the server

**Server** (`ssh basketwise`):

### 4.1 Update, timezone, basics

```bash
sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get -y upgrade
sudo timedatectl set-timezone Australia/Sydney
sudo apt-get install -y build-essential git curl rsync sqlite3 jq tmux unattended-upgrades
sudo dpkg-reconfigure -f noninteractive unattended-upgrades   # automatic security patches
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

```bash
# Install from Docker's apt repository, exactly as in DEPLOY.md Phase 4.3
# (get.docker.com is not recommended for production). Then:
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit
```

Reconnect so the `docker` group takes effect:

```bash
ssh basketwise
docker run --rm hello-world     # prints "Hello from Docker!"
docker compose version          # v2.x
```

### 4.4 Go (for the crawler)

```bash
GO_VERSION=$(curl -fsSL 'https://go.dev/VERSION?m=text' | head -1)   # e.g. go1.25.x
curl -fsSL "https://go.dev/dl/${GO_VERSION}.linux-amd64.tar.gz" | sudo tar -C /usr/local -xz
echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc
export PATH=$PATH:/usr/local/go/bin
go version
```

The crawler needs Go 1.24 or newer (`scraper/go.mod`), so the latest stable release is fine.

### 4.5 uv (Python tooling)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv --version
```

---

## Phase 5 — Get the code and secrets onto the server

### 5.1 Clone the repo

**If the repo is public:**

```bash
cd ~
git clone https://github.com/<you>/basketwise.git
```

**If it's private:** create a read-only deploy key.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N '' -C basketwise-ec2
cat ~/.ssh/github_deploy.pub
```

In GitHub, go to your repo → **Settings → Deploy keys → Add deploy key**, paste the key and leave *Allow write access* **off**. Then:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
cd ~ && git clone <REPO_URL> basketwise
```

The repo must end up at **`/home/ubuntu/basketwise`**; the systemd unit expects that path.

### 5.2 Server folders

```bash
sudo mkdir -p /srv/basketwise/{data,www,state}
sudo chown -R ubuntu:ubuntu /srv/basketwise
```

| Path | Contents |
|---|---|
| `/srv/basketwise/data/` | the published `basketwise.db` + `warm/` (mounted read-only into the API) |
| `/srv/basketwise/www/` | the built React app |
| `/srv/basketwise/state/` | `last_build.json`, used by validation to catch big drops |
| `/srv/basketwise/basketwise.env` | secrets and settings |

### 5.3 Secrets file

```bash
cp ~/basketwise/deploy/basketwise.env.example /srv/basketwise/basketwise.env
chmod 600 /srv/basketwise/basketwise.env
nano /srv/basketwise/basketwise.env
```

Set:

```ini
DOMAIN=basketwise.aryannaveen.com
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_SECRET_KEY=sb_secret_...
HEALTHCHECK_URL=
BASKETWISE_CORS=*
```

`HEALTHCHECK_URL` gets filled in during Phase 10, or stays blank. For live crawling, fill in `SUPABASE_URL` and `SUPABASE_SECRET_KEY` (Phase 1).

No quotes, no spaces around `=`, and no comments on the same line as a value. systemd reads a trailing `# ...` as part of the value. systemd and Docker both read this file.

---

## Phase 6 — Build the scraper toolchain

**Server:**

```bash
cd ~/basketwise/scraper

# 1. Go crawler (cgo + SQLite, so it needs build-essential from Phase 4)
go build -o hyperscrape ./cmd/hyperscrape
./hyperscrape 2>&1 | head -1          # prints usage → it built

# 2. Python tooling in its own venv (same name the scripts use)
UV_PROJECT_ENVIRONMENT=.venv-hs uv sync

# 3. Headless Chromium for the Woolworths cookie harvest (installs system libs; asks for sudo)
UV_PROJECT_ENVIRONMENT=.venv-hs uv run playwright install --with-deps chromium
```

> **Ubuntu 26.04:** Playwright 1.59 refused to install Chromium or its system packages on the 26.04 beta ("Cannot install dependencies for ubuntu26.04-x64"). Check that the locked version (`uv.lock`, 1.62.0 at the time of writing) supports 26.04 before relying on the Woolworths crawl. If it doesn't, upgrade Playwright, or crawl Woolworths from your Mac (see Troubleshooting → a retailer blocks the EC2 IP, option 3). The other three stores don't need a browser.

### 6.1 Smoke-test that the retailers answer from AWS

Do this **before** relying on a nightly run. Retailer anti-bot systems sometimes treat cloud IPs differently from home IPs.

```bash
cd ~/basketwise/scraper
./hyperscrape -store aldi       -phase list -max-pages 1 -db /tmp/t_aldi.db       >/dev/null && echo "aldi ok"
./hyperscrape -store harrisfarm -phase list -max-pages 1 -db /tmp/t_hf.db         >/dev/null && echo "harrisfarm ok"
./hyperscrape -store coles      -phase list -max-pages 1 -db /tmp/t_coles.db      >/dev/null && echo "coles ok"
UV_PROJECT_ENVIRONMENT=.venv-hs uv run tui/harvest_cookies.py --store woolworths --out /tmp/ck.json
./hyperscrape -store woolworths -phase list -max-pages 1 -cookies /tmp/ck.json -db /tmp/t_ww.db >/dev/null && echo "woolworths ok"
for s in aldi hf coles ww; do printf '%-6s ' $s; sqlite3 /tmp/t_$s.db 'select count(*) from products'; done
rm -f /tmp/t_*.db* /tmp/ck.json
```

Each store should show a non-zero count. A store that exits with code 2 or shows `0` is being blocked from this IP; see [Troubleshooting → a retailer blocks the EC2 IP](#a-retailer-blocks-the-ec2-ip). The pipeline still runs without it.

---

## Phase 7 — Start the API and the website

### 7.1 Start the containers

```bash
chmod +x ~/basketwise/deploy/scripts/*.sh
~/basketwise/deploy/scripts/up.sh
```

This builds the API image from `backend/Dockerfile` and starts two containers:

- **`basketwise-api`**: FastAPI on the internal Docker network only (no host port). Until Phase 8 publishes a database, it restarts in a loop with `database not found`. That's expected.
- **`basketwise-caddy`**: listens on 80/443 and gets a Let's Encrypt certificate for `<DOMAIN>` on first start.

Check Caddy got its certificate:

```bash
~/basketwise/deploy/scripts/up.sh logs caddy | grep -iE 'certificate obtained|error'
```

### 7.2 Build and publish the frontend

```bash
~/basketwise/deploy/scripts/frontend.sh
```

This runs `pnpm install && pnpm build` inside a temporary `node:24-slim` container, with `VITE_API_BASE=/api` baked into the bundle, then copies `frontend/dist/` to `/srv/basketwise/www/`. The first run takes a few minutes. On a `t3.micro` it leans on swap; that's fine.

Open `https://<DOMAIN>` in a browser. The page shell loads; product data appears after Phase 8.

> **Building on your Mac instead** (faster on a `t3.micro`):
> ```bash
> # Mac
> cd frontend && VITE_API_BASE=/api pnpm build
> rsync -a --delete dist/ basketwise:/srv/basketwise/www/
> ```

---

## Phase 8 — Load the first data

The API needs one good `basketwise.db` before it can serve anything. Pick one option.

### Option A: seed from your Mac (faster, keeps your price history)

If your Mac already has crawl databases in `scraper/data/`:

```bash
# Mac
cd ~/Desktop/basketwise/scraper/data
rsync -avP hyper_*.db basketwise:basketwise/scraper/data/
```

Then build and publish from them on the server, without crawling:

```bash
# server
set -a; source /srv/basketwise/basketwise.env; set +a
~/basketwise/deploy/scripts/daily.sh --no-crawl
```

### Option B: crawl from scratch on the server

```bash
# server
tmux new -s firstrun                       # survives an SSH disconnect
set -a; source /srv/basketwise/basketwise.env; set +a
~/basketwise/deploy/scripts/daily.sh
# detach: Ctrl-b then d    reattach: tmux attach -t firstrun
```

Expected first-run timings:

| Step | Duration |
|---|---|
| ALDI (full) | ~1 min |
| Harris Farm (full) | ~30 s |
| Coles (listing; full detail only on Sundays) | tens of minutes; the rate controller starts slow on a first run |
| Woolworths (cookie harvest + listing) | tens of minutes; it pages ~500k marketplace listings even though only ~25k are groceries |
| `build_master` + `build_api_db` | a few minutes; longer on `t3.micro` (swap) |

Later runs start at 25% of the last run's peak rate (`-start-rate auto`), so they're faster.

### If validation fails on the first run

`validate.py` refuses to publish a build with fewer than 20,000 products, fewer than 2,000 products comparable across retailers, or fewer than 2 retailers. If a store was blocked on the first run, you can lower the bar **once** to get something online:

```bash
BW_MIN_PRODUCTS=5000 BW_MIN_OFFERS=5000 BW_MIN_COMPARABLE=200 \
  ~/basketwise/deploy/scripts/daily.sh --no-crawl
```

### Check it worked

```bash
curl -s https://<DOMAIN>/api/health | jq
# {"status":"ok","build_id":"2026...","product_count":4xxxx,"offer_count":5xxxx,"retailers":[...]}
curl -s 'https://<DOMAIN>/api/products?limit=2' | jq '.[].name'
```

Then open:

- `https://<DOMAIN>/`: the site, now with real products
- `https://<DOMAIN>/api/docs`: Swagger

In Supabase: **Table Editor → `store_products`** has tens of thousands of rows, **`price_history`** has one row per product, and **Storage → artifacts** contains `latest.tar.gz`.

---

## Phase 9 — Schedule the daily scrape

```bash
sudo cp ~/basketwise/deploy/systemd/basketwise-daily.service /etc/systemd/system/
sudo cp ~/basketwise/deploy/systemd/basketwise-daily.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now basketwise-daily.timer
systemctl list-timers basketwise-daily.timer
```

`list-timers` should show the next run at about 03:30 Sydney time. It's randomised by up to 15 minutes, and a run missed while the box was off happens at next boot (`Persistent=true`).

Trigger a run now to test the systemd path end to end:

```bash
sudo systemctl start basketwise-daily.service --no-block
journalctl -u basketwise-daily -f          # Ctrl-C to stop following
```

### What the nightly job does

| Store | Daily | Sunday |
|---|---|---|
| ALDI | full | full |
| Harris Farm | full | full |
| Coles | listing (prices, names, sizes, images) | **full detail** (adds barcode, nutrition, ingredients) |
| Woolworths | cookie harvest + listing (prices and barcodes come in the listing) | same |

Woolworths' detail phase also fetches every marketplace listing, about 500k, which takes many hours at its polite 18 req/s cap. That's why it's listing-only. To force one full detail run, for example on a weekend:

```bash
sudo systemctl stop basketwise-daily.timer
set -a; source /srv/basketwise/basketwise.env; set +a
WOOLWORTHS_DETAIL=1 ~/basketwise/deploy/scripts/daily.sh
sudo systemctl start basketwise-daily.timer
```

Safety rules built into `daily.sh`:

- A store whose crawl fails **keeps its previous data**; the run carries on.
- A build that fails validation is **not published**; the site keeps serving yesterday's build.
- The new database and `warm/` are copied in next to the live ones, then renamed into place, so the API never sees a half-copied file.
- Supabase sync runs **after** publishing, so a Supabase outage can't take the site down.

---

## Phase 10 — Monitoring and alerts

### Failure emails (healthchecks.io, free)

1. Sign up at <https://healthchecks.io> → **Add Check**. Name: `basketwise-daily`. Schedule: **Cron**, `30 3 * * *`, timezone `Australia/Sydney`, grace **6 hours**.
2. Copy the ping URL (`https://hc-ping.com/<uuid>`) into `HEALTHCHECK_URL=` in `/srv/basketwise/basketwise.env`.

`daily.sh` pings `/start` at the beginning, the plain URL on success, and `/fail` on any error. You get an email if a run fails or doesn't happen at all.

### Set an AWS budget alert

**Billing and Cost Management → Budgets → Create budget → Use a template → Zero spend budget** → your email → **Create**. You get an email the moment anything costs money.

### Uptime check (optional)

Point a free monitor (healthchecks.io, UptimeRobot, Better Stack) at `https://<DOMAIN>/api/health`.

### Things to check now and then

```bash
df -h /                                   # keep at least 5 GB free
free -h
docker stats --no-stream                  # api should sit around 250 MB
journalctl -u basketwise-daily --since today | tail -40
curl -s https://<DOMAIN>/api/health | jq .build_id      # should change daily
```

Supabase database size (SQL Editor):

```sql
select pg_size_pretty(pg_database_size(current_database()));
select relname, pg_size_pretty(pg_total_relation_size(relid))
from pg_catalog.pg_statio_user_tables order by pg_total_relation_size(relid) desc;
```

If it gets close to 0.5 GB, see [Supabase is nearly full](#supabase-is-nearly-full).

---

## Final checklist

- [ ] `https://<DOMAIN>` loads over HTTPS, and Browse shows real products
- [ ] Clicking a product shows a price-trend chart (one point at first, a line after a few days)
- [ ] `https://<DOMAIN>/api/docs` loads Swagger
- [ ] `systemctl list-timers` shows `basketwise-daily.timer`
- [ ] The next morning, `/api/health` shows a new `build_id`
- [ ] Supabase `price_history` row count grows over the next few days
- [ ] healthchecks.io shows a green check after the first scheduled run
- [ ] An AWS zero-spend budget is set
- [ ] The secret key appears **only** in `/srv/basketwise/basketwise.env`

---

## Day-2 operations

### Deploy new code

The server never has hand edits, so reset to `origin/main` rather than merge. Everything the nightly job writes (`scraper/data/`) is gitignored, so the reset leaves the crawl data alone:

```bash
cd ~/basketwise
git fetch origin && git reset --hard origin/main

~/basketwise/deploy/scripts/up.sh             # backend changed → rebuild + restart API
~/basketwise/deploy/scripts/frontend.sh       # frontend changed → rebuild site
(cd scraper && go build -o hyperscrape ./cmd/hyperscrape)                # crawler changed
(cd scraper && UV_PROJECT_ENVIRONMENT=.venv-hs uv sync)                  # scraper Python deps changed
sudo cp deploy/systemd/* /etc/systemd/system/ && sudo systemctl daemon-reload   # unit files changed
```

### Useful commands

```bash
~/basketwise/deploy/scripts/up.sh ps                 # container status
~/basketwise/deploy/scripts/up.sh logs -f api        # API logs
~/basketwise/deploy/scripts/up.sh restart caddy
journalctl -u basketwise-daily -n 200 --no-pager     # last pipeline run
systemctl status basketwise-daily.timer
```

### Rebuild or publish without crawling

```bash
set -a; source /srv/basketwise/basketwise.env; set +a
~/basketwise/deploy/scripts/daily.sh --no-crawl
```

### Restore onto a fresh instance

Supabase Storage holds the last published build, so a new box can serve data before its first crawl. Do Phases 2–6, then:

```bash
set -a; source /srv/basketwise/basketwise.env; set +a
cd ~/basketwise/scraper
UV_PROJECT_ENVIRONMENT=.venv-hs uv run python ../deploy/scripts/sync_supabase.py pull-artifact --srv /srv/basketwise/data
~/basketwise/deploy/scripts/up.sh && ~/basketwise/deploy/scripts/frontend.sh
```

Then run Phase 9. The first crawl on the new box builds fresh `hyper_*.db` files. Price history already in Supabase is kept, and new changes append to it.

### Reset the Supabase sync

If you wipe or recreate the Supabase tables, delete the local sync state so the next run uploads everything again:

```bash
rm ~/basketwise/scraper/data/.supabase_state.json
```

---

## Troubleshooting

### HTTPS doesn't work / Caddy can't get a certificate

```bash
~/basketwise/deploy/scripts/up.sh logs caddy | tail -50
```

- `dig +short <DOMAIN>` must print `<EIP>`.
- The security group must allow **80 and 443 from anywhere**. Let's Encrypt connects from many IPs.
- On Cloudflare, the record must be **DNS only** (grey cloud).
- Don't delete the `caddy_data` volume. Let's Encrypt rate-limits repeat requests for the same hostname.

### The site loads but shows no products

- `curl -s https://<DOMAIN>/api/health`. If you get a 502, the API isn't running: `up.sh logs api`.
- `database not found at /data/basketwise.db` means no build has been published yet. Run Phase 8.
- DevTools → Network: requests should go to `/api/...`. If they go to `basket.taskglass.work`, the frontend was built without `VITE_API_BASE=/api`. Re-run `frontend.sh`.

### A retailer blocks the EC2 IP

Signs: the crawl log shows repeated backoffs and the store exits with code 2, or its product count stays at 0. Woolworths (Akamai) and Coles (Imperva) are the most likely; ALDI and Harris Farm have shown no anti-bot.

What happens automatically: that store keeps its last good data, and the run continues.

Your options:

1. **Live with it.** Three stores still make a working comparison site, as long as validation's minimums pass.
2. **Wait.** Flags often clear after a cooldown. The rate caps (`-max-rate`) are already conservative.
3. **Crawl from a home connection, serve from EC2.** `scraper/deploy/nightly.sh` was written for exactly this layout (`/srv/basketwise/data`, container `basketwise-api`). On your Mac:
   ```bash
   cd ~/Desktop/basketwise
   BASKETWISE_REMOTE=basketwise scraper/deploy/nightly.sh
   ```
   Disable the server's timer (`sudo systemctl disable --now basketwise-daily.timer`), and run the Supabase sync from the Mac afterwards with `SUPABASE_URL` and `SUPABASE_SECRET_KEY` exported. Use `launchd` or `cron` on the Mac to make it daily.

### The nightly job was killed / out of memory

```bash
journalctl -u basketwise-daily -n 100 --no-pager
sudo dmesg -T | grep -i -E 'killed process|out of memory' | tail
free -h
```

Check swap is on (`swapon --show`). On `t3.micro`, move up to `t3.small` if your free tier covers it: stop the instance → **Actions → Instance settings → Change instance type** → start. The Elastic IP stays attached.

### The nightly job is very slow

With **Standard** CPU credits, a `t3` box drops to its baseline speed once credits run out. Check **CloudWatch → EC2 → CPUCreditBalance**. Some slowdown is fine. If runs creep past about 6 hours, make sure nothing else is using CPU, or run the Coles full-detail crawl less often.

### Disk filling up

```bash
du -sh ~/basketwise/scraper/data/* | sort -h | tail
docker system prune -f        # old images and build cache
```

`master.db` and `hyper_woolworths.db` are the large files. Both are needed. If you're above 80% full, grow the volume: **EC2 → Volumes → Modify** to 40 GiB, then `sudo growpart /dev/nvme0n1 1 && sudo resize2fs /dev/nvme0n1p1`. Extra storage beyond the free allowance is billed.

### Supabase sync errors

- `401` / `Invalid API key`: wrong key in the env file. Use a **secret** key, not the publishable one. If a new `sb_secret_` key is rejected, try the legacy `service_role` key.
- `relation "store_products" does not exist`: run `deploy/supabase/schema.sql`.
- `Bucket not found`: the last statement of `schema.sql` didn't run. Create a **private** bucket named `artifacts` in the Storage UI.
- The project is paused: open the Supabase dashboard and click **Restore**. A daily sync keeps it awake, so a pause means the job hasn't been running.

### Supabase is nearly full

`price_history` is the table that grows. Thin out points older than a year:

```sql
delete from price_history where captured_at < now() - interval '365 days';
vacuum full price_history;
```

### Validation keeps failing

Read the reason in the journal (`VALIDATION FAILED:` lines). "products dropped >20%" after an intentional change? Delete `/srv/basketwise/state/last_build.json` once to reset the baseline.

---

## Security notes

- **No secrets in git.** `basketwise.env` lives only in `/srv/basketwise` with mode `600`. `.env` files are gitignored.
- **The Supabase secret key is server-only.** RLS is on with no policies, so the public key can do nothing.
- **SSH is limited to your IP**, with key-only login (Ubuntu's AMI default).
- **The API container** runs as a non-root user, with a read-only filesystem, all Linux capabilities dropped, a 512 MB memory cap, and the database mounted read-only.
- **Only Caddy has published ports** (80/443). The API is reachable only through it.
- **Automatic security updates** are on (`unattended-upgrades`). Reboot now and then for kernel updates: `sudo reboot`. All services come back on their own.
- **Be polite to the retailers.** The crawler's per-store rate caps exist to avoid hammering their sites. Don't raise them.

---

## Tearing it down

1. **EC2 → Instances →** select `basketwise` → **Instance state → Terminate**. This also deletes the root volume.
2. **EC2 → Elastic IPs →** select the IP → **Actions → Release Elastic IP address**. An unattached Elastic IP is billed.
3. **EC2 → Security Groups →** delete `basketwise-sg`. **Key Pairs →** delete `basketwise`.
4. Supabase → **Project Settings → General → Delete project**. Or leave it on the free plan; it pauses after 7 idle days.
5. Remove the DNS record if you used your own domain.

---

## Reference: files in `deploy/`

| File | Purpose |
|---|---|
| [`deploy/compose.yml`](../deploy/compose.yml) | API + Caddy containers. API runs with `--root-path /api`; Caddy strips the prefix. |
| [`deploy/Caddyfile`](../deploy/Caddyfile) | HTTPS, `/api/*` → FastAPI, everything else → React build with SPA fallback |
| [`deploy/basketwise.env.example`](../deploy/basketwise.env.example) | Template for `/srv/basketwise/basketwise.env` |
| [`deploy/supabase/schema.sql`](../deploy/supabase/schema.sql) | Supabase tables, RLS, store rows, `artifacts` bucket |
| [`deploy/scripts/up.sh`](../deploy/scripts/up.sh) | `docker compose` wrapper that loads the env file |
| [`deploy/scripts/frontend.sh`](../deploy/scripts/frontend.sh) | Builds the frontend in a Node container and publishes it to `/srv/basketwise/www` |
| [`deploy/scripts/daily.sh`](../deploy/scripts/daily.sh) | Nightly crawl → build → validate → publish → Supabase sync |
| [`deploy/scripts/validate.py`](../deploy/scripts/validate.py) | Publish gate (same checks as `scraper/deploy/nightly.sh`) |
| [`deploy/scripts/sync_supabase.py`](../deploy/scripts/sync_supabase.py) | `catalogue`, `push-artifact`, `pull-artifact` |
| [`deploy/systemd/basketwise-daily.service`](../deploy/systemd/basketwise-daily.service) | Runs `daily.sh` as `ubuntu`, with the env file and an 8-hour timeout |
| [`deploy/systemd/basketwise-daily.timer`](../deploy/systemd/basketwise-daily.timer) | 03:30 Australia/Sydney daily; catches up after downtime |

Related docs: [`backend/README.md`](../backend/README.md) (API internals), [`scraper/README.md`](../scraper/README.md) (crawler, rate limits, anti-bot tiers), [`Source-of-truth.md`](../Source-of-truth.md) (API contract).
