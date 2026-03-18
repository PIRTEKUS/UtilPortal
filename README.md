# UtilPortal

A cohesive IT self-serve portal built with Python, Flask, and Bootstrap 5. It allows end users to execute predefined database-oriented tasks safely through parameterized stored procedures or custom Python scripts. 

## Ubuntu Server Setup Instructions

These instructions will guide you through setting up a complete production-ready environment on an Ubuntu server (22.04 LTS recommended) using Nginx, Gunicorn, and MySQL.

### 1. Update and Install System Prerequisites

Open terminal and install the basic system packages:
```bash
sudo apt update
sudo apt upgrade -y
sudo apt install python3 python3-pip python3-venv nginx mysql-server libmysqlclient-dev pkg-config git curl -y
```

Next, you need to install the **Microsoft ODBC Driver 17** and **unixODBC** development headers so the application can communicate with SQL Server databases:

```bash
# Add the Microsoft repository key and list
curl https://packages.microsoft.com/keys/microsoft.asc | sudo tee /etc/apt/trusted.gpg.d/microsoft.asc
curl https://packages.microsoft.com/config/ubuntu/22.04/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list

# Update apt and install the ODBC driver and unixODBC
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql17
sudo apt-get install -y unixodbc-dev
```

### 2. Configure MySQL Database

Secure your MySQL installation and create the database and user for the portal:

```bash
sudo mysql_secure_installation
```
*(Follow prompt to set root password, remove anonymous users, disallow remote root login, etc.)*

Log into MySQL:
```bash
sudo mysql -u root -p
```

Create the database and a dedicated user:
```sql
CREATE DATABASE utilportal CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'portaluser'@'localhost' IDENTIFIED BY 'StrongPassword123!';
GRANT ALL PRIVILEGES ON utilportal.* TO 'portaluser'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

### 3. Setup Project Directory and Environment

It's best practice to run web applications out of `/var/www` or `/opt`. We will use `/opt/utilportal`.

```bash
# Create the directory
sudo mkdir -p /opt/utilportal

# Clone the repository
git clone https://github.com/PIRTEKUS/UtilPortal.git /opt/utilportal
cd /opt/utilportal

# Set ownership and permissions so Nginx/Gunicorn can operate
sudo chown -R $USER:www-data /opt/utilportal
sudo chmod -R 775 /opt/utilportal

# Create a virtual environment
python3 -m venv venv

# Activate and install dependencies
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Application Configuration

Create a `.env` file in the root directory (`/opt/utilportal/.env`) and add your settings:

```env
SECRET_KEY=your-super-secret-key-here
DATABASE_URL=mysql+pymysql://portaluser:StrongPassword123!@localhost/utilportal

# Placeholders for future Microsoft Azure AD (Office 365) SSO integration
AZURE_CLIENT_ID=
AZURE_TENANT_ID=
AZURE_CLIENT_SECRET=
```

### 5. Initialize the Database Schema
With the virtual environment activated, run the Python initialization script to build the SQL tables and create the default admin user:
```bash
python init_db.py
```

### 6. Setup Gunicorn Systemd Service

Create a systemd service file to keep the application running in the background.

```bash
sudo nano /etc/systemd/system/utilportal.service
```

Add the following configuration (adjust usernames/paths if different):
```ini
[Unit]
Description=Gunicorn instance to serve UtilPortal
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/utilportal
Environment="PATH=/opt/utilportal/venv/bin"
# Run gunicorn, binding it to a local unix socket
ExecStart=/opt/utilportal/venv/bin/gunicorn --workers 3 --bind unix:utilportal.sock -m 007 wsgi:app

[Install]
WantedBy=multi-user.target
```

Start and enable the service:
```bash
sudo systemctl start utilportal
sudo systemctl enable utilportal
```

### 7. Configure Nginx Reverse Proxy and SSL (Required for SSO)

Microsoft Azure AD exclusively requires HTTPS for all redirect URIs (unless testing on `localhost`). We will generate a self-signed SSL certificate so Nginx can securely serve the application.

Generate the SSL certificate:
```bash
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout /etc/ssl/private/nginx-selfsigned.key -out /etc/ssl/certs/nginx-selfsigned.crt -subj "/C=US/ST=State/L=City/O=Organization/CN=your_domain_or_ip"
```

Create the Nginx server block:
```bash
sudo nano /etc/nginx/sites-available/utilportal
```

Add this complete configuration (replace `your_domain_or_ip` in both places):
```nginx
server {
    listen 80;
    server_name your_domain_or_ip;
    
    # Automatically redirect all HTTP traffic to HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name your_domain_or_ip;

    ssl_certificate /etc/ssl/certs/nginx-selfsigned.crt;
    ssl_certificate_key /etc/ssl/private/nginx-selfsigned.key;

    location / {
        include proxy_params;
        proxy_pass http://unix:/opt/utilportal/utilportal.sock;
        
        # Ensure Flask knows it's being accessed via HTTPS
        proxy_set_header X-Forwarded-Proto https;
    }

    # Optional: Serve static files directly via Nginx
    location /static/ {
        alias /opt/utilportal/static/;
    }
}
```

Enable the site and restart Nginx:
```bash
# If the default site is still enabled, remove it to prevent conflicts
sudo rm -f /etc/nginx/sites-enabled/default

sudo ln -s /etc/nginx/sites-available/utilportal /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### 8. Final Checks
Open your web browser and navigate to your server's IP address or domain. The UtilPortal login screen should load successfully!

---

## Updating the Application

When new features or fixes are pushed to the GitHub repository, you can easily pull the latest code and apply it to your running server:

```bash
# Navigate to the project folder
cd /opt/utilportal

# Pull the latest changes
sudo git pull

# Activate your virtual environment and install any new dependencies
source venv/bin/activate
pip install -r requirements.txt

# Run the database init script again (safe to run on existing DBs)
# to apply any new database schema changes
python init_db.py

# Restart the Gunicorn service so it serves the new code
sudo systemctl restart utilportal
```
