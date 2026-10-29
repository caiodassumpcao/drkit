"""Sample credentials for tests.

Token-shaped values are assembled at runtime: a literal in the source would be reported by
secret scanners (GitHub secret scanning, Gitleaks) as a leaked credential.
"""

TELEGRAM_TOKEN = ":".join(("123456789", "AAHdqTcvCH1vGWJx" + "fSeofSAs0K5PALDsaw1"))
