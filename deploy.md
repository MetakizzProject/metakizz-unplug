# Deploying MetaKizz Ambassador Challenge to Hostinger VPS

## Prerequisites
- Hostinger VPS with SSH access
- Python 3.10+ on the VPS
- A subdomain (e.g., `challenge.metakizzproject.com`) pointing to your VPS IP

## Step 1: DNS Setup
In your Hostinger DNS settings, add an A record:
- **Host**: `challenge` (or your preferred subdomain)
- **Points to**: Your VPS IP address
- **TTL**: 3600

## Step 2: Upload the Code
SSH into your VPS and clone or upload the project:
```bash
ssh root@YOUR_VPS_IP
mkdir -p /var/www/metakizz-challenge
cd /var/www/metakizz-challenge
# Upload your code here (git clone, scp, or SFTP)
```

## Step 3: Set Up Python Environment
```bash
cd /var/www/metakizz-challenge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Step 4: Configure Environment
```bash
cp .env.example .env  # or create .env manually
nano .env
```
Set all values, especially:
- `FLASK_SECRET_KEY` — generate with: `python3 -c "import secrets; print(secrets.token_hex(32))"`
- `APP_URL` — `https://challenge.metakizzproject.com`
- `ADMIN_PASSWORD` — your admin password
- `CIRCLE_API_TOKEN` and `CIRCLE_COMMUNITY_ID`
- `RESEND_API_KEY`

## Step 5: Initialize Database
```bash
source venv/bin/activate
python tools/db_init.py --seed
python tools/circle_fetch_members.py  # Import Circle members
```

## Step 6: Set Up Gunicorn Service
Create a systemd service file:
```bash
sudo nano /etc/systemd/system/metakizz.service
```

Content:
```ini
[Unit]
Description=MetaKizz Ambassador Challenge
After=network.target

[Service]
User=root
WorkingDirectory=/var/www/metakizz-challenge
Environment="PATH=/var/www/metakizz-challenge/venv/bin"
ExecStart=/var/www/metakizz-challenge/venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8000 "app.app:create_app()"
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable metakizz
sudo systemctl start metakizz
sudo systemctl status metakizz  # Verify it's running
```

## Step 7: Set Up Nginx Reverse Proxy
```bash
sudo nano /etc/nginx/sites-available/metakizz
```

Content:
```nginx
server {
    listen 80;
    server_name challenge.metakizzproject.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static/ {
        alias /var/www/metakizz-challenge/app/static/;
        expires 7d;
    }
}
```

Enable the site:
```bash
sudo ln -s /etc/nginx/sites-available/metakizz /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## Step 8: SSL Certificate (HTTPS)
```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d challenge.metakizzproject.com
```

## Step 9: Verify
Visit `https://challenge.metakizzproject.com` and test:
1. Home page loads
2. Email lookup works
3. Join page creates ambassadors
4. Referral links track registrations
5. Leaderboard displays
6. Admin panel works at `/admin`

## Useful Commands
```bash
# View logs
sudo journalctl -u metakizz -f

# Restart after code changes
sudo systemctl restart metakizz

# Re-run Circle import (safe to re-run)
cd /var/www/metakizz-challenge && source venv/bin/activate && python tools/circle_fetch_members.py

# Check milestones
cd /var/www/metakizz-challenge && source venv/bin/activate && python tools/check_milestones.py

# Export data
cd /var/www/metakizz-challenge && source venv/bin/activate && python tools/export_csv.py
```
