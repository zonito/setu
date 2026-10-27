"""Tests for setu-exec v0.8: the Web API cancel route (Class B
orders that never take a socket session id).

Crypto is pinned byte-exact to IBKR's published worked example
("OAuth at Interactive Brokers", sections 7.2 / 7.4 / 7.5, keys
in section 8 — IBKR's own public example material). Everything
else is synthetic: a fake Web API transport/client, fake
Kubera/Gateway doubles in the style of tests/test_cancel_gates.py.
No real network, no real orders.

Run: python3 -m unittest discover tests
"""
import base64
import hashlib
import hmac as hmac_mod
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import setu_exec  # noqa: E402

ACCOUNT = "U19377323"
WEB_PERM = 829711680

# IBKR's published example signing key (oauth.pdf section 8.1).
IBKR_EXAMPLE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQDDgozsh3e7
rl59JI+OPYXJyc0Vlftmm45tTxXQ6PlzYsQifuOAwfK0bc2HPvMlVH/sxYEU
GR6H4K3D9j//uFbm6dg1/4YFTg68tnoReUk6x/kz8gF/8WKB7cVysSQvhWPK
+zGyDXq4w7J4XMpV9gSfeuIqkXchCwvkTVbysGB6C7jwySeiTRL/XhQfNj6i
GnLos60Uz7IuY0zuvJGvJIdgLTnI61MCaz59DzXhk0ATyZR00hfgGvvjNxBN
OUVvTh8wq18NWer207che8iYXF0kHTEO8jabWv9k/6Ol+NBW2YnKD3WcEcda
/sii+CAH+YmXVcoOdtCj9oJKp96voUQrAgMBAAECggEAAYlOGDG8VCI7pdoa
srBzE7HdRbUnA5hnVx6O+lRKWYjGJ8s9eG6NvLM8MTriaEGwu6iVqhXEcqbm
Vqrht923+RqPU2VNpU8GuJ7qeKauV6Lop2rYbiv0bTb0HUGdGi5/KUIuEsse
UsNe7Kk2AdTsFmC91D3OZNkUX9fnvgJ6z5qyc1SvZGbrUiJuOMzXFz7g0/YH
31H1mK72PIpFp5rha1djWzi0Pc99v06HwURIJAy/aB7aaB9oXWKf0xw94+PG
yUUUbfFJ0n3mgx7PQcOEaebu0JsGLBb28AUbughjjD9A9qcIjJUbdwWq6MYY
1JZfvymVuEurAaCLozdEWb0f2QKBgQDyaAdzlZL1SCNv9Jg+sBm8u1fkH1pR
WtZ6iQZLE46H6nwvOL4ZMm2Va/6SK6Jn7vikLHrXhizOanJuvgKjgYKiAknj
X8NGgTfObgh8X92xGch6m9irLLJoPHSBfAegr1Aro/z8rJuqdcBzyiHz7xof
ZZkr/B1WLE96A/g9tVxTLQKBgQDOeUeOuY8B2iYVY0EZQQ4Yriyk/n1fgz3T
JFc8OuKJiir6WnYRSueC5DBsI0ghDQLq4VCG8yAkIUk/w98lGDaGGw+c+Mn1
mAvAbTN7p/Rkv5h1EEJqkWL8EyVbQj3yMlKVXz0fJ05ShD7c1FwV4pasiY5y
Yy6UK1Lkmr3DRhZrtwKBgAm9Jjd9lebm3A2ebRNvrckIWdKfc9h65s0LfkY7
5ekOAalABVsrzH1JVnP27tJwJmsqIlFQYbJ1Ul2ioqaBCIeLwf4xlb3aaSyQ
/SRKEbUQzR4n7r+Jzy0i6KdfHUtWX8kxEHPyV1q02mOMhmqve4QxZ8L9oQwa
QaMsT6fjBm+NAoGAY4LsTAt+syPSqwg1Pdiu1JJNjg/hmQEF8RdYu/ydZjCj
WhnzQY5aWilkdRnkD5nXyCBj0UaoPQGV7heXGqe6z5OHSN5XZ/ip4UpAP97I
2S3GatU1TWtYy6jmnj8k3/AFAVzvdm5ZP0fhjEkFQL7+Y5XAg7ztnBftUaSK
xOyora0CgYACDDuIJjJ7Xym49iIzJcs+w4KSLpDdvXt5yVuJoOhMeLgsA0JQ
dqUbBrEo6tTzdDMeUWuEwqfC4la5myQojkzyjZSFrnhk9ecBYxNwoDPDVL7/
sviL3SEWu87gRfyIW9AtUZgr2iibyL9KXNcRc5D1kaiQj1DbJ5XOsRycWvhh
XQ==
-----END PRIVATE KEY-----
"""

# oauth.pdf section 7.4: the consumer's DH public value A (hex).
IBKR_EXAMPLE_DH_A_HEX = (
    "adcc3e6d1a297418336fd90f41f0b1a1d9b025b35725f6803d6b13309bc3d0"
    "fcfdaeff17306bcafa5d0e91a66ad540254cacae28550e30145df9d7a3847"
    "bb7774b6c53a6f1e5c1aaed51fffb17807c8e2083d93ede25801b41a83dd9"
    "fcce5b3f8cff4200dff23ebf907c6eab820a35fc32133eb09c653d7ceebbad"
    "f14715a3c191a37a442d1063232ddbc7fbc1be855d62b7383e134175e33c19b"
    "9118d6e3213e599664187319b39960efc5eb7e9f0e891d3bc71fd7e0f13f033"
    "0c0edf8f67007e5bf327219569298bea3ebde9c772c2b9461f484ed956e888"
    "c7c545f11a05c02812ef07ea026d0bd69a0b2fe60d7c106e059515a088780e"
    "bd1143b0765bebb")
# The decrypted example access token secret (oauth.pdf 7.3.4/7.4.2).
IBKR_EXAMPLE_SECRET_HEX = (
    "4766f306ad7408bbdaa1950cf4f337101555d0fa42ab904871e2fe57e365b272")


# ---------------------------------------------------------------------------
# DER writer helpers: build synthetic key/parameter PEMs in-test.
# ---------------------------------------------------------------------------

def _der_len(n):
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _der_tlv(tag, body):
    return bytes([tag]) + _der_len(len(body)) + body


def _der_int(x):
    raw = x.to_bytes(max(1, (x.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der_tlv(0x02, raw)


def _der_seq(*parts):
    return _der_tlv(0x30, b"".join(parts))


def _to_pem(label, der):
    b64 = base64.b64encode(der).decode("ascii")
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return (f"-----BEGIN {label}-----\n" + "\n".join(lines)
            + f"\n-----END {label}-----\n")


# Toy RSA key: p=61, q=53 (textbook values, test material only).
TOY = {"p": 61, "q": 53, "n": 3233, "e": 17, "d": 2753,
       "dp": 53, "dq": 49, "qinv": 38}


def toy_pkcs1_pem():
    der = _der_seq(*(_der_int(x) for x in (
        0, TOY["n"], TOY["e"], TOY["d"], TOY["p"], TOY["q"],
        TOY["dp"], TOY["dq"], TOY["qinv"])))
    return _to_pem("RSA PRIVATE KEY", der)


def toy_pkcs8_pem():
    pkcs1 = _der_seq(*(_der_int(x) for x in (
        0, TOY["n"], TOY["e"], TOY["d"], TOY["p"], TOY["q"],
        TOY["dp"], TOY["dq"], TOY["qinv"])))
    alg = _der_seq(bytes.fromhex("06092a864886f70d010101"),
                   bytes.fromhex("0500"))
    der = _der_seq(_der_int(0), alg, _der_tlv(0x04, pkcs1))
    return _to_pem("PRIVATE KEY", der)


def toy_dhparam_pem(prime=23, generator=5):
    return _to_pem("DH PARAMETERS",
                   _der_seq(_der_int(prime), _der_int(generator)))


# ---------------------------------------------------------------------------
# Crypto: IBKR's published vectors, byte-exact.
# ---------------------------------------------------------------------------

class CryptoVectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.n, cls.e, cls.d = setu_exec.parse_rsa_private_key(
            IBKR_EXAMPLE_KEY_PEM)

    def test_example_key_parses(self):
        self.assertEqual(self.n.bit_length(), 2048)
        self.assertEqual(self.e, 65537)

    def test_request_token_signature_section_72(self):
        params = {
            "oauth_callback": "oob",
            "oauth_consumer_key": "TESTCONS",
            "oauth_nonce": "fcbc9c08d69ac269f7f1",
            "oauth_signature_method": "RSA-SHA256",
            "oauth_timestamp": "1473793701",
        }
        base = setu_exec.oauth_signature_base_string(
            "POST",
            "http://localhost:12345/tradingapi/v1/oauth/request_token",
            params)
        self.assertEqual(
            base,
            "POST&http%3A%2F%2Flocalhost%3A12345%2Ftradingapi%2Fv1"
            "%2Foauth%2Frequest_token&oauth_callback%3Doob"
            "%26oauth_consumer_key%3DTESTCONS%26oauth_nonce"
            "%3Dfcbc9c08d69ac269f7f1%26oauth_signature_method"
            "%3DRSA-SHA256%26oauth_timestamp%3D1473793701")
        signature = setu_exec.rsa_sha256_sign(
            base.encode("utf-8"), self.n, self.d)
        self.assertEqual(
            signature,
            "nHpiG+/oGBtXtQPYOEc1PJKVUYCgFzbbUyQ1slb5GtvRVp+nfOKlm4e"
            "Xt7P4DhNecsrg5gEc/AWHD+L7snXFmOWlj6c1XjcSCj1r8ERWHg/f8U"
            "43PwLkVHCarwgh3cVcH0KGioPpShefNcVvriAUxfBiScWKiB/1zmI1"
            "cs9yBHqoJS6pv4KlAGeLj3eXLmYphZGgJzLMLyj6X4UIT1RftHLKbq"
            "wmgfVMoBuDm5EDJDokN2VowepIMnkQBjiUJGWklUcgD8Jq9VHAgzTf"
            "dMsX33Nnfia+Z4ZcdJLUn2uUlNJX3FtWJeY02OImvXoQOZNXN6hiUH"
            "7q158Z/LOqWDyUAA==")

    def test_live_session_token_signature_section_74(self):
        params = {
            "diffie_hellman_challenge": IBKR_EXAMPLE_DH_A_HEX,
            "oauth_consumer_key": "TESTCONS",
            "oauth_nonce": "36f7d85e418f8bfe8561",
            "oauth_signature_method": "RSA-SHA256",
            "oauth_timestamp": "1473793702",
            "oauth_token": "6f531f8fd316915af53f",
        }
        base = setu_exec.oauth_signature_base_string(
            "POST",
            "http://localhost:12345/tradingapi/v1/oauth/"
            "live_session_token",
            params)
        signed = (IBKR_EXAMPLE_SECRET_HEX + base).encode("utf-8")
        signature = setu_exec.rsa_sha256_sign(signed, self.n, self.d)
        # An RSA-2048 signature is exactly 256 bytes / 344 base64
        # chars; the guard keeps a mistyped constant from failing
        # opaquely (this constant was once typed one char long).
        self.assertEqual(len(signature), 344)
        self.assertEqual(
            signature,
            "KzaCo+vE5k1T3aor2/th/IhS1gj1b81cLTPVZYtsHsMYoyn0xY2f7Gk"
            "xnc9i1ZgeM5JFdh5qz32GhDlArbhHodSRxKEGPJ2BqOGlEBsL1RymJY"
            "Y0sujoMg+uw06Ti7pg7h6OWdYi+yskRxbstD4O1Z4ZZXn6s/gBrO60"
            "+V5ZQe7ENsgbg7pvHmzjREC4NSrWLA4MQIbJimzZJ04JChvrSRsTcop"
            "nRdj3OXg9ce2eGFgPW1cbCV4fNoBg51M8BHOxGB10yux9usRBfh6Udz"
            "CaTHSbskhxIrUGLYc04SMh0KNE99dxww2w5vnyGPk2QnnPyRTcNBAM"
            "5UjerB3CqFeBUw==")

    def test_hmac_resource_signature_section_75(self):
        params = {
            "conid": "8314",
            "oauth_consumer_key": "TESTCONS",
            "oauth_nonce": "aecef17086308940e861",
            "oauth_signature_method": "HMAC-SHA256",
            "oauth_timestamp": "1473795686",
            "oauth_token": "6f531f8fd316915af53f",
        }
        base = setu_exec.oauth_signature_base_string(
            "GET",
            "http://localhost:12345/tradingapi/v1/marketdata/snapshot",
            params)
        lst = base64.b64decode("YBWbLw+9RYP2nWrPQHxHZkBb1aM=")
        signature = base64.b64encode(hmac_mod.new(
            lst, base.encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii")
        self.assertEqual(
            signature, "+BdIuZDNooYZAbO9RZUCTC5F/3HjFOb04Tu4crpi0v8=")


class DerParsingTests(unittest.TestCase):
    def test_pkcs1_toy_key(self):
        n, e, d = setu_exec.parse_rsa_private_key(toy_pkcs1_pem())
        self.assertEqual((n, e, d), (TOY["n"], TOY["e"], TOY["d"]))

    def test_pkcs8_toy_key(self):
        n, e, d = setu_exec.parse_rsa_private_key(toy_pkcs8_pem())
        self.assertEqual((n, e, d), (TOY["n"], TOY["e"], TOY["d"]))

    def test_dh_parameters(self):
        prime, generator = setu_exec.parse_dh_parameters(
            toy_dhparam_pem(2305843009213693951, 5))
        self.assertEqual((prime, generator), (2305843009213693951, 5))

    def test_garbage_pem_raises(self):
        with self.assertRaises(setu_exec.WebApiError):
            setu_exec.parse_rsa_private_key("not a pem at all")

    def test_java_bigint_bytes(self):
        self.assertEqual(setu_exec._java_bigint_bytes(0), b"\x00")
        self.assertEqual(setu_exec._java_bigint_bytes(0x7F), b"\x7f")
        self.assertEqual(setu_exec._java_bigint_bytes(0x80), b"\x00\x80")
        self.assertEqual(setu_exec._java_bigint_bytes(0xFF), b"\x00\xff")
        self.assertEqual(setu_exec._java_bigint_bytes(0x0100), b"\x01\x00")


class LiveSessionTokenFlowTests(unittest.TestCase):
    """The full mint + signed-request loop against a fake server
    that independently implements IBKR's side of the DH exchange."""

    PRIME = 2 ** 255 - 19
    SECRET = bytes.fromhex(IBKR_EXAMPLE_SECRET_HEX)

    def _creds(self):
        n, _e, d = setu_exec.parse_rsa_private_key(IBKR_EXAMPLE_KEY_PEM)
        return setu_exec.WebApiCredentials(
            consumer_key="TESTCONS",
            access_token="6f531f8fd316915af53f",
            access_token_secret=self.SECRET,
            rsa_modulus=n, rsa_private_exponent=d,
            dh_prime=self.PRIME, dh_generator=2,
            realm="test_realm")

    def _server(self, sign_correctly=True):
        captured = {}

        def transport(method, url, headers, body):
            captured.setdefault("calls", []).append((method, url))
            if url.endswith("/oauth/live_session_token"):
                auth = headers["Authorization"]
                pairs = dict(
                    part.split("=", 1) for part in
                    auth[len("OAuth "):].split(", "))
                challenge = int(pairs["diffie_hellman_challenge"]
                                .strip('"'), 16)
                b = 424242
                server_public = pow(2, b, self.PRIME)
                shared = pow(challenge, b, self.PRIME)
                lst = hmac_mod.new(
                    setu_exec._java_bigint_bytes(shared), self.SECRET,
                    hashlib.sha1).digest()
                captured["lst"] = lst
                signature = hmac_mod.new(
                    lst, b"TESTCONS", hashlib.sha1).hexdigest()
                if not sign_correctly:
                    signature = "0" * 40
                return 200, {
                    "diffie_hellman_response": format(server_public, "x"),
                    "live_session_token_signature": signature}
            if url.endswith("/iserver/auth/ssodh/init"):
                captured["init_body"] = json.loads(body.decode("utf-8"))
                return 200, {"authenticated": True}
            if url.endswith("/iserver/account/orders"):
                captured["orders_headers"] = headers
                return 200, {"orders": []}
            raise AssertionError(f"unexpected URL {url}")

        return transport, captured

    def test_mint_and_signed_request(self):
        transport, captured = self._server()
        client = setu_exec.WebApiClient(
            self._creds(), transport=transport,
            nonce_source=lambda: "abcdef0123456789abcd",
            dh_random_source=lambda: 987654321)
        lst = client.mint_live_session_token()
        self.assertEqual(lst, captured["lst"])
        init = client.init_brokerage_session()
        self.assertEqual(init, {"authenticated": True})
        self.assertEqual(captured["init_body"],
                         {"publish": True, "compete": True})
        self.assertEqual(client.live_orders(), [])
        # The orders request must be HMAC-signed with the LST:
        # rebuild the base string from the captured header and check.
        auth = captured["orders_headers"]["Authorization"]
        pairs = {}
        for part in auth[len("OAuth "):].split(", "):
            key, _, value = part.partition("=")
            pairs[key] = value.strip('"')
        from urllib.parse import unquote
        params = {k: unquote(v) for k, v in pairs.items()
                  if k != "oauth_signature"}
        params.pop("realm", None)
        base = setu_exec.oauth_signature_base_string(
            "GET", setu_exec.WEB_API_BASE + "/iserver/account/orders",
            params)
        expected = base64.b64encode(hmac_mod.new(
            captured["lst"], base.encode("utf-8"),
            hashlib.sha256).digest()).decode("ascii")
        self.assertEqual(unquote(pairs["oauth_signature"]), expected)

    def test_bad_server_signature_fails_closed(self):
        transport, _ = self._server(sign_correctly=False)
        client = setu_exec.WebApiClient(
            self._creds(), transport=transport,
            nonce_source=lambda: "abcdef0123456789abcd",
            dh_random_source=lambda: 987654321)
        with self.assertRaises(setu_exec.WebApiError):
            client.mint_live_session_token()


