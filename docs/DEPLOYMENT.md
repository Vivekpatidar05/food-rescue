# Deploying FoodRescue (GitHub Student Developer Pack edition)

FoodRescue is a data-science-heavy Flask app: the container needs **pandas,
scikit-learn, statsmodels and spaCy (+ the `en_core_web_sm` model)** — about
1 GB installed and 500–800 MB resident. That rules out tiny "hobby slug"
platforms and makes a plain VM the best home for the API.

## Recommended stack (everything covered by the Student Pack)

| Piece | Service | Student Pack benefit | Why |
| --- | --- | --- | --- |
| API + frontend | **DigitalOcean Droplet** (Ubuntu, 2 GB RAM, ~$12/mo) | **$200 credit** → ~16 months free | No image-size limits — the full DS stack installs like on your laptop |
| Database | **MongoDB Atlas M0** (free tier, 512 MB) | **$50 Atlas credit** to upgrade later | Managed, backed up, 2dsphere indexes work out of the box |
| Domain | **Namecheap** free `.me` (or **.TECH** / **Name.com** `.app`/`.dev`) | Free for 1 year | Real HTTPS URL for the PWA |
| TLS | Let's Encrypt (certbot) | free anyway | Auto-renewing certificates |
| Email (OTP + resets) | **Brevo** free tier | 300 emails/day free | Already integrated — just set `BREVO_API_KEY` |

Alternatives from the pack, and why they are second choice here:

- **Microsoft Azure ($100 credit)** — works (App Service B1), but the credit
  lasts ~7 months vs DigitalOcean's ~16, and B1's 1.75 GB RAM is tighter once
  spaCy loads.
- **Heroku ($13/month for 24 months)** — the 500 MB compressed slug limit is
  a real risk with pandas + scikit-learn + statsmodels + spaCy. Possible if
  you strip the DS layer; not recommended for the full app.

## Step-by-step (DigitalOcean droplet)

### 1. Database — MongoDB Atlas

1. Create a free **M0 cluster** (choose a region near your droplet).
2. Add a database user and allow your droplet's IP in Network Access.
3. Copy the connection string:
   `mongodb+srv://USER:PASS@cluster0.xxxx.mongodb.net/food_rescue`

### 2. Droplet

Create an Ubuntu 24.04 droplet (Basic, 2 GB RAM). Then:

