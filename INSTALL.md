# Groundtruth Studio - Installation Guide

## Quick Start

### 1. Install Dependencies

```bash
# Install Python packages
pip3 install -r requirements.txt

# Install system dependencies
sudo apt install ffmpeg python3-pip

# Install yt-dlp (if not already installed)
pip3 install yt-dlp
```

### 2. Seed Tag Taxonomy

Run this once to populate the database with the 29-group tag taxonomy:

```bash
cd /var/www/html/groundtruth-studio
python3 seed_taxonomy.py
```

You should see:
```
Seeding comprehensive tag taxonomy...
✓ Successfully seeded 29 tag groups with all options

Tag groups created:
  - Ground Truth (dropdown)
  - Confidence Level (dropdown)
  ... (27 more groups)

Total: 29 tag groups
```

### 3. Start the Server

**Option A: Development Mode** (Quick Start)

```bash
cd /var/www/html/groundtruth-studio
./start_server.sh
```

Access at:
- http://10.153.2.6:5000
- http://10.100.2.18:5000
- http://localhost:5000

**Option B: Production Mode** (Apache Reverse Proxy)

Follow the instructions in [APACHE_SETUP.md](APACHE_SETUP.md) to configure:
- http://10.153.2.6/groundtruth-studio/
- http://10.100.2.18/groundtruth-studio/

## Verification

1. Check system status:
```bash
# Verify Flask is installed
python3 -c "import flask; print(f'Flask {flask.__version__} installed')"

# Verify FFmpeg is installed
ffmpeg -version | head -1

# Verify yt-dlp is installed
yt-dlp --version
```

2. Test database seeding:
```bash
python3 seed_taxonomy.py
```

3. Test server startup:
```bash
./start_server.sh
```

Then in another terminal:
```bash
curl http://localhost:5000/api/system/status
```

Should return:
```json
{
  "success": true,
  "yt_dlp_installed": true,
  "ffmpeg_installed": true
}
```

## Directory Permissions

Ensure proper permissions for video storage:

```bash
cd /var/www/html/groundtruth-studio

# Create directories if they don't exist
mkdir -p downloads thumbnails static

# Set permissions (if needed)
chmod 755 downloads thumbnails static
chmod 644 video_archive.db
```

## Troubleshooting

### "ModuleNotFoundError: No module named 'flask'"

Install Flask:
```bash
pip3 install -r requirements.txt
```

### "command not found: ffmpeg"

Install FFmpeg:
```bash
sudo apt install ffmpeg
```

### Database connection errors

PostgreSQL handles concurrency natively and supports multiple simultaneous connections. Ensure the DATABASE_URL environment variable is set and the PostgreSQL service is running.

### Port 5000 already in use

Check what's using the port:
```bash
sudo netstat -tlnp | grep 5000
```

Kill the process or change the port in `app/api.py`:
```python
app.run(debug=True, host='0.0.0.0', port=5001)  # Changed to 5001
```

### Cannot access from external IP (10.153.2.6 or 10.100.2.18)

1. Check firewall:
```bash
sudo ufw status
sudo ufw allow 5000/tcp  # If needed
```

2. Verify Flask is binding to 0.0.0.0:
```bash
sudo netstat -tlnp | grep 5000
```

Should show `0.0.0.0:5000` not `127.0.0.1:5000`

3. Test from local machine first:
```bash
curl http://localhost:5000/
```

## Next Steps

Once installed and running:

1. **Access the main interface** at http://localhost:5000 (or configured URL)
2. **Add videos** using the Download or Upload tabs
3. **Annotate videos** by clicking "Annotate" on any video card
4. **Explore tag groups** - The system includes 29 predefined tag groups
5. **Review the README** for complete usage documentation

## Production Deployment

For production use, see:
- [APACHE_SETUP.md](APACHE_SETUP.md) - Apache reverse proxy configuration
- [README.md](README.md) - Complete system documentation

Consider:
- Setting up systemd service for automatic startup
- Configuring Apache reverse proxy
- Enabling HTTPS with SSL certificates
- Setting up database backups
- Configuring log rotation

## Support

Check these resources if you encounter issues:
1. [README.md](README.md) - Complete documentation
2. [APACHE_SETUP.md](APACHE_SETUP.md) - Apache configuration
3. System status indicator in web UI (top right corner)
4. Browser console for JavaScript errors
5. Flask logs for backend errors