class CredentialsLoaderTests(unittest.TestCase):
    def _write_creds(self, tmp, **overrides):
        sig = Path(tmp) / "sig.pem"
        sig.write_text(toy_pkcs8_pem(), encoding="utf-8")
        dh = Path(tmp) / "dh.pem"
        dh.write_text(toy_dhparam_pem(), encoding="utf-8")
        data = {
            "consumer_key": "TESTCONS9",
            "access_token": "token-abc",
            "access_token_secret_hex": "00112233",
            "signature_key_path": str(sig),
            "dhparam_path": str(dh),
        }
        data.update(overrides)
        path = Path(tmp) / f"webapi-{ACCOUNT}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_missing_file_means_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            creds = setu_exec.load_webapi_credentials(
                {"_config_dir": tmp}, ACCOUNT, enforce_permissions=False)
            self.assertIsNone(creds)

    def test_valid_credentials_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_creds(tmp)
            creds = setu_exec.load_webapi_credentials(
                {"_config_dir": tmp}, ACCOUNT, enforce_permissions=False)
            self.assertEqual(creds.consumer_key, "TESTCONS9")
            self.assertEqual(creds.access_token_secret,
                             bytes.fromhex("00112233"))
            self.assertEqual(creds.rsa_modulus, TOY["n"])
            self.assertEqual(creds.dh_prime, 23)
            self.assertEqual(creds.realm, "limited_poa")

    def test_broken_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, f"webapi-{ACCOUNT}.json").write_text(
                "{nope", encoding="utf-8")
            with self.assertRaises(setu_exec.WebApiError):
                setu_exec.load_webapi_credentials(
                    {"_config_dir": tmp}, ACCOUNT,
                    enforce_permissions=False)

    def test_missing_field_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_creds(tmp, access_token="")
            with self.assertRaises(setu_exec.WebApiError):
                setu_exec.load_webapi_credentials(
                    {"_config_dir": tmp}, ACCOUNT,
                    enforce_permissions=False)

    def test_bad_secret_hex_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_creds(tmp, access_token_secret_hex="zzzz")
            with self.assertRaises(setu_exec.WebApiError):
                setu_exec.load_webapi_credentials(
                    {"_config_dir": tmp}, ACCOUNT,
                    enforce_permissions=False)

    def test_group_writable_credentials_refused_when_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_creds(tmp)
            path.chmod(0o664)
            with self.assertRaises(setu_exec.WebApiError):
                setu_exec.load_webapi_credentials(
                    {"_config_dir": tmp}, ACCOUNT,
                    enforce_permissions=True)


