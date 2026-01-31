# Groundtruth Studio - Quick Start Guide

## ✅ System is Ready!

Groundtruth Studio has been installed and is ready to use.

## Access Methods

### Option 1: Manual Start (Testing)

```bash
cd /var/www/html/groundtruth-studio
./start_server.sh
```

**Access at:**
- http://10.153.2.6:5000
- http://10.100.2.18:5000
- http://localhost:5000

Press `Ctrl+C` to stop the server.

### Option 2: Systemd Service (Recommended for Production)

Install as a system service that runs automatically:

```bash
# Copy service file
sudo cp /var/www/html/groundtruth-studio/groundtruth-studio.service /etc/systemd/system/

# Create log file
sudo touch /var/log/groundtruth-studio.log
sudo touch /var/log/groundtruth-studio-error.log
sudo chown ublirnevire:www-data /var/log/groundtruth-studio*.log

# Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable groundtruth-studio
sudo systemctl start groundtruth-studio
```

**Check status:**
```bash
sudo systemctl status groundtruth-studio
```

**View logs:**
```bash
sudo tail -f /var/log/groundtruth-studio.log
```

**Control service:**
```bash
sudo systemctl stop groundtruth-studio    # Stop
sudo systemctl start groundtruth-studio   # Start
sudo systemctl restart groundtruth-studio # Restart
```

## What's Included

✅ **29 Tag Groups** - Comprehensive taxonomy already seeded in database
✅ **Virtual Environment** - Python dependencies isolated in `venv/`
✅ **Dynamic Forms** - Annotation forms generated from database schema
✅ **Conditional Logic** - Forms adapt based on Ground Truth selection
✅ **Complete Documentation** - README.md, INSTALL.md, APACHE_SETUP.md

## First Steps

1. **Start the server** (use either option above)

2. **Open web interface**
   - http://10.153.2.6:5000 (or port 5000 on other IP)

3. **Add a test video**
   - Click "Upload" tab
   - Upload a video file
   - Or use "Download" tab with a YouTube URL

4. **Try annotation**
   - Click "Annotate" on any video
   - Click "+ Tag" to create a time-range tag
   - Select Ground Truth (required)
   - Select Confidence Level (required)
   - Fill in optional tag groups as needed
   - Notice how the form changes based on your selections!

5. **Explore tag groups**
   - Click "+ Keyframe" for frame-precise annotation
   - Try different Ground Truth values to see conditional forms
   - Check the "Negative Example" box to see false positive tags

## Tag Taxonomy Quick Reference

**Required (2):**
- Ground Truth - Primary classification
- Confidence Level - Annotator confidence

**Environmental (3):**
- Lighting, Weather, Water conditions

**Technical (2):**
- Camera Issues, Visibility Issues

**Behavioral (4):**
- Violation Context, Motor State, Boat Motion, Extenuating Circumstances

**Features (4):**
- False Positive types, Present/Absent Indicators

**Object Attributes (10):**
- Boat, Vehicle, Face properties

**Training (2):**
- Training Priority, Dataset Usage

**Review (2):**
- Reviewer Notes, Flags

See [SYSTEM_STATUS.md](SYSTEM_STATUS.md) for complete details.

## Troubleshooting

**Server won't start?**
```bash
cd /var/www/html/groundtruth-studio
venv/bin/python app/api.py
```
Check the error output.

**Port 5000 already in use?**
```bash
sudo lsof -i :5000
```
Kill the process or use a different port.

**Virtual environment missing?**
```bash
cd /var/www/html/groundtruth-studio
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**Database not seeded?**
```bash
cd /var/www/html/groundtruth-studio
venv/bin/python seed_taxonomy.py
```

## Next Steps

- **Production deployment**: See [APACHE_SETUP.md](APACHE_SETUP.md) for Apache reverse proxy
- **Complete documentation**: See [README.md](README.md)
- **Installation details**: See [INSTALL.md](INSTALL.md)
- **System status**: See [SYSTEM_STATUS.md](SYSTEM_STATUS.md)

---

**Groundtruth Studio is ready to use!**

Current status: ✅ Installed, ✅ Dependencies ready, ✅ Database seeded
