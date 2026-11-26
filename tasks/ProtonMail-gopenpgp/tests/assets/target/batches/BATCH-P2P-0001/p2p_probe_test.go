package mime_test

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ProtonMail/gopenpgp/v3/constants"
	"github.com/ProtonMail/gopenpgp/v3/crypto"
	mimepkg "github.com/ProtonMail/gopenpgp/v3/mime"
)

// ----- compile-time surface checks (BATCH-P2P-0001 / R003, R004, R016) -----

var _ func(
	message []byte,
	messageEncoding int8,
	decryptionHandle crypto.PGPDecryption,
	verifyHandle crypto.PGPVerify,
	callbacks mimepkg.MIMECallbacks,
) = mimepkg.Decrypt

var _ mimepkg.MIMECallbacks = (*p2pSignatureCallbacks)(nil)

type p2pSignatureCallbacks struct{}

func (p2pSignatureCallbacks) OnBody(string, string)      {}
func (p2pSignatureCallbacks) OnAttachment(string, []byte) {}
func (p2pSignatureCallbacks) OnEncryptedHeaders(string)   {}
func (p2pSignatureCallbacks) OnVerified(int)              {}
func (p2pSignatureCallbacks) OnError(error)               {}

// ----- recording callbacks -----

type p2pRecorder struct {
	events     []string
	bodies     []string
	mimeTypes  []string
	attHeaders []string
	attData    [][]byte
	encHeaders []string
	verified   []int
	errs       []error
}

func newP2PRecorder() *p2pRecorder { return &p2pRecorder{} }

func (r *p2pRecorder) OnBody(body string, mimetype string) {
	r.events = append(r.events, "body")
	r.bodies = append(r.bodies, body)
	r.mimeTypes = append(r.mimeTypes, mimetype)
}

func (r *p2pRecorder) OnAttachment(headers string, data []byte) {
	r.events = append(r.events, "attachment")
	r.attHeaders = append(r.attHeaders, headers)
	r.attData = append(r.attData, data)
}

func (r *p2pRecorder) OnEncryptedHeaders(headers string) {
	r.events = append(r.events, "encryptedheaders")
	r.encHeaders = append(r.encHeaders, headers)
}

func (r *p2pRecorder) OnVerified(status int) {
	r.events = append(r.events, "verified")
	r.verified = append(r.verified, status)
}

func (r *p2pRecorder) OnError(err error) {
	r.events = append(r.events, "error")
	r.errs = append(r.errs, err)
}

// ----- keys and messages -----

func p2pReadFile(t *testing.T, path string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Clean(path))
	if err != nil {
		t.Fatalf("p2p probe: cannot read %s: %v", path, err)
	}
	return data
}

func p2pDecryptionKeyRing(t *testing.T, file, passphrase string) *crypto.KeyRing {
	t.Helper()
	key, err := crypto.NewPrivateKeyFromArmored(string(p2pReadFile(t, file)), []byte(passphrase))
	if err != nil {
		t.Fatalf("p2p probe: cannot unlock private key %s: %v", file, err)
	}
	ring, err := crypto.NewKeyRing(key)
	if err != nil {
		t.Fatalf("p2p probe: cannot create private keyring: %v", err)
	}
	return ring
}

func p2pPublicKeyRing(t *testing.T, file string) *crypto.KeyRing {
	t.Helper()
	key, err := crypto.NewKeyFromArmored(string(p2pReadFile(t, file)))
	if err != nil {
		t.Fatalf("p2p probe: cannot load public key %s: %v", file, err)
	}
	if key.IsPrivate() {
		publicKey, err := key.GetPublicKey()
		if err != nil {
			t.Fatalf("p2p probe: cannot derive public key: %v", err)
		}
		key, err = crypto.NewKey(publicKey)
		if err != nil {
			t.Fatalf("p2p probe: cannot parse derived public key: %v", err)
		}
	}
	ring, err := crypto.NewKeyRing(key)
	if err != nil {
		t.Fatalf("p2p probe: cannot create public keyring: %v", err)
	}
	return ring
}

func p2pDecrypt(t *testing.T, message []byte, encoding int8, verify bool) *p2pRecorder {
	t.Helper()
	decRing := p2pDecryptionKeyRing(t, "testdata/mime/decryption-key.asc", "test_passphrase")
	verRing := p2pPublicKeyRing(t, "testdata/mime/verification-key.asc")
	pgp := crypto.PGP()
	decHandle, err := pgp.Decryption().
		DecryptionKeys(decRing).
		VerificationKeys(verRing).
		VerifyTime(1557754627).
		New()
	if err != nil {
		t.Fatalf("p2p probe: cannot build decryption handle: %v", err)
	}
	var verifyHandle crypto.PGPVerify
	if verify {
		verifyHandle, err = pgp.Verify().
			VerificationKeys(verRing).
			VerifyTime(1557754627).
			New()
		if err != nil {
			t.Fatalf("p2p probe: cannot build verify handle: %v", err)
		}
	}
	rec := newP2PRecorder()
	mimepkg.Decrypt(message, encoding, decHandle, verifyHandle, rec)
	return rec
}