# ---------------------------------------------------------------------------
# Identity matching against the Web API live-orders list.
# ---------------------------------------------------------------------------

WEB_ROW = {
    "id": "cancel-row-web",
    "account_id": ACCOUNT,
    "con_id": 111222333,
    "symbol": "IREN",
    "local_symbol": "IREN  261030P00040000",
    "expiry": "2026-10-30",
    "strike": 40.0,
    "option_type": "Put",
    "quantity": 0.0,
    "premium": None,
    "target_limit": None,
    "action": "cancel",
    "target_order_id": str(WEB_PERM),
    "web_order_id": "",
    "target_action": "Buy",
    "target_quantity": 10.0,
    "target_limit_price": 0.60,
    "reason": "orphan_order per coverage feed",
    "source": "orphan_scan",
    "approval_mode": "manual",
}


def web_entry(**over):
    entry = {
        "orderId": WEB_PERM,
        "acctId": ACCOUNT,
        "conid": 111222333,
        "side": "BUY",
        "status": "PreSubmitted",
        "sizeAndFills": "0/10",
        "limitPrice": 0.60,
        "orderDesc": "BUY 10 LMT 0.60",
    }
    entry.update(over)
    return entry


class WebIdentityMatchTests(unittest.TestCase):
    def match(self, orders, row=None):
        return setu_exec.match_web_order(row or dict(WEB_ROW),
                                         orders, ACCOUNT)

    def test_exact_match(self):
        entry, reason = self.match([web_entry()])
        self.assertIsNotNone(entry)
        self.assertIsNone(reason)

    def test_absent_target(self):
        entry, reason = self.match([web_entry(orderId=123)])
        self.assertIsNone(entry)
        self.assertIn("not in the Web API live-orders list", reason)

    def test_duplicate_is_ambiguous(self):
        entry, reason = self.match([web_entry(), web_entry()])
        self.assertIsNone(entry)
        self.assertIn("ambiguous", reason)

    def test_conid_mismatch(self):
        entry, reason = self.match([web_entry(conid=999)])
        self.assertIsNone(entry)
        self.assertIn("mismatch on conid", reason)

    def test_account_mismatch(self):
        entry, reason = self.match([web_entry(acctId="U99999999")])
        self.assertIsNone(entry)
        self.assertIn("mismatch on account", reason)

    def test_side_mismatch(self):
        entry, reason = self.match([web_entry(side="SELL")])
        self.assertIsNone(entry)
        self.assertIn("mismatch on side", reason)

    def test_terminal_status_refuses(self):
        entry, reason = self.match([web_entry(status="Filled")])
        self.assertIsNone(entry)
        self.assertIn("no longer a working order", reason)

    def test_unrecognised_status_refuses(self):
        entry, reason = self.match([web_entry(status="SomethingNew")])
        self.assertIsNone(entry)
        self.assertIn("unrecognised status", reason)

    def test_pending_cancel_is_working(self):
        entry, reason = self.match([web_entry(status="PendingCancel")])
        self.assertIsNotNone(entry)

    def test_partial_fill_refuses(self):
        entry, reason = self.match([web_entry(sizeAndFills="3/10")])
        self.assertIsNone(entry)
        self.assertIn("partially filled", reason)

    def test_quantity_mismatch(self):
        entry, reason = self.match([web_entry(sizeAndFills="0/5")])
        self.assertIsNone(entry)
        self.assertIn("mismatch on quantity", reason)

    def test_quantity_incomplete_refuses(self):
        bare = web_entry()
        del bare["sizeAndFills"]
        entry, reason = self.match([bare])
        self.assertIsNone(entry)
        self.assertIn("identity incomplete", reason)

    def test_limit_from_order_desc(self):
        bare = web_entry()
        del bare["limitPrice"]
        entry, reason = self.match([bare])
        self.assertIsNotNone(entry)

    def test_limit_mismatch(self):
        entry, reason = self.match([web_entry(limitPrice=0.65)])
        self.assertIsNone(entry)
        self.assertIn("mismatch on limit price", reason)

    def test_explicit_web_order_id_wins(self):
        row = dict(WEB_ROW, web_order_id="555",
                   target_order_id="999")
        entry, reason = self.match([web_entry(orderId=555)], row=row)
        self.assertIsNotNone(entry)


