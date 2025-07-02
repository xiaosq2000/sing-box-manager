# Sing-box VPN Client for Linux

## Features

- 🚀 **Smart Installation**: Automatically detects your shell and configures it
- 🌍 **Multi-language Support**: Full Chinese and English interface
- 🔒 **Secure**: No need to run with sudo - prompts for password only when needed
- 🎯 **Flexible Proxy Management**: Global and shell-local proxy settings
- 🛠️ **Multiple Protocols**: Supports Trojan and Hysteria2
- 📊 **Status Monitoring**: Check proxy status and network connectivity

## Installation

> [!TIP]
> The installer is designed to be user-friendly and secure. It will:
> 1. Automatically detect your shell (`bash` or `zsh`) and configure it
> 2. Install a systemd service (will prompt for sudo password)
> 3. Create backups of modified files

### Quick Install

```bash
./install.sh
```

### Installation Options

```bash
# Install with specific protocol
./install.sh -p hysteria2

# Install without modifying shell configuration
./install.sh --no-rc

# Install with verbose output
./install.sh -V

# Show help
./install.sh -h
```

### Supported Options

- `-h, --help` - Display help messages
- `-V, --verbose` - Enable debug logging
- `-p, --protocol PROTOCOL` - Specify protocol (trojan or hysteria2)
- `--no-rc` - Skip shell RC file configuration

## Usage

After installation, you'll have access to several commands for managing your proxy:

### Global Proxy Management

These commands affect system-wide proxy settings:

```bash
# Enable global proxy
set_proxy

# Disable global proxy
unset_proxy
```

### Shell-Local Proxy Management

These commands only affect the current shell session:

```bash
# Enable proxy for current shell only
set_local_proxy

# Disable proxy for current shell
unset_local_proxy
```

### Network Diagnostics

```bash
# Check your public IP address
check_public_ip

# Check your private IP address
check_private_ip

# Check proxy status and configuration
check_proxy_status

# Check if a port is available
check_port_availability 1080
```

### Advanced Usage

For debugging or detailed information:

```bash
# Verbose proxy status check
VERBOSE=true check_proxy_status

# Check public IP with custom timeout (in seconds)
check_public_ip 5
```

## Environment Support

The scripts automatically detect and adapt to your environment:

- **Native Linux**: Full functionality with systemd service management
- **WSL2**: Automatically configures proxy to use Windows host
- **Docker**: Detects container environment and uses host proxy
- **Language**: Automatically uses Chinese interface for zh_CN locale

## Troubleshooting

If the proxy doesn't work immediately after `set_proxy`:

1. Wait a few seconds for the service to start
2. Run `check_proxy_status` to verify configuration
3. For detailed debugging: `VERBOSE=true check_proxy_status`

## Uninstallation

To remove the service:

```bash
sudo systemctl stop sing-box-trojan.service
sudo systemctl disable sing-box-trojan.service
sudo rm /etc/systemd/system/sing-box-trojan.service
sudo rm -rf /usr/local/etc/sing-box
sudo rm /usr/local/bin/sing-box
```

To remove shell configuration, edit your `.bashrc` or `.zshrc` and remove the lines between:
```bash
# Network proxy management configuration
# and the next empty line
```