func p2pScenario(t *testing.T, name string, verify bool) *p2pRecorder {
	t.Helper()
	return p2pDecrypt(t, p2pReadFile(t, "testdata/mime/"+name), crypto.Armor, verify)
}

// p2pEncryptToDecryptionKey encrypts an arbitrary plaintext MIME body to the
// public counterpart of the fixture decryption key, so that Decrypt processes
// exactly the bytes supplied by the probe.
func p2pEncryptToDecryptionKey(t *testing.T, plaintext []byte) []byte {
	t.Helper()
	privateKey, err := crypto.NewPrivateKeyFromArmored(
		string(p2pReadFile(t, "testdata/mime/decryption-key.asc")),
		[]byte("test_passphrase"),
	)
	if err != nil {
		t.Fatalf("p2p probe: cannot unlock private key: %v", err)
	}
	armoredPublic, err := privateKey.GetArmoredPublicKey()
	if err != nil {
		t.Fatalf("p2p probe: cannot derive public key: %v", err)
	}
	publicKey, err := crypto.NewKeyFromArmored(armoredPublic)
	if err != nil {
		t.Fatalf("p2p probe: cannot parse public key: %v", err)
	}
	encHandle, err := crypto.PGP().Encryption().Recipient(publicKey).New()
	if err != nil {
		t.Fatalf("p2p probe: cannot build encryption handle: %v", err)
	}
	pgpMessage, err := encHandle.Encrypt(plaintext)
	if err != nil {
		t.Fatalf("p2p probe: cannot encrypt plaintext: %v", err)
	}
	return pgpMessage.Bytes()
}

func p2pDecryptCrafted(t *testing.T, plaintext []byte) *p2pRecorder {
	t.Helper()
	return p2pDecrypt(t, p2pEncryptToDecryptionKey(t, plaintext), crypto.Bytes, false)
}

// ----- observations -----

// P2P-003 / P2P-004: the exported mime API exists with the preserved signature.
func TestP2P003AndP2P004ExportedAPI(t *testing.T) {
	var callbacks mimepkg.MIMECallbacks = p2pSignatureCallbacks{}
	callbacks.OnBody("", "")
	callbacks.OnAttachment("", nil)
	callbacks.OnEncryptedHeaders("")
	callbacks.OnVerified(constants.SIGNATURE_OK)
	callbacks.OnError(nil)
}

// P2P-013: a concrete type implements exactly the preserved MIMECallbacks set.
func TestP2P013CallbacksInterface(t *testing.T) {
	var callbacks mimepkg.MIMECallbacks = &p2pSignatureCallbacks{}
	if callbacks == nil {
		t.Fatal("p2p probe: nil MIMECallbacks")
	}
}

// P2P-002: a failing decryption handle yields exactly one OnError and no other callback.
func TestP2P002DecryptionFailure(t *testing.T) {
	sentinel := errors.New("p2p-sentinel-decryption-error")
	rec := newP2PRecorder()
	mimepkg.Decrypt([]byte("irrelevant"), crypto.Armor, &p2pFailingDecryption{err: sentinel}, nil, rec)
	if len(rec.events) != 1 || rec.events[0] != "error" {
		t.Fatalf("p2p probe: expected only OnError, got %v", rec.events)
	}
	if !errors.Is(rec.errs[0], sentinel) {
		t.Fatalf("p2p probe: OnError did not receive the original error: %v", rec.errs[0])
	}
}

type p2pFailingDecryption struct{ err error }

func (d *p2pFailingDecryption) Decrypt([]byte, int8) (*crypto.VerifiedDataResult, error) {
	return nil, d.err
}
func (d *p2pFailingDecryption) DecryptingReader(crypto.Reader, int8) (*crypto.VerifyDataReader, error) {
	return nil, d.err
}
func (d *p2pFailingDecryption) DecryptDetached([]byte, []byte, int8) (*crypto.VerifiedDataResult, error) {
	return nil, d.err
}
func (d *p2pFailingDecryption) DecryptSessionKey([]byte) (*crypto.SessionKey, error) {
	return nil, d.err
}
func (d *p2pFailingDecryption) ClearPrivateParams() {}