# ---------------------------------------------------------------------------
# Executor flows: Class B rows through process_cancel_row.
# ---------------------------------------------------------------------------

def class_b_order(**over):
    order = {
        "con_id": 111222333,
        "symbol": "IREN",
        "expiry": "20261030",
        "strike": 40.0,
        "right": "P",
        "action": "Buy",
        "limit_price": 0.60,
        "order_ref": "",
        "status": "PreSubmitted",
        "perm_id": WEB_PERM,
        "order_id": 0,          # the Class B signature
        "filled": 0.0,
        "remaining": 10.0,
    }
    order.update(over)
    return order


def make_flow_cfg(tmp, mode="simulate"):
    cfg = {
        "_config_dir": tmp,
        "account_id": ACCOUNT,
        "kubera_base": "http://127.0.0.1:9",
        "token_file": str(Path(tmp) / "token"),
        "gateway_host": "127.0.0.1",
        "gateway_port": 4001,
        "exec_client_id": 91,
        "mode": mode,
        "exec_poll_seconds": 30,
    }
    Path(tmp, "token").write_text("test-token", encoding="utf-8")
    if mode == "live":
        Path(tmp, "executor.live").write_text("live\n", encoding="utf-8")
    return cfg


def write_flow_creds(tmp):
    """Synthetic Web API credentials in the executor's config dir
    (toy keys; the API client itself is faked in the flow tests,
    but the loader runs for real)."""
    sig = Path(tmp) / "sig.pem"
    sig.write_text(toy_pkcs8_pem(), encoding="utf-8")
    dh = Path(tmp) / "dh.pem"
    dh.write_text(toy_dhparam_pem(), encoding="utf-8")
    creds_path = Path(tmp, f"webapi-{ACCOUNT}.json")
    creds_path.write_text(json.dumps({
        "consumer_key": "TESTCONS9",
        "access_token": "token-abc",
        "access_token_secret_hex": "00112233",
        "signature_key_path": str(sig),
        "dhparam_path": str(dh),
    }), encoding="utf-8")
    # Live mode refuses group/other-writable credential files;
    # the real files are mode 600 too.
    for path in (sig, dh, creds_path):
        path.chmod(0o600)


