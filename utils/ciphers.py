import base64
import binascii
from typing import Optional

# Shadowsocks 常见非标准别名 -> Clash / Mihomo 官方标准名称映射表
SS_CIPHER_ALIASES: dict[str, str] = {
    # ChaCha20-Poly1305 变体（常见漏写 ietf）
    'chacha20-poly1305': 'chacha20-ietf-poly1305',
    'chacha20-ietf-poly-1305': 'chacha20-ietf-poly1305',
    'xchacha20-poly1305': 'xchacha20-ietf-poly1305',
    'xchacha20-ietf-poly-1305': 'xchacha20-ietf-poly1305',

    # AES-GCM 常见缺少连字符或别名
    'aes128-gcm': 'aes-128-gcm',
    'aes192-gcm': 'aes-192-gcm',
    'aes256-gcm': 'aes-256-gcm',

    # 常见流加密别名
    'rc4md5': 'rc4-md5',
    'rc4_md5': 'rc4-md5',
}

# Clash / Mihomo 内核支持的合法 Shadowsocks 加密算法集合
VALID_SS_CIPHERS: set[str] = {
    # AEAD Ciphers
    'aes-128-gcm',
    'aes-192-gcm',
    'aes-256-gcm',
    'chacha20-ietf-poly1305',
    'xchacha20-ietf-poly1305',

    # Shadowsocks 2022
    '2022-blake3-aes-128-gcm',
    '2022-blake3-aes-256-gcm',
    '2022-blake3-chacha20-poly1305',

    # Stream Ciphers (Legacy)
    'aes-128-cfb',
    'aes-192-cfb',
    'aes-256-cfb',
    'aes-128-ctr',
    'aes-192-ctr',
    'aes-256-ctr',
    'rc4-md5',
    'chacha20-ietf',
    'chacha20',
    'xchacha20',
}


def normalize_ss_cipher(cipher: Optional[str]) -> str:
    """
    归一化 Shadowsocks 加密算法名称。
    去除首尾空格、转换为小写，并将常见非标准别名（如 chacha20-poly1305）映射为标准名称。
    """
    if not cipher:
        return ''
    c = str(cipher).strip().lower()
    return SS_CIPHER_ALIASES.get(c, c)


def is_valid_ss_cipher(cipher: Optional[str]) -> bool:
    """
    校验加密算法是否为 Clash / Mihomo 支持的合法 Shadowsocks 加密方式。
    """
    if not cipher:
        return False
    norm = normalize_ss_cipher(cipher)
    return norm in VALID_SS_CIPHERS


def is_valid_ss_password(cipher: Optional[str], password: Optional[str]) -> bool:
    """
    校验 SS 密码是否与加密算法兼容。

    Shadowsocks 2022 (2022-blake3-*) 的密码必须是合法的 base64 编码，
    且解码后长度必须为 32 字节 (aes-256-gcm / chacha20-poly1305) 或
    16 字节 (aes-128-gcm)。这类节点若密钥非法，Mihomo 内核会在加载配置时
    直接报错并导致整份订阅不可用，因此必须在生成阶段剔除。
    传统 AEAD / 流加密 Cipher 的密码可以是任意字符串，不做限制。
    """
    c = normalize_ss_cipher(cipher)
    if not c.startswith('2022-blake3'):
        return True
    if not password:
        return False
    pwd = str(password).strip()
    try:
        # Clash 要求标准 base64；补齐 padding 后严格解码
        key = base64.b64decode(pwd + '=' * (-len(pwd) % 4), validate=True)
    except (binascii.Error, ValueError):
        return False
    expected = 16 if c == '2022-blake3-aes-128-gcm' else 32
    return len(key) == expected
