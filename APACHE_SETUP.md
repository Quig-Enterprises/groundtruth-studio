# Apache Reverse Proxy Setup for Groundtruth Studio

This guide explains how to make Groundtruth Studio accessible at:
- `http://10.153.2.6/groundtruth-studio/`
- `http://10.100.2.18/groundtruth-studio/`

## Option 1: Quick Start (Development Mode)

Run the Flask application directly on port 5000:

```bash
cd /var/www/html/groundtruth-studio
./start_server.sh
```

Access at:
- `http://10.153.2.6:5000`
- `http://10.100.2.18:5000`
- `http://localhost:5000`

## Option 2: Apache Reverse Proxy (Production Mode)

### Step 1: Enable Apache Proxy Modules

```bash
sudo a2enmod proxy proxy_http
```

### Step 2: Update Apache Configuration

Edit `/etc/apache2/sites-available/wordpress.conf` and add this section inside the `<VirtualHost *:80>` block:

```apache
# Groundtruth Studio reverse proxy
ProxyPass /groundtruth-studio http://localhost:5000
ProxyPassReverse /groundtruth-studio http://localhost:5000

<Location /groundtruth-studio>
    Require all granted
</Location>
```

Complete configuration should look like:

```apache
<VirtualHost *:80>
    ServerName localhost
    DocumentRoot /var/www/html/wordpress

    # Alias for eqmon application
    Alias /eqmon /var/www/html/eqmon
    <Directory /var/www/html/eqmon>
        Options -Indexes +FollowSymLinks
        AllowOverride All
        Require all granted
    </Directory>

    # Groundtruth Studio reverse proxy
    ProxyPass /groundtruth-studio http://localhost:5000
    ProxyPassReverse /groundtruth-studio http://localhost:5000

    <Location /groundtruth-studio>
        Require all granted
    </Location>

    <Directory /var/www/html/wordpress>
        Options FollowSymLinks
        AllowOverride All
        Require all granted
    </Directory>

    ErrorLog ${APACHE_LOG_DIR}/wordpress_error.log
    CustomLog ${APACHE_LOG_DIR}/wordpress_access.log combined
</VirtualHost>
```

### Step 3: Update Flask Application

Edit `/var/www/html/groundtruth-studio/app/api.py` to support running behind a reverse proxy.

Find this line near the end:
```python
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
```

Add this configuration before the `if __name__ == '__main__':` line:

```python
# Support for reverse proxy
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
```

### Step 4: Create Systemd Service

Create `/etc/systemd/system/groundtruth-studio.service`:

```ini
[Unit]
Description=Groundtruth Studio Flask Application
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/var/www/html/groundtruth-studio
Environment="PATH=/usr/bin:/usr/local/bin"
ExecStart=/usr/bin/python3 /var/www/html/groundtruth-studio/app/api.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Step 5: Enable and Start Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable groundtruth-studio
sudo systemctl start groundtruth-studio
```

### Step 6: Restart Apache

```bash
sudo systemctl restart apache2
```

### Step 7: Test

Access Groundtruth Studio at:
- `http://10.153.2.6/groundtruth-studio/`
- `http://10.100.2.18/groundtruth-studio/`

## Option 3: Standalone Static Files (No Flask)

If you only want to serve the frontend without the backend API:

```bash
sudo ln -s /var/www/html/groundtruth-studio /var/www/html/groundtruth-studio
```

Then add to Apache config:

```apache
Alias /groundtruth-studio /var/www/html/groundtruth-studio
<Directory /var/www/html/groundtruth-studio>
    Options -Indexes +FollowSymLinks
    AllowOverride All
    Require all granted
</Directory>
```

**Note:** This method won't work because the application requires the Flask API to function. Use Option 1 or Option 2.

## Verification

Check if the Flask service is running:

```bash
# Check systemd service status
sudo systemctl status groundtruth-studio

# Check if Flask is listening on port 5000
sudo netstat -tlnp | grep 5000

# Test direct access
curl http://localhost:5000/

# Test proxied access
curl http://localhost/groundtruth-studio/
```

## Troubleshooting

### 502 Bad Gateway
- Ensure Flask service is running: `sudo systemctl status groundtruth-studio`
- Check Flask logs: `sudo journalctl -u groundtruth-studio -f`

### 404 Not Found
- Verify proxy configuration in Apache config
- Restart Apache: `sudo systemctl restart apache2`

### Assets Not Loading
- The Flask application uses absolute paths - ensure ProxyFix is configured
- Check browser console for errors

### Permission Errors
- Ensure www-data user has access to video archive directory
- Check file permissions: `ls -la /var/www/html/groundtruth-studio/`

## Current Status

The system is currently installed at:
- **Installation Path**: `/var/www/html/groundtruth-studio/`
- **Database**: `video_archive.db` (SQLite)
- **Videos**: `downloads/` directory
- **Thumbnails**: `thumbnails/` directory

To complete the setup, follow **Option 2** above (recommended for production) or **Option 1** for quick testing.