// P2P-006: go-message charset decoding keeps quoted-printable ISO-8859-1 bodies.
func TestP2P007OnBodyInvalidUTF8(t *testing.T) {
	plaintext := []byte("Content-Type: text/plain; charset=utf-8\r\nMIME-Version: 1.0\r\n\r\nhello\xffworld")
	rec := p2pDecryptCrafted(t, plaintext)
	if len(rec.bodies) != 1 {
		t.Fatalf("p2p probe: expected exactly one OnBody, got %v", rec.events)
	}
	if rec.bodies[0] != "hello\ufffdworld" {
		t.Fatalf("p2p probe: invalid UTF-8 not replaced, got %q", rec.bodies[0])
	}
	if rec.mimeTypes[0] != "text/plain" {
		t.Fatalf("p2p probe: expected text/plain, got %q", rec.mimeTypes[0])
	}
}

// P2P-008: unreadable decrypted content produces an error prefixed with "mime: ".
func TestP2P008UnreadableMIME(t *testing.T) {
	rec := p2pDecryptCrafted(t, []byte("\x00\x01 not a mime message"))
	if len(rec.errs) == 0 {
		t.Fatalf("p2p probe: expected OnError for unreadable MIME, got %v", rec.events)
	}
	if !strings.HasPrefix(rec.errs[0].Error(), "mime: ") {
		t.Fatalf("p2p probe: error not prefixed with \"mime: \": %q", rec.errs[0].Error())
	}
	t.Run("empty decrypted message", func(t *testing.T) {
		rec := p2pDecryptCrafted(t, nil)
		if len(rec.events) != 1 || rec.events[0] != "error" || len(rec.errs) != 1 || !strings.HasPrefix(rec.errs[0].Error(), "mime: ") {
			t.Fatalf("p2p probe: empty MIME must emit only a mime error, events=%v errs=%v", rec.events, rec.errs)
		}
	})
}

// P2P-010: a valid detached signature is verified (multipart/signed canonicalization).
func TestP2P010MultipartSignedVerification(t *testing.T) {
	rec := p2pScenario(t, "scenario_00.asc", true)
	if len(rec.errs) != 0 {
		t.Fatalf("p2p probe: expected verified signature, got errors %v", rec.errs)
	}
	if len(rec.verified) != 1 || rec.verified[0] != constants.SIGNATURE_OK {
		t.Fatalf("p2p probe: expected OnVerified(SIGNATURE_OK), got %v", rec.verified)
	}
}

// P2P-011: callback order is signature status, OnBody, attachments, final OnEncryptedHeaders.
func TestP2P011CallbackOrder(t *testing.T) {
	plaintext := []byte(strings.Join([]string{
		"Content-Type: multipart/mixed; boundary=\"p2p\"",
		"MIME-Version: 1.0",
		"",
		"--p2p",
		"Content-Type: text/plain",
		"",
		"body text",
		"--p2p",
		"Content-Type: application/octet-stream",
		"Content-Transfer-Encoding: base64",
		"",
		"AAEC",
		"--p2p",
		"Content-Type: application/octet-stream",
		"Content-Transfer-Encoding: base64",
		"",
		"AwQF",
		"--p2p--",
		"",
	}, "\r\n"))
	rec := p2pDecryptCrafted(t, plaintext)
	want := []string{"body", "attachment", "attachment", "encryptedheaders"}
	if len(rec.events) != len(want) {
		t.Fatalf("p2p probe: unexpected callback sequence %v", rec.events)
	}
	for i := range want {
		if rec.events[i] != want[i] {
			t.Fatalf("p2p probe: expected callback sequence %v, got %v", want, rec.events)
		}
	}
}

// P2P-012: a non-nil verify handle yields one OnVerified; a nil handle yields none.
func TestP2P012VerifyHandleNilBehavior(t *testing.T) {
	withVerify := p2pScenario(t, "scenario_00.asc", true)
	if len(withVerify.verified) != 1 || withVerify.verified[0] != constants.SIGNATURE_OK {
		t.Fatalf("p2p probe: non-nil verify handle expected exactly one OnVerified(SIGNATURE_OK), got %v", withVerify.verified)
	}
	withoutVerify := p2pScenario(t, "scenario_00.asc", false)
	if len(withoutVerify.verified) != 0 {
		t.Fatalf("p2p probe: nil verify handle expected no OnVerified, got %v", withoutVerify.verified)
	}
}

// P2P-014: OnEncryptedHeaders("") is the final callback for a parsed message.
func TestP2P014EncryptedHeaders(t *testing.T) {
	rec := p2pScenario(t, "scenario_00.asc", true)
	if len(rec.encHeaders) != 1 || rec.encHeaders[0] != "" {
		t.Fatalf("p2p probe: expected a single OnEncryptedHeaders(\"\"), got %v", rec.encHeaders)
	}
	if rec.events[len(rec.events)-1] != "encryptedheaders" {
		t.Fatalf("p2p probe: OnEncryptedHeaders was not the final callback: %v", rec.events)
	}
}

