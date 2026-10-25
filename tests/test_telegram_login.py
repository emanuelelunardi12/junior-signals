import hashlib
import hmac
import time

from app import config, telegram


def sign(data, token):
    s = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    return hmac.new(hashlib.sha256(token.encode()).digest(), s.encode(), hashlib.sha256).hexdigest()


def test_valid_login(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
    data = {"id": "42", "first_name": "Ana", "auth_date": str(int(time.time()))}
    assert telegram.verify_login({**data, "hash": sign(data, "123:abc")})["id"] == "42"


def test_tampered_login_rejected(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
    data = {"id": "42", "first_name": "Ana", "auth_date": str(int(time.time()))}
    h = sign(data, "123:abc")
    assert telegram.verify_login({**data, "id": "43", "hash": h}) is None


def test_expired_login_rejected(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
    data = {"id": "42", "auth_date": str(int(time.time()) - 200000)}
    assert telegram.verify_login({**data, "hash": sign(data, "123:abc")}) is None
