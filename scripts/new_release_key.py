"""Makes the key that signs each release's SHA256SUMS, for the apps' updater to check (desktop/updater.py).

    python scripts/new_release_key.py

Run it once. The private key goes into the RELEASE_SIGNING_KEY secret of the repository's `release` environment, and the
public key into RELEASE_PUBLIC_KEY in app/services/updates.py. Add the secret first: from then on the release workflow
signs, and it refuses to publish without a signature once the public key is in the code. Apps with the public key built in
won't install an unsigned release, and there's no way to change the key in apps already out there without the old key's
signature on the release that brings the new one, so keep the private key safe.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print("RELEASE_SIGNING_KEY (a secret in the `release` environment):\n")
    print(private.decode())
    print(f'In app/services/updates.py:\n\nRELEASE_PUBLIC_KEY = "{base64.b64encode(public).decode()}"')


if __name__ == "__main__":
    main()
