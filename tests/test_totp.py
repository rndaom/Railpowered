"""Optional second sign-in factor: RFC 6238 time-based one-time codes."""

from __future__ import annotations

import base64
import unittest

from mc_host import auth

SHA1_KEY = b"12345678901234567890"
SHA256_KEY = b"12345678901234567890123456789012"
SHA512_KEY = b"1234567890123456789012345678901234567890123456789012345678901234"

# RFC 6238 Appendix B: (time, SHA-1, SHA-256, SHA-512), 8 digits, 30 s steps.
RFC6238_VECTORS = [
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
]
SECRET = base64.b32encode(SHA1_KEY).decode()  # GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ


class TotpAlgorithmTests(unittest.TestCase):
    def test_rfc6238_vectors(self):
        for at, sha1, sha256, sha512 in RFC6238_VECTORS:
            with self.subTest(at=at):
                self.assertEqual(auth.totp(SHA1_KEY, at, digits=8), sha1)
                self.assertEqual(auth.totp(SHA256_KEY, at, digits=8, digest="sha256"), sha256)
                self.assertEqual(auth.totp(SHA512_KEY, at, digits=8, digest="sha512"), sha512)

    def test_six_digit_codes_are_the_low_digits(self):
        self.assertEqual(auth.totp(SHA1_KEY, 59), "287082")
        self.assertEqual(auth.totp(SHA1_KEY, 1111111109), "081804")

    def test_secret_parsing(self):
        self.assertEqual(auth.parse_totp_secret(SECRET), SHA1_KEY)
        spaced = " ".join(SECRET[i:i + 4] for i in range(0, len(SECRET), 4)).lower()
        self.assertEqual(auth.parse_totp_secret(spaced), SHA1_KEY)
        self.assertEqual(auth.parse_totp_secret(SECRET.rstrip("=") + "===="), SHA1_KEY)
        self.assertEqual(len(auth.parse_totp_secret("JBSWY3DPEHPK3PXP")), 10)
        for bad in ("", "not base32!", "JBSWY3DP", "18" * 10):
            self.assertIsNone(auth.parse_totp_secret(bad), bad)


class TotpCheckerTests(unittest.TestCase):
    def checker(self, secret: str = SECRET, now: float = 1111111109) -> auth.TotpChecker:
        self.now = now
        return auth.TotpChecker(secret_source=lambda: secret, clock=lambda: self.now)

    def test_off_without_a_secret(self):
        self.assertFalse(self.checker("").enabled())
        self.assertFalse(self.checker("${{secret(32)}}").enabled())

    def test_current_and_neighbouring_codes_are_accepted_once(self):
        checker = self.checker()
        self.assertTrue(checker.enabled() and checker.valid_secret())
        self.assertTrue(checker.check("081804"))
        self.assertFalse(checker.check("081804"))  # no replay
        # The next step's code (a phone clock a little ahead) still works.
        self.assertTrue(checker.check(auth.totp(SHA1_KEY, self.now + 30)))
        # A code from before one already used can't be used either.
        self.assertFalse(checker.check(auth.totp(SHA1_KEY, self.now - 30)))

    def test_previous_step_is_accepted_for_a_slow_typist(self):
        checker = self.checker()
        self.assertTrue(checker.check(" 081 804 "))  # pasted with spaces
        checker = self.checker(now=1111111109 + 30)
        self.assertTrue(checker.check("081804"))

    def test_codes_outside_the_window_or_malformed_are_refused(self):
        checker = self.checker()
        for code in (auth.totp(SHA1_KEY, self.now + 90), auth.totp(SHA1_KEY, self.now - 90),
                     "", "12345", "1234567", "abcdef", "07081804"):
            self.assertFalse(checker.check(code), code)

    def test_an_invalid_secret_fails_closed(self):
        checker = self.checker("JBSWY3DP")
        self.assertTrue(checker.enabled())
        self.assertFalse(checker.valid_secret())
        self.assertFalse(checker.check(auth.totp(b"Hello!\xde\xad", self.now)))


if __name__ == "__main__":
    unittest.main()