class FlowKubera:
    def __init__(self, rows):
        self.rows = rows
        self.events = []

    def get_control(self):
        return {"kill_switch": False}

    def poll_approved(self):
        return list(self.rows)

    def post_event(self, row_id, kind, detail):
        self.events.append((row_id, kind, detail))
        return {}

    def kinds(self):
        return [k for _, k, _ in self.events]

    def details(self, kind):
        return [d for _, k, d in self.events if k == kind]


class FlowGateway:
    def __init__(self, orders=None):
        self.orders = list(orders or [])
        self.cancel_calls = []
        self.bound_cancel_calls = []

    def fetch_positions(self):
        return {}

    def fetch_open_orders(self):
        return list(self.orders)

    def _apply_cancel(self, order):
        self.orders = [o for o in self.orders
                       if o.get("perm_id") != order.get("perm_id")]

    def cancel_order(self, order, on_transmit=None):
        self.cancel_calls.append(order)
        if on_transmit is not None:
            on_transmit()
        self._apply_cancel(order)
        return {"ok": True, "status": "Cancelled"}

    def cancel_bound_order(self, order, on_transmit=None):
        self.bound_cancel_calls.append(order)
        if on_transmit is not None:
            on_transmit()
        self._apply_cancel(order)
        return {"ok": True, "transmitted": True, "status": "Cancelled"}

    def probe_bound_target(self, order):
        return {"visible": True, "order_id": order.get("order_id"),
                "error": None}


