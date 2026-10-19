"""Paired-only LAN address recovery. Advertisements never establish trust."""
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time

PORT = 8591

def response_valid(body, nonce, device):
    try:
        if body['nonce'] != nonce or body['pin'] != device['pin'] or not 1024 <= int(body['port']) <= 65535:
            return False
        message = f"discover|{nonce}|{body['pin']}|{int(body['port'])}"
        expected = hmac.new(device['token'].encode(),message.encode(),hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected,body['proof'])
    except (KeyError, TypeError, ValueError):
        return False

def discover(device, timeout=2):
    nonce=secrets.token_hex(32)
    request=json.dumps({'protocol':'panelyra-discovery-v1','nonce':nonce}).encode()
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
        sock.settimeout(.25)
        sock.sendto(request,('255.255.255.255',PORT))
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            try:
                raw,address=sock.recvfrom(2048)
                body=json.loads(raw)
                if not ipaddress.ip_address(address[0]).is_private:continue
                if response_valid(body,nonce,device):
                    return f"{address[0]}:{int(body['port'])}"
            except (socket.timeout,ValueError,KeyError):
                continue
    return None
