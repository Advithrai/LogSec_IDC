#!/usr/bin/env python3
import os
import sys
import hashlib
import datetime
import subprocess
import smtplib
from email.mime.text import MIMEText
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding

# ---------------- CONFIG ----------------
PROJECT_DIR = os.path.expanduser("~/log_project")
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
CHAIN_FILE = os.path.join(PROJECT_DIR, "chain.txt")
KEY_FILE = os.path.join(PROJECT_DIR, "secret.key")
PRIVATE_KEY_FILE = os.path.join(PROJECT_DIR, "private_key.pem")
PUBLIC_KEY_FILE = os.path.join(PROJECT_DIR, "public_key.pem")
POST_HOOK = os.path.join(PROJECT_DIR, "sign_and_push.sh")

os.makedirs(LOG_DIR, exist_ok=True)

def load_dotenv(path=os.path.join(PROJECT_DIR, ".env")):
    """Load KEY=VALUE lines from an optional .env file (never commit it)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_dotenv()

# Defined after load_dotenv() so LOGSEC_SOURCE_LOG in .env is picked up.
LOG_FILE = os.path.expanduser(os.environ.get("LOGSEC_SOURCE_LOG", os.path.join(PROJECT_DIR, "source.log")))

# ---------------- EMAIL ALERT ----------------
def send_email_alert(message):
    # Credentials come from the environment (or ~/log_project/.env), never the source code.
    sender = os.environ.get("LOGSEC_EMAIL_SENDER")
    password = os.environ.get("LOGSEC_EMAIL_PASSWORD")  # Gmail App Password
    receiver = os.environ.get("LOGSEC_EMAIL_RECEIVER", sender)

    # Treat the example/placeholder values as "not set".
    placeholder_senders = {"your-gmail@gmail.com", "you@gmail.com"}
    placeholder_passwords = {"abcdefghijklmnop", "xxxx-xxxx-xxxx-xxxx"}
    if sender in placeholder_senders:
        sender = None
    if password in placeholder_passwords:
        password = None

    if not sender or not password:
        print("[-] Email alert skipped: set LOGSEC_EMAIL_SENDER and LOGSEC_EMAIL_PASSWORD.")
        print("[!] ALERT:", message)
        return

    msg = MIMEText(message)
    msg["Subject"] = "⚠ Log Tampering Detected!"
    msg["From"] = sender
    msg["To"] = receiver

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, [receiver], msg.as_string())
        print("[+] Email alert sent successfully!")
    except Exception as e:
        print("[-] Failed to send email:", e)

# ---------------- KEY MANAGEMENT ----------------
def generate_fernet_key():
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    os.chmod(KEY_FILE, 0o600)
    return key

def load_fernet_key():
    if not os.path.exists(KEY_FILE):
        return generate_fernet_key()
    return open(KEY_FILE, "rb").read()

def generate_rsa_keys():
    if os.path.exists(PRIVATE_KEY_FILE) and os.path.exists(PUBLIC_KEY_FILE):
        return
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    # Save private key
    with open(PRIVATE_KEY_FILE, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))
    # Save public key
    with open(PUBLIC_KEY_FILE, "wb") as f:
        f.write(public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ))
    os.chmod(PRIVATE_KEY_FILE, 0o600)
    os.chmod(PUBLIC_KEY_FILE, 0o644)
    print("[*] RSA key pair generated.")

def load_rsa_keys():
    generate_rsa_keys()
    with open(PRIVATE_KEY_FILE, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    with open(PUBLIC_KEY_FILE, "rb") as f:
        public_key = serialization.load_pem_public_key(f.read())
    return private_key, public_key

# ---------------- FERNET ENCRYPTION ----------------
def encrypt_logfile(input_path, output_path):
    key = load_fernet_key()
    f = Fernet(key)
    with open(input_path, "rb") as f_in:
        data = f_in.read()
    enc_data = f.encrypt(data)
    with open(output_path, "wb") as f_out:
        f_out.write(enc_data)
    print(f"[+] Encrypted -> {output_path}")

def decrypt_logfile(input_path, output_path):
    key = load_fernet_key()
    f = Fernet(key)
    with open(input_path, "rb") as f_in:
        enc_data = f_in.read()
    data = f.decrypt(enc_data)
    with open(output_path, "wb") as f_out:
        f_out.write(data)
    print(f"[+] Decrypted -> {output_path}")

# ---------------- HASHING ----------------
def hash_file(path):
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4096), b""):
            sha.update(block)
    return sha.hexdigest()

# ---------------- RSA SIGNING ----------------
def sign_hash(private_key, hash_str):
    return private_key.sign(
        hash_str.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )

def verify_signature(public_key, hash_str, signature):
    try:
        public_key.verify(
            signature,
            hash_str.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256()
        )
        return True
    except Exception:
        return False

# ---------------- LOG SNAPSHOT ----------------
def collect_and_encrypt_snapshot():
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    plain_path = os.path.join(LOG_DIR, f"log_{ts}.txt")
    enc_path = plain_path + ".enc"

    if not os.path.exists(LOG_FILE):
        print(f"[!] Source log not found: {LOG_FILE}. Set LOGSEC_SOURCE_LOG in .env or create the file.")
        sys.exit(1)

    # Copy log content
    with open(LOG_FILE, "rb") as src, open(plain_path, "wb") as dst:
        dst.write(src.read())

    encrypt_logfile(plain_path, enc_path)
    os.remove(plain_path)
    return enc_path

# ---------------- CHAIN UPDATE ----------------
def update_chain(enc_file, private_key):
    prev = "GENESIS"
    if os.path.exists(CHAIN_FILE) and os.path.getsize(CHAIN_FILE) > 0:
        with open(CHAIN_FILE, "r") as c:
            for line in reversed(c.readlines()):
                line = line.strip()
                if line and " : " in line:
                    prev = line.split(" : ")[-2]
                    break

    cur_hash = hash_file(enc_file)
    chain_hash = hashlib.sha256((cur_hash + prev).encode()).hexdigest()
    signature = sign_hash(private_key, cur_hash)

    with open(CHAIN_FILE, "a") as c:
        c.write(f"{enc_file} : {chain_hash} : {signature.hex()}\n")

    print(f"[+] Chain updated -> {chain_hash}")
    return chain_hash

# ---------------- CHAIN VALIDATION ----------------
def validate_chain(public_key):
    if not os.path.exists(CHAIN_FILE):
        print("[!] No chain file found.")
        return False

    with open(CHAIN_FILE, "r") as c:
        lines = [ln.strip() for ln in c.readlines() if ln.strip()]

    if not lines:
        print("[!] chain.txt empty")
        return False

    prev_chain = "GENESIS"  # the first entry is chained to GENESIS, so it gets verified too
    for line in lines:
        try:
            cur_log, cur_chain, cur_signature_hex = line.split(" : ")
            signature_bytes = bytes.fromhex(cur_signature_hex)
        except ValueError:
            send_email_alert(f"[!] Invalid chain line: {line}")
            return False

        if not os.path.exists(cur_log):
            send_email_alert(f"⚠ Log snapshot missing (deleted?): {cur_log}")
            print(f"[!] ⚠ Log snapshot missing -> {cur_log}")
            return False

        file_hash = hash_file(cur_log)
        recalculated = hashlib.sha256((file_hash + prev_chain).encode()).hexdigest()
        if recalculated != cur_chain or not verify_signature(public_key, file_hash, signature_bytes):
            send_email_alert(f"⚠ Tampering or signature invalid: {cur_log}")
            print(f"[!] ⚠ Tampering or signature invalid -> {cur_log}")
            return False

        prev_chain = cur_chain

    print("[+] All logs verified OK with valid RSA signatures.")
    return True

# ---------------- OPTIONAL POST HOOK ----------------
def run_post_hook():
    if os.path.exists(POST_HOOK) and os.access(POST_HOOK, os.X_OK):
        try:
            subprocess.run([POST_HOOK], check=True)
        except subprocess.CalledProcessError as e:
            print("[!] Post hook failed:", e)

# ---------------- MAIN ----------------
if  __name__ == "__main__":
    private_key, public_key = load_rsa_keys()

    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        validate_chain(public_key)
        sys.exit(0)

    enc_file = collect_and_encrypt_snapshot()
    update_chain(enc_file, private_key)
    validate_chain(public_key)
    run_post_hook()