```bash
sudo apt update && sudo apt install -y python3.12-venv git nginx
git clone https://github.com/Vivekpatidar05/food-rescue.git
cd food-rescue/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

### 3. Production `.env` (in the repo root, next to `backend/`)

```ini
MONGO_URI=mongodb+srv://USER:PASS@cluster0.xxxx.mongodb.net/food_rescue
JWT_SECRET=<64 random hex chars — python -c "import secrets; print(secrets.token_hex(32))">
CORS_ORIGINS=https://yourdomain.me
FLASK_ENV=production            # waitress (real WSGI server) takes over
ADMIN_EMAIL=<your admin email>
ADMIN_PASSWORD=<strong password>
BREVO_API_KEY=<key from brevo.com>   # signup OTPs + password-reset emails
SENDER_EMAIL=no-reply@yourdomain.me
SENDER_NAME=FoodRescue
```

### 4. Run the API as a systemd service

`/etc/systemd/system/foodrescue.service`:

```ini
[Unit]
Description=FoodRescue API
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/food-rescue/backend
ExecStart=/opt/food-rescue/backend/.venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now foodrescue
```

### 5. Serve the frontend from the same domain (no CORS pain)

Point the frontend at the same origin: in each `frontend/*.js`, change
`const API_BASE = "http://localhost:5000"` (and `var API` in `fr-ui.js`) to
`""` — requests then hit the same domain and nginx routes them.

`/etc/nginx/sites-available/foodrescue`:

```nginx
server {
    server_name yourdomain.me;
    root /opt/food-rescue/frontend;
    index auth.html;

    # API blueprints — everything else is a static file
    location ~ ^/(health|auth|donor|ngo|volunteer|media|stats|api|gamification|social|admin|integrations|esg)($|/) {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/foodrescue /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 6. Domain + HTTPS

1. Claim your free `.me` at Namecheap (Student Pack) and add an **A record**
   pointing to the droplet IP.
2. `sudo apt install -y certbot python3-certbot-nginx && sudo certbot --nginx -d yourdomain.me`

### 7. Smoke test

- `https://yourdomain.me/health` → `{"status": "ok"}`
- Sign-up flow sends a real OTP email (Brevo key set → no `dev_code` leaks).
- `admin.html` signs in with your `.env` admin credentials.

### 8. HTTPS hardening (HSTS)

certbot handles the certificate; add an HSTS header so browsers refuse plain
HTTP for a year. In the `443` server block that certbot created in
`/etc/nginx/sites-available/foodrescue`, add inside `server { … }`:

```nginx
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

Then `nginx -t && systemctl reload nginx`. (The Flask app already sets
`X-Frame-Options`, `X-Content-Type-Options` and `Referrer-Policy`.)

## Automated backups

MongoDB Atlas **M0 (free tier) has no automated backups** — a bad delete or
migration is unrecoverable. `scripts/backup_db.py` closes that gap: it dumps
every collection to one gzipped Extended-JSON file (lossless — ObjectIds and
dates survive) and prunes anything older than 14 days.

Install it as a daily cron job on the droplet:

```bash
cat > /etc/cron.d/foodrescue-backup <<'EOF'
30 2 * * * root /opt/food-rescue/backend/.venv/bin/python /opt/food-rescue/scripts/backup_db.py >> /var/log/foodrescue-backup.log 2>&1
EOF
chmod 644 /etc/cron.d/foodrescue-backup
# prove it works right away:
/opt/food-rescue/backend/.venv/bin/python /opt/food-rescue/scripts/backup_db.py
```

Backups land in `/opt/food-rescue-backups/` (override with `FR_BACKUP_DIR`).
Restore one with:

```bash
# merge (upsert by _id):
python scripts/restore_db.py /opt/food-rescue-backups/foodrescue-YYYYMMDD-HHMMSS.jsonl.gz
# exact point-in-time restore (drops collections first):
python scripts/restore_db.py <file.jsonl.gz> --wipe
```

> These backups live **on the droplet**, so they cover app/DB-level data loss
> but not loss of the droplet itself. For real disaster recovery, copy them
> off-box periodically — `scp` them down, or push to DigitalOcean Spaces / S3.

## Email deliverability (Brevo domain authentication)

Sending "from" a `@gmail.com` address via Brevo works but Gmail's DMARC policy
sends much of it to spam — which silently breaks signup, since users need the
OTP email. Authenticate your own domain so mail passes SPF/DKIM/DMARC:

1. **Brevo → Senders, Domains & Dedicated IPs → Domains → Add a domain** →
   enter your domain.
2. Brevo shows DNS records unique to your account — a **Brevo code** (TXT), a
   **DKIM** record (TXT at `mail._domainkey`), and a **DMARC** record (TXT at
   `_dmarc`); it may also ask you to add `include:spf.brevo.com` to your SPF.
3. Add each record **exactly as shown** in your DNS host (name.com → *Manage
   DNS*): Host is the subdomain part (`@`, `mail._domainkey`, `_dmarc`), Type
   usually TXT, Value pasted verbatim.
4. Wait ~5–15 min for DNS, then click **Authenticate / Verify** in Brevo.
5. Point the app at a domain sender and restart:

   ```ini
   SENDER_EMAIL=no-reply@yourdomain.tld
   SENDER_NAME=FoodRescue
   ```
   ```bash
   systemctl restart foodrescue
   ```

After this, OTP and password-reset emails send from your domain and reach
inboxes instead of spam.

## Post-launch security checklist

- **Tighten Atlas Network Access** from `0.0.0.0/0` to the droplet's IP only.
- **Revoke the deploy API token** once provisioning is done (regenerate if you
  need to manage the droplet again later).
- **Firewall:** `ufw` should expose only `22`, `80`, `443`; the app port
  (`5000`) stays bound to the droplet and reachable only via nginx.
- **Rotate** any secret that was ever pasted into a shared channel
  (`JWT_SECRET`, DB password, Brevo key) before real traffic.

## Notes for the data-science layer

- `seed_data.py --reset` can populate Atlas with the 10k-batch synthetic
  history for the forecasting/fraud dashboards (run it once from the droplet).
- Holt-Winters forecasting and the IsolationForest audit run inside the API
  process — no extra workers needed at this scale.
- If RAM gets tight, upgrade the droplet before touching the code: the DS
  stack is the footprint, not the traffic.
