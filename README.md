# Voicemail Forwarder

[![Docker Hub](https://img.shields.io/docker/v/dblagbro/voicemail-forwarder?label=Docker%20Hub)](https://hub.docker.com/r/dblagbro/voicemail-forwarder)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A production-ready Docker service that automatically monitors POP3 mailboxes for voicemails and forwards them via email with audio attachments. Perfect for integrating legacy voicemail systems with modern email workflows.

## 🌟 Key Features

- **🔄 Automated POP3 Polling**: Continuously monitors configured mailboxes with customizable intervals
- **📧 Smart Email Forwarding**: Forwards voicemails via SMTP with preserved audio attachments
- **🎯 Duplicate Prevention**: SQLite-based tracking ensures messages are never forwarded twice
- **🖥️ Web Management UI**: Browser-based interface for easy configuration and monitoring
- **📁 Attachment Archiving**: Automatically saves audio files locally for audit and backup
- **📊 Comprehensive Logging**: Detailed logs with POP3 response codes and processing status
- **🔐 Secure**: Supports TLS/STARTTLS for both POP3 and SMTP connections
- **🐳 Container-Ready**: Minimal Alpine-based Docker image with persistent volumes

## 📋 Table of Contents

- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Web Interface](#web-interface)
- [Architecture](#architecture)
- [Networking](#networking)
- [Volumes](#volumes)
- [Environment Variables](#environment-variables)
- [Building from Source](#building-from-source)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## 🚀 Quick Start

### Using Docker

```bash
docker run -d \
  --name voicemail-forwarder \
  --network host \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  dblagbro/voicemail-forwarder:latest
```

### Using Docker Compose

```yaml
services:
  voicemail-forwarder:
    image: dblagbro/voicemail-forwarder:latest
    container_name: voicemail-forwarder
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
```

The application has no built-in login. Restrict port 8089 to a trusted administration path and use an authenticated reverse proxy. Do not expose the raw port publicly. After starting, access the web UI at `http://localhost:8089`.

## ⚙️ Configuration

### Web Interface Configuration

The easiest way to configure the service is through the web UI at `http://localhost:8089`:

1. **Mailboxes Tab**: Add POP3 mailboxes to monitor
2. **Settings Tab**: Configure SMTP server for forwarding
3. **System Settings**: Adjust polling interval and preferences

### Manual Configuration

Configuration is stored in `data/config.json`:

```json
{
  "mailboxes": [
    {
      "name": "4013",
      "pop3_host": "mail.example.com",
      "pop3_port": 110,
      "pop3_user": "4013",
      "pop3_pass": "password",
      "use_stls": false,
      "dest_to": "user@example.com",
      "enabled": true
    }
  ],
  "smtp": {
    "host": "smtp.gmail.com",
    "port": 587,
    "use_tls": true,
    "username": "your-email@gmail.com",
    "password": "your-app-password"
  },
  "poll_interval_seconds": 300,
  "smtp_from": "voicemail@yourdomain.com"
}
```

### Configuration Options

#### Mailbox Settings

| Field | Description | Required |
|-------|-------------|----------|
| `name` | Friendly name for the mailbox | Yes |
| `pop3_host` | POP3 server hostname or IP | Yes |
| `pop3_port` | POP3 port (usually 110 or 995) | Yes |
| `pop3_user` | POP3 username | Yes |
| `pop3_pass` | POP3 password | Yes |
| `use_stls` | Enable STARTTLS encryption | No (default: false) |
| `dest_to` | Email address to forward messages to | Yes |
| `enabled` | Enable/disable this mailbox | No (default: true) |

#### SMTP Settings

| Field | Description | Required |
|-------|-------------|----------|
| `host` | SMTP server hostname | Yes |
| `port` | SMTP port (usually 587 or 465) | Yes |
| `use_tls` | Enable TLS encryption | No (default: true) |
| `username` | SMTP authentication username | Yes |
| `password` | SMTP authentication password | Yes |

#### System Settings

| Field | Description | Default |
|-------|-------------|---------|
| `poll_interval_seconds` | Seconds between polling cycles | 300 (5 min) |
| `smtp_from` | From address for forwarded emails | voicemail@localhost |

## 🖥️ Web Interface

The web interface provides comprehensive monitoring and management:

### Dashboard
- Manual poll trigger button
- System status overview
- Recent activity summary

### Mailboxes
- Add/edit/delete mailbox configurations
- Enable/disable individual mailboxes
- Test connections
- Change mailbox passwords

### Attachments
- Browse archived audio files
- Listen to voicemails directly in browser
- Download attachments
- View metadata (date, size, mailbox)

### Logs
- Real-time log viewer
- Filter by log level
- Search functionality
- Download logs

### Settings
- Configure SMTP server
- Adjust polling intervals
- Test email delivery

## 🏗️ Architecture

### Processing Flow

```
┌──────────────┐
│  POP3 Server │
└──────┬───────┘
       │ Poll every N seconds
       ▼
┌──────────────────┐
│ Voicemail        │
│ Forwarder        │
│                  │
│ 1. Connect       │
│ 2. Authenticate  │
│ 3. List messages │
│ 4. Check UIDL    │───► SQLite (seen messages)
│ 5. Fetch new     │
│ 6. Forward       │───► SMTP Server ───► Recipient
│ 7. Archive       │───► Local storage
└──────────────────┘
```

### Components

- **Flask Web Server**: Provides REST API and web UI
- **Core Poller**: Handles POP3 polling and message processing
- **State Manager**: SQLite database for tracking processed messages
- **Archive Manager**: Saves audio attachments to persistent storage
- **Gunicorn**: Production WSGI server

## 🌐 Networking

### Host Network Mode (Recommended)

The existing deployment uses `network_mode: host`; a routed Docker bridge can also reach LAN mail servers:

```yaml
network_mode: host
```

**Why host networking?**
- Direct access to LAN-based POP3 servers
- No NAT traversal issues
- Simplified configuration

### Bridge Network Mode (Alternative)

If using Docker bridge networking:

1. Ensure proper routing to POP3 servers
2. Map port 8089: `ports: ["8089:8089"]`
3. Configure firewall rules if needed

### Reverse Proxy

Example nginx configuration for HTTPS access:

```nginx
location /voicemail-forwarder {
    proxy_pass http://localhost:8089;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## 💾 Volumes

| Path | Purpose | Required |
|------|---------|----------|
| `/app/data` | Configuration and state database | Yes |
| `/app/logs` | Application logs | No |

### Data Directory Structure

```
data/
├── config.json           # Configuration file
└── state.db             # SQLite database (processed messages)

logs/
└── forwarder.log        # Application logs

attachments/             # Created automatically
├── mailbox_4013/
│   ├── 2026-01-10_voicemail.wav
│   └── ...
└── mailbox_1000/
    └── ...
```

## 🔧 Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PORT` | Web UI listen port | 8089 |
| `TZ` | Timezone for logs | UTC |
| `PYTHONUNBUFFERED` | Disable Python buffering | 1 |

Example:

```bash
docker run -d \
  -e TZ=America/New_York \
  -e PORT=8090 \
  --network host \
  dblagbro/voicemail-forwarder:latest
```

## 🔨 Building from Source

```bash
# Clone the repository
git clone https://github.com/dblagbro/voicemail-forwarder.git
cd voicemail-forwarder

# Build the image
docker build -t voicemail-forwarder:local .

# Run the container
docker run -d \
  --name voicemail-forwarder \
  --network host \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  voicemail-forwarder:local
```

### Development Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
cd app
python app.py
```

## 🔍 Troubleshooting

### Common Issues

#### Cannot connect to POP3 server

**Symptoms**: Logs show timeout or connection refused errors

**Solutions**:
- Verify POP3 host/port are correct
- Check firewall rules
- Test connection: `telnet pop3_host 110`
- Ensure using `network_mode: host` if on LAN

#### Messages not being forwarded

**Symptoms**: Logs show "already_seen" for new messages

**Solutions**:
- Check SMTP configuration in Settings
- Test SMTP connection
- Preserve `data/state.db`: deleting deduplication history can resend previously forwarded mail. Diagnose with a consistent SQLite backup in an isolated fixture.
- Check logs for SMTP errors

#### Web UI not accessible

**Symptoms**: Cannot reach http://localhost:8089

**Solutions**:
- Verify container is running: `docker ps`
- Check port mapping: `docker port voicemail-forwarder`
- Review container logs: `docker logs voicemail-forwarder`
- Ensure no port conflicts

#### Attachment not saving

**Symptoms**: Logs show forwarding but attachments missing

**Solutions**:
- Check volume permissions
- Verify disk space: `df -h`
- Review logs for file I/O errors

### Debug Logging

Enable detailed logging by checking the logs tab in the web UI or:

```bash
docker exec -it voicemail-forwarder tail -f /app/logs/forwarder.log
```

### Manual Testing

Test POP3 connection manually:

```bash
# Install telnet or nc
telnet pop3_server 110

# Commands to test
USER username
PASS password
STAT
UIDL
QUIT
```

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- Built with Flask and Gunicorn
- Uses SQLite for state management
- Containerized with Docker Alpine

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/dblagbro/voicemail-forwarder/issues)
- **Discussions**: [GitHub Discussions](https://github.com/dblagbro/voicemail-forwarder/discussions)

---

**Note**: This is designed for enterprise voicemail systems that use POP3 for message delivery. Common use cases include Avaya Communication Manager, Cisco Unity, and similar systems.

## Security maintenance (2026-11-11)

See [SECURITY-MAINTENANCE.md](SECURITY-MAINTENANCE.md) for the maintained runtime baseline, isolated validation, recovery prerequisites and deployment status. When a mailbox has `use_stls=true`, a TLS failure now stops that mailbox before USER/PASS; the connection-test UI reports the same failure. Explicit `use_stls=false` remains an operator-selected legacy mode.