// P2P-015: dual signature failure reports both errors and the highest status.
func TestP2P015DualSignatureFailure(t *testing.T) {
	for _, name := range []string{"scenario_13.asc", "scenario_23.asc"} {
		rec := p2pScenario(t, name, true)
		if len(rec.errs) != 2 {
			t.Errorf("p2p probe: %s expected two OnError calls, got %d (%v)", name, len(rec.errs), rec.events)
		}
		if len(rec.verified) != 1 || rec.verified[0] != constants.SIGNATURE_FAILED {
			t.Errorf("p2p probe: %s expected OnVerified(SIGNATURE_FAILED), got %v", name, rec.verified)
		}
	}
}

// P2P-016: every OnVerified argument is one of the constants signature statuses.
func TestP2P016OnVerifiedStatuses(t *testing.T) {
	allowed := map[int]bool{
		constants.SIGNATURE_OK:          true,
		constants.SIGNATURE_NOT_SIGNED:  true,
		constants.SIGNATURE_NO_VERIFIER: true,
		constants.SIGNATURE_FAILED:      true,
		constants.SIGNATURE_BAD_CONTEXT: true,
	}
	for _, name := range []string{
		"scenario_00.asc", "scenario_01.asc", "scenario_02.asc", "scenario_03.asc",
		"scenario_10.asc", "scenario_11.asc", "scenario_12.asc", "scenario_13.asc",
		"scenario_20.asc", "scenario_21.asc", "scenario_22.asc", "scenario_23.asc",
	} {
		rec := p2pScenario(t, name, true)
		for _, status := range rec.verified {
			if !allowed[status] {
				t.Errorf("p2p probe: %s produced OnVerified(%d)", name, status)
			}
		}
	}
}

// P2P-018: OnError receives typed crypto.SignatureVerificationError values.
func TestP2P018TypedSignatureErrors(t *testing.T) {
	cases := map[string][]int{
		"scenario_11.asc": {constants.SIGNATURE_NOT_SIGNED, constants.SIGNATURE_NOT_SIGNED},
		"scenario_12.asc": {constants.SIGNATURE_NOT_SIGNED, constants.SIGNATURE_NO_VERIFIER},
		"scenario_13.asc": {constants.SIGNATURE_NOT_SIGNED, constants.SIGNATURE_FAILED},
		"scenario_21.asc": {constants.SIGNATURE_NO_VERIFIER, constants.SIGNATURE_NOT_SIGNED},
		"scenario_22.asc": {constants.SIGNATURE_NO_VERIFIER, constants.SIGNATURE_NO_VERIFIER},
		"scenario_23.asc": {constants.SIGNATURE_NO_VERIFIER, constants.SIGNATURE_FAILED},
	}
	for name, want := range cases {
		rec := p2pScenario(t, name, true)
		if len(rec.errs) != len(want) {
			t.Errorf("p2p probe: %s expected %d typed errors, got %d", name, len(want), len(rec.errs))
			continue
		}
		for i, err := range rec.errs {
			var sigErr *crypto.SignatureVerificationError
			if !errors.As(err, &sigErr) {
				t.Errorf("p2p probe: %s error %d is not a SignatureVerificationError: %v", name, i, err)
				continue
			}
			if sigErr.Status != want[i] {
				t.Errorf("p2p probe: %s error %d expected status %d, got %d", name, i, want[i], sigErr.Status)
			}
		}
	}
}

// P2P-019: every attachment is delivered in MIME order with decoded bytes.
func TestP2P019Attachments(t *testing.T) {
	plaintext := []byte(strings.Join([]string{
		"Content-Type: multipart/mixed; boundary=\"p2p\"",
		"MIME-Version: 1.0",
		"",
		"--p2p",
		"Content-Type: application/octet-stream",
		"Content-Transfer-Encoding: base64",
		"",
		"AAEC",
		"--p2p",
		"Content-Type: application/octet-stream",
		"Content-Transfer-Encoding: base64",
		"",
		"AwQF",
		"--p2p--",
		"",
	}, "\r\n"))
	rec := p2pDecryptCrafted(t, plaintext)
	if len(rec.attData) != 2 {
		t.Fatalf("p2p probe: expected two attachments, got %d (%v)", len(rec.attData), rec.events)
	}
	if string(rec.attData[0]) != string([]byte{0x00, 0x01, 0x02}) {
		t.Errorf("p2p probe: first attachment decoded incorrectly: %v", rec.attData[0])
	}
	if string(rec.attData[1]) != string([]byte{0x03, 0x04, 0x05}) {
		t.Errorf("p2p probe: second attachment decoded incorrectly: %v", rec.attData[1])
	}
}
