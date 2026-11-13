"""
Fresh Trade Engine - Bound HTTP Client Module
Provides requests.Session and HTTP helper functions with socket source address binding (outbound/static IP).
Supports:
- Direct local IP binding (when IP is bound to local network interface or configured in Linux ip addr)
- AWS EC2 Elastic IP -> Local Private IP automatic resolution via Instance Metadata Service (IMDS)
- Graceful fallback with clear diagnostic logging if binding cannot be assigned locally
"""

import socket
import logging
from typing import Optional, Dict, Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

logger = logging.getLogger("BoundHTTP")

# In-memory resolution cache: target_ip -> resolved_local_bind_ip (or None)
_RESOLVED_IP_CACHE: Dict[str, Optional[str]] = {}


class SourceAddressAdapter(HTTPAdapter):
    """Custom requests HTTPAdapter that binds the client socket to a specific source IP."""
    def __init__(self, source_address: str, **kwargs):
        self.source_address = source_address
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["source_address"] = (self.source_address, 0)
        return super().init_poolmanager(*args, **kwargs)


def can_bind_ip(ip: str) -> bool:
    """Tests if a given IP address can be bound locally by creating a temporary TCP socket."""
    if not ip or ip.lower() in ("default", "none", "auto", "0.0.0.0"):
        return False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((ip, 0))
        s.close()
        return True
    except Exception:
        return False


def _query_aws_ec2_imds(target_ip: str) -> Optional[str]:
    """
    On AWS EC2 instances, queries Instance Metadata Service (IMDS) to find which
    local private IP address corresponds to the public Elastic IP (target_ip).
    """
    try:
        # 1. Try IMDSv2 token first (1 second timeout)
        token = None
        try:
            token_resp = requests.put(
                "http://169.254.169.254/latest/api/token",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
                timeout=1.0
            )
            if token_resp.status_code == 200:
                token = token_resp.text.strip()
        except Exception:
            token = None

        headers = {"X-aws-ec2-metadata-token": token} if token else {}

        # 2. Query network interfaces
        macs_resp = requests.get(
            "http://169.254.169.254/latest/meta-data/network/interfaces/macs/",
            headers=headers,
            timeout=1.5
        )
        if macs_resp.status_code != 200:
            return None

        mac_list = [m.strip().rstrip("/") for m in macs_resp.text.splitlines() if m.strip()]

        for mac in mac_list:
            # Check ipv4-associations for this public target_ip
            assoc_url = f"http://169.254.169.254/latest/meta-data/network/interfaces/macs/{mac}/ipv4-associations/{target_ip}"
            try:
                assoc_resp = requests.get(assoc_url, headers=headers, timeout=1.0)
                if assoc_resp.status_code == 200:
                    local_private_ip = assoc_resp.text.strip()
                    if local_private_ip and can_bind_ip(local_private_ip):
                        print(f"[BoundHTTP] AWS IMDS mapped Elastic IP {target_ip} -> local private IP {local_private_ip}")
                        return local_private_ip
            except Exception:
                pass

            # Also check public-ipv4s list
            try:
                pub_url = f"http://169.254.169.254/latest/meta-data/network/interfaces/macs/{mac}/public-ipv4s"
                pub_resp = requests.get(pub_url, headers=headers, timeout=1.0)
                if pub_resp.status_code == 200 and target_ip in pub_resp.text:
                    # Fetch local-ipv4s for this MAC
                    loc_url = f"http://169.254.169.254/latest/meta-data/network/interfaces/macs/{mac}/local-ipv4s"
                    loc_resp = requests.get(loc_url, headers=headers, timeout=1.0)
                    if loc_resp.status_code == 200:
                        candidate_private = loc_resp.text.splitlines()[0].strip()
                        if can_bind_ip(candidate_private):
                            print(f"[BoundHTTP] AWS IMDS mapped public IP {target_ip} -> local IP {candidate_private}")
                            return candidate_private
            except Exception:
                pass

    except Exception as e:
        logger.debug(f"[BoundHTTP] IMDS lookup skipped: {e}")

    return None


def resolve_source_ip(target_ip: Optional[str]) -> Optional[str]:
    """
    Resolves the actual local IP address to bind to:
    1. If target_ip can be directly bound (e.g., local alias or private IP), returns it.
    2. If it's a public Elastic IP on AWS EC2, queries IMDS to find the mapped local private IP.
    3. If neither works, returns None with a logged warning so requests fall back to default route.
    """
    if not target_ip:
        return None

    clean_ip = str(target_ip).strip()
    if not clean_ip or clean_ip.lower() in ("default", "none", "auto", "0.0.0.0"):
        return None

    if clean_ip in _RESOLVED_IP_CACHE:
        return _RESOLVED_IP_CACHE[clean_ip]

    # Test 1: Can we bind to clean_ip directly?
    if can_bind_ip(clean_ip):
        _RESOLVED_IP_CACHE[clean_ip] = clean_ip
        return clean_ip

    # Test 2: Try AWS EC2 Instance Metadata mapping
    resolved = _query_aws_ec2_imds(clean_ip)
    if resolved:
        _RESOLVED_IP_CACHE[clean_ip] = resolved
        return resolved

    # Fallback: IP cannot be bound locally
    print(
        f"[BoundHTTP] Notice: Outbound IP '{clean_ip}' cannot be bound directly to local network socket. "
        f"Using standard network routing.",
        flush=True
    )
    _RESOLVED_IP_CACHE[clean_ip] = None
    return None


def get_bound_requests_session(static_ip: Optional[str] = None) -> requests.Session:
    """
    Creates and returns a requests.Session. If static_ip is provided and resolvable,
    mounts SourceAddressAdapter on http:// and https:// so that every socket is bound
    to that outbound IP address.
    """
    session = requests.Session()
    bound_ip = resolve_source_ip(static_ip)
    if bound_ip:
        adapter = SourceAddressAdapter(
            source_address=bound_ip,
            pool_connections=20,
            pool_maxsize=50,
            max_retries=1
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    return session
