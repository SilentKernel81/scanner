# Card Scanner - Installation Guide

Complete installation guide for deploying the Card Scanner on Raspberry Pi OS (Raspbian) and Ubuntu.

## Table of Contents

- [Quick Start](#quick-start)
- [System Requirements](#system-requirements)
- [Manual Installation](#manual-installation)
- [Automated Installation](#automated-installation)
- [Post-Installation Setup](#post-installation-setup)
- [Running the Scanner](#running-the-scanner)
- [Setting Up as a Service](#setting-up-as-a-service)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)

---

## Quick Start

For those who want to get started quickly:

```bash
# Clone or copy the project to your system
cd card_scanner_hailo

# Make install script executable
chmod +x install.sh

# Run the installer
./install.sh

# Follow the prompts
```

---

## System Requirements

### Hardware

**Minimum:**
- Raspberry Pi 4 (2GB RAM) or equivalent x86_64 system
- USB webcam or Raspberry Pi Camera Module
- 2GB free disk space (more for card images)
- Internet connection for initial setup

**Recommended:**
- Raspberry Pi 5 (4GB+ RAM) or modern x86_64 system
- USB webcam with autofocus
- 10GB+ free disk space
- Hailo-8L AI Accelerator (optional, for accelerated detection)

### Software

**Operating Systems:**
- Raspberry Pi OS (Raspbian) Bullseye or later
- Ubuntu 20.04 LTS or later
- Debian 11 or later

**Required:**
- Python 3.8 or higher
- Internet connection (for downloading card database and models)

---

## Manual Installation

### 1. Install System Dependencies

**On Raspberry Pi OS / Debian:**
```bash
sudo apt-get update
sudo apt-get install -y \
    python3 python3-pip python3-venv python3-dev \
    python3-opencv libopencv-dev \
    tesseract-ocr libtesseract-dev \
    v4l-utils git \
    python3-picamera2 libcamera-tools
```

**On Ubuntu:**
```bash
sudo apt-get update
sudo apt-get install -y \
    python3 python3-pip python3-venv python3-dev \
    python3-opencv libopencv-dev \
    tesseract-ocr libtesseract-dev \
    v4l-utils git
```

### 2. Create Virtual Environment

```bash
cd card_scanner_hailo
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Link System Python Packages (for GStreamer)

```bash
SITE_PACKAGES=$(python3 -c "import site; print(site.getsitepackages()[0])")
echo "/usr/lib/python3/dist-packages" > "$SITE_PACKAGES/system-gi.pth"
```

### 5. Download YOLOv8 Model

```bash
pip install ultralytics
python3 -c "from ultralytics import YOLO; model = YOLO('yolov8n.pt')"
```

### 6. Download Card Database

```bash
python3 setup_database.py
```

This downloads ~150MB of card data from Scryfall and takes 5-10 minutes.

---

## Automated Installation

### Using install.sh Script

The automated installer handles all setup steps:

```bash
# Make executable
chmod +x install.sh

# Run installer
./install.sh
```

The script will:
1. Detect your operating system
2. Install system dependencies
3. Create Python virtual environment
4. Install Python packages
5. Download YOLOv8 model
6. Offer to download card database
7. Offer to set up systemd service

---

## Post-Installation Setup

### 1. Set Up Vision AI API Key

The scanner uses Vision AI to automatically identify cards. Choose ONE provider:

#### Google Gemini (Recommended - Free Tier Available)

1. Get API key: https://makersuite.google.com/app/apikey
2. Set environment variable:
   ```bash
   export GEMINI_API_KEY='your-api-key-here'
   ```
3. Make it permanent:
   ```bash
   echo 'export GEMINI_API_KEY="your-api-key-here"' >> ~/.bashrc
   source ~/.bashrc
   ```

#### OpenAI GPT-4

1. Get API key: https://platform.openai.com/api-keys
2. Set environment variable:
   ```bash
   export OPENAI_API_KEY='your-api-key-here'
   ```
3. Update config.py:
   ```python
   VISION_AI_PROVIDER = 'openai'
   ```

#### Anthropic Claude

1. Get API key: https://console.anthropic.com/settings/keys
2. Set environment variable:
   ```bash
   export ANTHROPIC_API_KEY='your-api-key-here'
   ```
3. Update config.py:
   ```python
   VISION_AI_PROVIDER = 'anthropic'
   ```

### 2. Configure Camera (Optional)

Edit `config.py` to adjust camera settings:

```python
# Camera configuration
CAMERA_RESOLUTION = (2560, 1440)
CAMERA_FPS = 30
CAMERA_TYPE = 'auto'  # 'auto', 'usb', or 'picamera'
```

---

## Running the Scanner

### Method 1: Manual Start

```bash
# Navigate to installation directory
cd card_scanner_hailo

# Activate virtual environment
source venv/bin/activate

# Start the scanner
python3 app.py
```

### Method 2: System Service

If you installed the systemd service:

```bash
# Start service
sudo systemctl start card-scanner

# Check status
sudo systemctl status card-scanner

# View logs
sudo journalctl -u card-scanner -f

# Enable on boot
sudo systemctl enable card-scanner
```

### Accessing the Web Interface

Once started, access the scanner at:

- **Local:** http://localhost:5000
- **Network:** http://YOUR_PI_IP:5000

To find your Pi's IP address:
```bash
hostname -I
```

---

## Setting Up as a Service

### Automatic Setup

Run the installer and choose "Yes" when asked about systemd service.

### Manual Setup

1. Create service file:
   ```bash
   sudo nano /etc/systemd/system/card-scanner.service
   ```

2. Add configuration (replace paths):
   ```ini
   [Unit]
   Description=Card Scanner Web Service
   After=network.target

   [Service]
   Type=simple
   User=YOUR_USERNAME
   WorkingDirectory=/path/to/card_scanner_hailo
   Environment="PATH=/path/to/card_scanner_hailo/venv/bin"
   Environment="GEMINI_API_KEY=your-api-key-here"
   ExecStart=/path/to/card_scanner_hailo/venv/bin/python3 /path/to/card_scanner_hailo/app.py
   Restart=on-failure
   RestartSec=10

   [Install]
   WantedBy=multi-user.target
   ```

3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable card-scanner
   sudo systemctl start card-scanner
   ```

---

## Troubleshooting

### Camera Not Detected

**USB Camera:**
```bash
# List available cameras
v4l2-ctl --list-devices

# Test camera
ffplay /dev/video0
```

**Pi Camera:**
```bash
# Test Pi camera
libcamera-hello

# Check camera status
vcgencmd get_camera
```

### Port Already in Use

If port 5000 is already in use, edit `config.py`:
```python
PORT = 8080  # Change to available port
```

### Database Not Loading

```bash
# Check database exists
ls -lh data/cards_database.db

# Re-download if needed
rm data/cards_database.db
python3 setup_database.py
```

### Vision AI Not Working

```bash
# Verify API key is set
echo $GEMINI_API_KEY

# Test connectivity
curl -H "x-goog-api-key: $GEMINI_API_KEY" \
  https://generativelanguage.googleapis.com/v1/models
```

### Check Logs

```bash
# Application logs
tail -f data/logs/card_scanner.log

# System service logs
sudo journalctl -u card-scanner -f
```

### Python Package Issues

```bash
# Clean install
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Uninstalling

### Automated Uninstall

```bash
chmod +x uninstall.sh
./uninstall.sh
```

### Manual Uninstall

1. Stop and remove service:
   ```bash
   sudo systemctl stop card-scanner
   sudo systemctl disable card-scanner
   sudo rm /etc/systemd/system/card-scanner.service
   sudo systemctl daemon-reload
   ```

2. Remove virtual environment:
   ```bash
   rm -rf venv
   ```

3. Remove data (optional):
   ```bash
   rm -rf data scanned_cards
   ```

4. Remove system packages (optional):
   ```bash
   sudo apt-get remove python3-opencv tesseract-ocr
   sudo apt-get autoremove
   ```

---

## Additional Resources

- **User Guide:** See `README.md`
- **Project Instructions:** See `CLAUDE.md`
- **Vision AI Setup:** See `SETUP_VISION_AI.md`
- **Hailo Support:** See `HAILO_STATUS.md` and `NEXT_STEPS.md`

---

## Support

For issues or questions:
1. Check the troubleshooting section above
2. Review application logs: `data/logs/card_scanner.log`
3. Check GitHub issues (if project is on GitHub)

---

## License

See LICENSE file for details.