class FakeWebClient:
    def __init__(self, orders, on_cancel=None, cancel_error=None,
                 init_error=None, remove_on_cancel=True):
        self.orders = list(orders)
        self.on_cancel = on_cancel
        self.cancel_error = cancel_error
        self.init_error = init_error
        self.remove_on_cancel = remove_on_cancel
        self.cancel_calls = []
        self.init_calls = 0

    def init_brokerage_session(self):
        self.init_calls += 1
        if self.init_error is not None:
            raise setu_exec.WebApiError(self.init_error)
        return {}

    def live_orders(self):
        return list(self.orders)

    def cancel_order(self, account_id, web_order_id):
        self.cancel_calls.append((account_id, web_order_id))
        if self.cancel_error is not None:
            raise setu_exec.WebApiError(self.cancel_error)
        if self.remove_on_cancel:
            self.orders = [o for o in self.orders
                           if str(o.get("orderId")) != str(web_order_id)]
        if self.on_cancel is not None:
            self.on_cancel()
        return {"msg": ["Request was submitted to cancel the order"]}


def make_flow_executor(tmp, mode, gateway, rows=()):
    cfg = make_flow_cfg(tmp, mode=mode)
    kubera = FlowKubera(list(rows))
    tracker = setu_exec.PlacedTracker(cfg)
    executor = setu_exec.Executor(cfg, kubera, gateway, tracker)
    executor._web_verify_attempts = 3
    executor._web_verify_interval = 0
    return executor, kubera


