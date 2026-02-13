# Voicemail Forwarder

A Docker-based service that polls POP3 mailboxes for voicemails and forwards them via email with attachments.

## Features

- **POP3 Polling**: Automatically checks configured mailboxes for new voicemails
- **Email Forwarding**: Forwards voicemails with audio attachments via SMTP
- **Web UI**: Browser-based interface for configuration and monitoring
- **Attachment Archive**: Saves audio attachments locally for reference
- **Detailed Logging**: Comprehensive logging of all operations
- **Duplicate Detection**: Tracks processed messages to prevent re-sending

## Quick Start

### Using Docker Hub Image

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
voicemail-forwarder:
  image: dblagbro/voicemail-forwarder:latest
  container_name: voicemail-forwarder
  restart: unless-stopped
  network_mode: host
  volumes:
    - ./data:/app/data
    - ./logs:/app/logs
```

## Configuration

The web UI is available at `http://localhost:8089` (default port).

Configure your mailboxes and SMTP settings through the web interface:
- **Mailboxes**: Add POP3 mailboxes to monitor
- **Settings**: Configure SMTP server for forwarding emails
- **System Settings**: Adjust polling interval and other options

### Configuration File

Configuration is stored in `data/config.json`:

```json
{
  "mailboxes": [
    {
      "name": "4013",
      "pop3_host": "192.168.1.100",
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
  "poll_interval_seconds": 300
}
```

## Network Mode

This service requires `network_mode: host` to properly access POP3 servers on the local network. If using Docker bridge networking, ensure proper routing is configured.

## Volumes

- `/app/data` - Configuration and state database
- `/app/logs` - Application logs

## Environment

- Built on Python 3.12 Alpine
- Runs on Gunicorn with Flask
- Uses SQLite for tracking processed messages

## Building from Source

```bash
git clone https://github.com/dblagbro/voicemail-forwarder.git
cd voicemail-forwarder
docker build -t voicemail-forwarder .
```

## License

Copyright © 2026. All rights reserved.

## Support

For issues or questions, please open an issue on GitHub.
