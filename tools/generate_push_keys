"""Run locally/Render shell; copy output into private environment settings, never GitHub."""
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
key=ec.generate_private_key(ec.SECP256R1())
private=key.private_bytes(serialization.Encoding.DER,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
public=key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
print('VAPID_PRIVATE_KEY='+base64.urlsafe_b64encode(private).decode().rstrip('='))
print('VAPID_PUBLIC_KEY='+base64.urlsafe_b64encode(public).decode().rstrip('='))
print('Keep the private key secret. Set VAPID_CONTACT=mailto:YOUR_SUPPORT_ADDRESS separately.')
