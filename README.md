# UtilPortal

A cohesive IT self-serve portal built with Python, Flask, and Bootstrap 5. It allows end users to execute predefined database-oriented tasks safely through parameterized stored procedures or custom Python scripts. 

## Ubuntu Server Setup Instructions

These instructions will guide you through setting up a complete production-ready environment on an Ubuntu server (22.04 LTS recommended) using Nginx, Gunicorn, and MySQL.

### 1. Update and Install System Prerequisites

Open terminal and run:
```bash
sudo apt update
sudo apt upgrade -y
sudo apt install python3 python3-pip python3-venv nginx mysql-server libmysqlclient-dev pkg-config git -y
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
# Make your ubuntu user the owner
sudo chown -R $USER:$USER /opt/utilportal

# Clone the repository
git clone https://github.com/PIRTEKUS/UtilPortal.git /opt/utilportal
cd /opt/utilportal

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
With the virtual environment activated, run the Flask initialization commands to build the SQL tables:
```bash
# Later, you will run command(s) like this (once we create the DB init script)
# flask shell
# >>> from models import db
# >>> db.create_all()
# >>> exit()
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

### 7. Configure Nginx Reverse Proxy

Create an Nginx server block to point to the Gunicorn socket.

```bash
sudo nano /etc/nginx/sites-available/utilportal
```

Add this configuration (replace `your_domain_or_ip`):
```nginx
server {
    listen 80;
    server_name your_domain_or_ip;

    location / {
        include proxy_params;
        proxy_pass http://unix:/opt/utilportal/utilportal.sock;
    }

    # Optional: Serve static files directly via Nginx
    location /static/ {
        alias /opt/utilportal/static/;
    }
}
```

Enable the site and restart Nginx:
```bash
sudo ln -s /etc/nginx/sites-available/utilportal /etc/nginx/sites-enabled
sudo nginx -t
sudo systemctl restart nginx
```

### 8. Final Checks
Open your web browser and navigate to your server's IP address or domain. The UtilPortal login screen should load successfully!
