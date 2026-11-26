# Migrate MIME message parsing from go-mime to go-message

## Background

The `mime` package of GopenPGP V3 (`github.com/ProtonMail/gopenpgp/v3/mime`) decrypts PGP/MIME messages and reports the decoded body, attachments, and signature status through the `MIMECallbacks` interface. It currently uses the `github.com/ProtonMail/go-mime` fork (`v0.0.0-20230322103455-7d82a3887f2f`) for MIME part traversal, transfer-encoding and charset decoding, and attachment collection.

## Goal

- Migrate the MIME handling in `mime/mime.go` and `mime/signature_collector.go` from `go-mime` to `go-message` (`github.com/emersion/go-message`) `v0.18.0`.
- `go-message` MUST provide the MIME part traversal, transfer-encoding and charset decoding, body and attachment collection, and `multipart/signed` extraction that `go-mime` provides today.
- `go.mod` MUST require `go-message` `v0.18.0` and MUST NOT require `github.com/ProtonMail/go-mime` after the migration.
- The `crypto` package's encryption, decryption, signing, verification, and key APIs, the `mobile` gomobile bindings, and the public contracts of `armor`, `constants`, and `profile` are out of scope.

## Public contract to preserve

- `mime.Decrypt(message []byte, messageEncoding int8, decryptionHandle crypto.PGPDecryption, verifyHandle crypto.PGPVerify, callbacks MIMECallbacks)` MUST keep this signature. `messageEncoding` is `crypto.Bytes` or `crypto.Armor`; `verifyHandle` may be nil.
- The `MIMECallbacks` interface MUST keep these methods: `OnBody(body string, mimetype string)`, `OnAttachment(headers string, data []byte)`, `OnEncryptedHeaders(headers string)`, `OnVerified(verified int)`, and `OnError(err error)`.
- For a message that decrypts and parses successfully, callbacks MUST be invoked in this order: signature status first (`OnVerified` and/or `OnError`), then `OnBody`, then one `OnAttachment` per attachment in MIME order, then a final `OnEncryptedHeaders("")`.
- `OnVerified` values MUST be the `constants` signature statuses: `SIGNATURE_OK` (0), `SIGNATURE_NOT_SIGNED` (1), `SIGNATURE_NO_VERIFIER` (2), `SIGNATURE_FAILED` (3), or `SIGNATURE_BAD_CONTEXT` (4).
- Signature failures passed to `OnError` MUST be errors that `errors.As` resolves to a `crypto.SignatureVerificationError` carrying the corresponding `Status`.

## Behavior requirements

- If `decryptionHandle.Decrypt` returns an error, `Decrypt` MUST call `OnError` with that error and MUST NOT invoke any other callback.
- If the decrypted content cannot be read as a MIME message, `Decrypt` MUST call `OnError` with an error whose message is prefixed with the literal `mime: `.
- When a `multipart/signed` part is parsed, the signed content MUST be normalized by trimming trailing spaces, tabs, and carriage returns from each line and then canonicalizing line endings to CRLF, and MUST be verified with `PGPVerify.VerifyDetached` using armor encoding.
- If both the embedded OpenPGP signature check and the PGP/MIME signature check yield a `crypto.SignatureVerificationError`, `Decrypt` MUST call `OnError` once for each error (embedded first, then PGP/MIME) and then call `OnVerified` once with the higher of the two `Status` values, where `SIGNATURE_FAILED` > `SIGNATURE_NO_VERIFIER` > `SIGNATURE_NOT_SIGNED` > `SIGNATURE_OK`.
- Otherwise, when `verifyHandle` is not nil, `Decrypt` MUST call `OnVerified(constants.SIGNATURE_OK)` exactly once; when `verifyHandle` is nil, `Decrypt` MUST NOT call `OnVerified`.
- `OnBody` MUST be called once with the message body and its MIME type, with invalid UTF-8 sequences in the body replaced by the Unicode replacement character.
- `OnAttachment` MUST be called once per attachment with the attachment's raw header block string and its decoded bytes.
- `OnEncryptedHeaders` MUST be called once with an empty string after the body and attachment callbacks.

## Constraints

1. The migration MUST NOT remove or rename the public `mime` API or change its observable callback behavior outside the stated goal.
2. Production code in the `mime` package MUST NOT import or call `github.com/ProtonMail/go-mime` for the migrated MIME parsing, traversal, decoding, or collection responsibilities.
3. Production code in the `mime` package MUST NOT delegate the migrated responsibilities to the `go-mime` runtime through subprocesses, RPC, FFI, or dynamic loading.
4. Changes MUST be limited to `mime/mime.go`, `mime/signature_collector.go`, `mime/mime_test.go`, `go.mod`, and the corresponding `go.sum` update.
5. The exported API and behavior of `crypto`, `armor`, `constants`, `profile`, and `mobile` MUST NOT change; `go-crypto` and other out-of-scope dependencies may remain.
6. The MIME test fixtures under `mime/testdata/` MUST NOT be modified, removed, or replaced.
7. You MUST NOT skip checks, delete or weaken assertions, loosen comparisons, or hardcode inputs to fake compliance.
8. `mime/mime_test.go` MAY be adapted to the new internal implementation, provided its behavioral assertions on callback values, signature statuses, and errors are preserved.
9. All work MUST be performed in the repository checkout at `/workspace/repo`, and all created or modified files MUST be left in the working tree; the changes are collected as a patch of the working tree against the provided starting commit.

## Validation

From `/workspace/repo`, run `go test -v -race ./...`.

This runs the Go unit tests, including the `mime` package tests, and checks that the migrated implementation still produces the expected callback values, signature statuses, and errors. Passing this command does not by itself prove every requirement above.
