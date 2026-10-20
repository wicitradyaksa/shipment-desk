"""Checked launcher for one shared host. No network or firewall changes."""
import ipaddress
import json
from pathlib import Path
import ssl
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def arguments(config):
    host = config.get('host', '127.0.0.1')
    port = config.get('port', 8879)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError('Choose a port from 1024 to 65535.')
    try:
        address = ipaddress.IPv4Address(host)
    except (ValueError, TypeError):
        raise ValueError('Host must be the IPv4 address assigned to this computer.')
    if address.is_unspecified or address.is_multicast or address.is_reserved or address.is_link_local:
        raise ValueError('Use a specific office or private-network IPv4 address, not all interfaces.')
    args = ['--host', host, '--port', str(port)]
    if host != '127.0.0.1':
        private = any(address in ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10'))
        if not private:
            raise ValueError('This launcher supports private-network access only.')
        cert, key = config.get('certificate'), config.get('private_key')
        if not cert or not key:
            raise ValueError('Private-network access needs a certificate trusted by staff devices and its private key.')
        cert, key = ROOT / cert, ROOT / key
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        args += ['--cert', str(cert), '--key', str(key)]
    return args


if __name__ == '__main__':
    try:
        config = json.loads((ROOT/'host_config.json').read_text())
        args = arguments(config)
        scheme = 'https' if '--cert' in args else 'http'
        print(f"Staff workspace: {scheme}://{config['host']}:{config['port']}", flush=True)
        print('Keep this host running. Daily backups are stored in the backups folder.', flush=True)
        if '--check' in sys.argv:
            print('Startup configuration is valid. This check does not verify VPN connectivity or certificate trust on staff devices.')
        else:
            raise SystemExit(subprocess.call([sys.executable, str(ROOT/'app.py'), *args], cwd=ROOT))
    except (ValueError, OSError, ssl.SSLError) as error:
        print(f'Cannot start shared host: {error}', file=sys.stderr)
        raise SystemExit(1)
