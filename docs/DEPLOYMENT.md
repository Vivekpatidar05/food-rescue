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

## Notes for the data-science layer

- `seed_data.py --reset` can populate Atlas with the 10k-batch synthetic
  history for the forecasting/fraud dashboards (run it once from the droplet).
- Holt-Winters forecasting and the IsolationForest audit run inside the API
  process — no extra workers needed at this scale.
- If RAM gets tight, upgrade the droplet before touching the code: the DS
  stack is the footprint, not the traffic.
