"""Create development TLS certificates. Never installs trust into any OS."""
import argparse
import ipaddress
from pathlib import Path
import shutil
import subprocess
import tempfile

from .scenario_server import local_ipv4_addresses


def create_certificates(directory, openssl=None, addresses=None):
    executable = openssl or shutil.which('openssl')
    if not executable:
        candidate = Path('C:/msys64/ucrt64/bin/openssl.exe')
        executable = str(candidate) if candidate.exists() else None
    if not executable:
        raise ValueError('OpenSSL не найден. Укажите --openssl с путём к openssl.exe.')
    destination = Path(directory).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    ips = sorted({str(ipaddress.ip_address(value)) for value in (addresses or local_ipv4_addresses())} | {'127.0.0.1'})
    ips = [ip for ip in ips if not ipaddress.ip_address(ip).is_unspecified]

    def run(*args, cwd):
        result = subprocess.run([executable, *args], cwd=cwd, capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('OpenSSL: ' + result.stderr.decode('utf-8', errors='replace')[-1500:])

    ca_key, ca_cert = destination / 'local-ca.key', destination / 'local-ca.crt'
    if ca_key.exists() != ca_cert.exists():
        raise ValueError('Есть только часть CA-пары. Восстановите local-ca.key и local-ca.crt или выберите новую папку.')
    with tempfile.TemporaryDirectory(prefix='cert-', dir=destination) as staging:
        temp = Path(staging)
        if ca_key.exists():
            shutil.copy2(ca_key, temp / ca_key.name)
            shutil.copy2(ca_cert, temp / ca_cert.name)
        else:
            run('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '365',
                '-subj', '/CN=Field Router Local Development CA', '-keyout', 'local-ca.key', '-out', 'local-ca.crt',
                '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
                '-addext', 'keyUsage=critical,keyCertSign,cRLSign', cwd=temp)
        run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-subj', '/CN=localhost',
            '-keyout', 'server.key', '-out', 'server.csr', cwd=temp)
        (temp / 'extensions.cnf').write_text(
            'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
            'extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,' + ','.join('IP:' + ip for ip in ips) + '\n', encoding='ascii')
        run('x509', '-req', '-in', 'server.csr', '-CA', 'local-ca.crt', '-CAkey', 'local-ca.key',
            '-CAcreateserial', '-days', '30', '-sha256', '-extfile', 'extensions.cnf', '-out', 'server.crt', cwd=temp)
        run('verify', '-CAfile', 'local-ca.crt', 'server.crt', cwd=temp)
        for name in ('local-ca.key', 'local-ca.crt', 'server.key', 'server.crt'):
            shutil.copy2(temp / name, destination / name)
    return {'directory': str(destination), 'addresses': ips, 'ca_certificate': str(ca_cert)}


def main():
    parser = argparse.ArgumentParser(description='Локальный HTTPS для GPS; доверие устанавливается отдельно на устройстве')
    parser.add_argument('--directory', type=Path, default=Path('output/local/https'))
    parser.add_argument('--openssl')
    parser.add_argument('--ip', action='append')
    args = parser.parse_args()
    result = create_certificates(args.directory, args.openssl, args.ip)
    print('Созданы TLS-сертификаты: ' + result['directory'])
    print('Адреса: ' + ', '.join(result['addresses']))
    print('На телефон передавайте ТОЛЬКО публичный local-ca.crt: ' + result['ca_certificate'])
    print('Установите доверие к этому CA на тестовом устройстве. Инструкция: README_GPS.md.')
    print('Закрытые *.key остаются на компьютере. Доверие в системе автоматически не изменялось.')


if __name__ == '__main__':
    main()
