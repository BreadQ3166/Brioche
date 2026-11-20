"""Collect Qt's own dependencies without optional build-machine OpenSSL.

Brioche uses QtNetwork only for interface enumeration. Its authenticated TLS
connections use Python ssl, and tsnet uses Go TLS. Both are collected separately.
Qt's native SecureTransport provider remains part of the standard Qt dependencies.
"""
from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
