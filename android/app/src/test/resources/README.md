# Test-only TLS identity

`receive-test.p12` is a deliberately public, throwaway certificate and private key
used by the loopback receiver unit tests. Its password is in the test source. It
is not an app signing key, production certificate, pairing identity, or secret.
Never use this fixture outside isolated tests. Real device identities and signing
keys must remain private and must never be committed.
