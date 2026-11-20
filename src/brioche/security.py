"""Persistent server identity, ephemeral access token and mandatory certificate pin."""
import datetime as dt
import hashlib
import os
from pathlib import Path
import re
import secrets
import ssl
import sys
import ipaddress
import threading
from urllib.parse import urlsplit


def data_dir():
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return root / "Brioche"


_identity_lock = threading.RLock()

def identity(folder=None, hosts=None):
    with _identity_lock:
        return _identity(folder, hosts)


def _identity(folder=None, hosts=None):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    folder = Path(folder) if folder else data_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert_path, key_path = folder / "identity.crt", folder / "identity.key"
    if not cert_path.exists() or not key_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Brioche")])
        now = dt.datetime.now(dt.timezone.utc)
        builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True))
        if hosts:
            builder = builder.add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address(host)) for host in hosts]), critical=False)
        cert = builder.sign(key, hashes.SHA256())
        # Private file permissions are applied at creation, not after writing secrets.
        with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
            out.write(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    pin = cert.fingerprint(hashes.SHA256()).hex()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    return ctx, pin


def client_context():
    # CA verification is replaced by a REQUIRED exact SHA256 pin check before auth.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def verify_pin(ssl_object, expected):
    actual = hashlib.sha256(ssl_object.getpeercert(binary_form=True)).hexdigest()
    if not secrets.compare_digest(actual, expected):
        raise ValueError("设备证书不匹配：请向被控端重新核实连接凭据")


def access_uri(host, port, pin, token):
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"brioche://{host}:{port}#{pin}.{token}"


def parse_uri(text):
    if len(text) > 2048:
        raise ValueError("连接凭据过长")
    try:
        url = urlsplit(text.strip())
        host, port = url.hostname, url.port
        pin, token = url.fragment.split(".")
    except ValueError as exc:
        raise ValueError("连接凭据格式错误") from exc
    if (url.scheme != "brioche" or not host or not port or not 1 <= port <= 65535
            or url.username or url.password or url.path or url.query
            or not re.fullmatch(r"[a-f0-9]{64}", pin)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)):
        raise ValueError("请粘贴被控端提供的完整 Brioche 连接凭据")
    return host, port, pin, token