class ExecutorWebFlowTests(unittest.TestCase):
    def test_normalise_row_carries_web_fields(self):
        row = setu_exec.normalise_row(
            dict(WEB_ROW, web_order_id="424242"))
        self.assertEqual(row["web_order_id"], "424242")
        self.assertEqual(row["cancel_route"], "")
        bare = setu_exec.normalise_row({"id": "x"})
        self.assertEqual(bare["web_order_id"], "")

    def test_probe_script_has_no_cancel_path(self):
        source = (Path(__file__).resolve().parent.parent
                  / "webapi_probe.py").read_text(encoding="utf-8")
        self.assertNotIn("cancel_order", source)
        self.assertNotIn('"DELETE"', source)

    def test_simulate_without_credentials_keeps_legacy_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway = FlowGateway([class_b_order()])
            executor, kubera = make_flow_executor(tmp, "simulate",
                                                  gateway)
            executor.process_cancel_row(
                dict(WEB_ROW), {}, gateway.fetch_open_orders())
            would = kubera.details("would_execute")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0]["simulate_gate_failed"])
            self.assertEqual(would[0]["gate"], "target")
            self.assertEqual(
                would[0]["reason"],
                "target order carries no session order id")

    def test_simulate_with_credentials_proves_web_route_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_flow_creds(tmp)
            gateway = FlowGateway([class_b_order()])
            fake_web = FakeWebClient([web_entry()])
            executor, kubera = make_flow_executor(tmp, "simulate",
                                                  gateway)
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake_web):
                executor.process_cancel_row(
                    dict(WEB_ROW), {}, gateway.fetch_open_orders())
            would = kubera.details("would_execute")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0]["route"], "web")
            self.assertEqual(
                would[0]["delete"],
                f"DELETE /v1/api/iserver/account/{ACCOUNT}"
                f"/order/{WEB_PERM}")
            self.assertGreaterEqual(fake_web.init_calls, 1)
            self.assertEqual(fake_web.cancel_calls, [])

    def test_live_without_credentials_fails_with_legacy_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway = FlowGateway([class_b_order()])
            executor, kubera = make_flow_executor(tmp, "live", gateway)
            executor.tracker.cancel_streak = 2
            executor.process_cancel_row(
                dict(WEB_ROW), {}, gateway.fetch_open_orders())
            failed = kubera.details("failed")
            self.assertEqual(len(failed), 1)
            self.assertEqual(
                failed[0]["reason"],
                "target order carries no session order id")
            self.assertEqual(executor.tracker.cancel_streak, 0)
            self.assertEqual(executor.tracker.placements_today(), 0)

    def test_live_web_cancel_executes_and_verifies_both_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_flow_creds(tmp)
            gateway = FlowGateway([class_b_order()])

            def drop_from_socket_feed():
                gateway.orders = []

            fake_web = FakeWebClient([web_entry()],
                                     on_cancel=drop_from_socket_feed)
            executor, kubera = make_flow_executor(tmp, "live", gateway)
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake_web):
                executor.process_cancel_row(
                    dict(WEB_ROW), {}, gateway.fetch_open_orders())
            self.assertEqual(fake_web.cancel_calls,
                             [(ACCOUNT, str(WEB_PERM))])
            executed = kubera.details("executed")
            self.assertEqual(len(executed), 1)
            self.assertEqual(executed[0]["route"], "web")
            self.assertEqual(executed[0]["web_order_id"], str(WEB_PERM))
            self.assertTrue(executed[0]["verified_absent"])
            self.assertTrue(executed[0]["verified_web_absent"])
            self.assertEqual(executor.tracker.cancel_streak, 1)
            self.assertEqual(executor.tracker.placements_today(), 1)
            self.assertEqual(executor.tracker.cancels, {})
            self.assertEqual(gateway.cancel_calls, [])
            self.assertEqual(gateway.bound_cancel_calls, [])

    def test_live_web_identity_mismatch_refuses_without_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_flow_creds(tmp)
            gateway = FlowGateway([class_b_order()])
            fake_web = FakeWebClient([web_entry(conid=999)])
            executor, kubera = make_flow_executor(tmp, "live", gateway)
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake_web):
                executor.process_cancel_row(
                    dict(WEB_ROW), {}, gateway.fetch_open_orders())
            refused = kubera.details("gate_failed")
            self.assertEqual(len(refused), 1)
            self.assertEqual(refused[0]["gate"], "web_identity")
            self.assertEqual(fake_web.cancel_calls, [])
            self.assertEqual(executor.tracker.placements_today(), 0)
            self.assertEqual(executor.tracker.cancels, {})

    def test_live_web_session_failure_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_flow_creds(tmp)
            gateway = FlowGateway([class_b_order()])
            fake_web = FakeWebClient([], init_error="ssodh refused")
            executor, kubera = make_flow_executor(tmp, "live", gateway)
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake_web):
                executor.process_cancel_row(
                    dict(WEB_ROW), {}, gateway.fetch_open_orders())
            refused = kubera.details("gate_failed")
            self.assertEqual(len(refused), 1)
            self.assertEqual(refused[0]["gate"], "web_session")
            self.assertEqual(fake_web.cancel_calls, [])

    def test_live_web_delete_error_fails_and_resets_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_flow_creds(tmp)
            gateway = FlowGateway([class_b_order()])
            fake_web = FakeWebClient([web_entry()], cancel_error="boom")
            executor, kubera = make_flow_executor(tmp, "live", gateway)
            executor.tracker.cancel_streak = 3
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake_web):
                executor.process_cancel_row(
                    dict(WEB_ROW), {}, gateway.fetch_open_orders())
            failed = kubera.details("failed")
            self.assertEqual(len(failed), 1)
            self.assertIn("Web API cancel failed: boom",
                          failed[0]["reason"])
            self.assertEqual(executor.tracker.cancel_streak, 0)
            # The DELETE was sent, so the brake counts it.
            self.assertEqual(executor.tracker.placements_today(), 1)
            self.assertEqual(executor.tracker.cancels, {})

    def test_live_web_unverified_stays_tracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_flow_creds(tmp)
            gateway = FlowGateway([class_b_order()])
            fake_web = FakeWebClient([web_entry()],
                                     remove_on_cancel=False)
            executor, kubera = make_flow_executor(tmp, "live", gateway)
            executor.tracker.cancel_streak = 2
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake_web):
                executor.process_cancel_row(
                    dict(WEB_ROW), {}, gateway.fetch_open_orders())
            self.assertNotIn("executed", kubera.kinds())
            self.assertNotIn("failed", kubera.kinds())
            record = executor.tracker.cancels.get(WEB_ROW["id"])
            self.assertIsNotNone(record)
            self.assertTrue(record["transmitted"])
            self.assertEqual(record["route"], "web")
            self.assertEqual(executor.tracker.cancel_streak, 2)

    def test_live_broken_credentials_refuse_at_web_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, f"webapi-{ACCOUNT}.json").write_text(
                "{broken", encoding="utf-8")
            gateway = FlowGateway([class_b_order()])
            executor, kubera = make_flow_executor(tmp, "live", gateway)
            executor.process_cancel_row(
                dict(WEB_ROW), {}, gateway.fetch_open_orders())
            refused = kubera.details("gate_failed")
            self.assertEqual(len(refused), 1)
            self.assertEqual(refused[0]["gate"], "web_credentials")
            self.assertNotIn("failed", kubera.kinds())

    def test_socket_row_never_builds_web_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_flow_creds(tmp)
            gateway = FlowGateway([class_b_order(order_id=8811)])
            executor, kubera = make_flow_executor(tmp, "live", gateway)
            with mock.patch.object(
                    setu_exec, "build_webapi_client") as builder:
                executor.process_cancel_row(
                    dict(WEB_ROW), {}, gateway.fetch_open_orders())
            builder.assert_not_called()
            self.assertEqual(len(gateway.cancel_calls), 1)
            self.assertIn("executed", kubera.kinds())

    def test_brake_defers_second_class_b_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_flow_creds(tmp)
            second = class_b_order(perm_id=WEB_PERM + 1, con_id=111222334,
                                   strike=41.0, limit_price=0.55)
            gateway = FlowGateway([class_b_order(), second])

            def drop_first_from_socket_feed():
                gateway.orders = [
                    o for o in gateway.orders
                    if o.get("perm_id") != WEB_PERM]

            fake_web = FakeWebClient(
                [web_entry(),
                 web_entry(orderId=WEB_PERM + 1, conid=111222334,
                           limitPrice=0.55)],
                on_cancel=drop_first_from_socket_feed)
            row2 = dict(WEB_ROW, id="cancel-row-web-2",
                        con_id=111222334, strike=41.0,
                        target_order_id=str(WEB_PERM + 1),
                        target_limit_price=0.55)
            executor, kubera = make_flow_executor(tmp, "live", gateway)
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake_web):
                executor.process_cancel_row(
                    dict(WEB_ROW), {}, gateway.fetch_open_orders())
                executor.process_cancel_row(
                    row2, {}, gateway.fetch_open_orders())
            self.assertEqual(len(fake_web.cancel_calls), 1)
            deferred = [d for _, k, d in kubera.events
                        if k == "would_execute" and d.get("deferred")]
            self.assertEqual(len(deferred), 1)
            self.assertEqual(deferred[0]["gate"], "runaway_brake")


if __name__ == "__main__":
    unittest.main()
