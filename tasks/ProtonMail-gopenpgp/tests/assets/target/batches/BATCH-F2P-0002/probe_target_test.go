package mime_test

import (
	"bytes"
	"errors"
	"strings"
	"testing"

	"github.com/ProtonMail/gopenpgp/v3/constants"
	"github.com/ProtonMail/gopenpgp/v3/crypto"
	"github.com/ProtonMail/gopenpgp/v3/mime"
)

// Black-box F2P probes (RG-F): every observation is derived only from the
// public `mime.Decrypt` / `MIMECallbacks` contract, never from an internal
// helper name.  A behaviourally-equivalent migration therefore passes even
// when its internal symbols differ from the reference implementation.

const probeCRLF = "\r\n"

type probeRecorder struct {
	body     []string
	mtype    []string
	attHdr   []string
	attData  [][]byte
	encHdr   []string
	verified []int
	errs     []error
}

func (r *probeRecorder) OnBody(b, m string) {
	r.body = append(r.body, b)
	r.mtype = append(r.mtype, m)
}
func (r *probeRecorder) OnAttachment(h string, d []byte) {
	r.attHdr = append(r.attHdr, h)
	r.attData = append(r.attData, d)
}
func (r *probeRecorder) OnEncryptedHeaders(h string) { r.encHdr = append(r.encHdr, h) }
func (r *probeRecorder) OnVerified(v int)            { r.verified = append(r.verified, v) }
func (r *probeRecorder) OnError(e error)             { r.errs = append(r.errs, e) }

// Count real verification calls so a successful outer signature cannot hide
// a parser that silently ignores a signed part inside its content.
type probeCountingVerifier struct {
	crypto.PGPVerify
	calls int
}

func (v *probeCountingVerifier) VerifyDetached(data, signature []byte, encoding int8) (*crypto.VerifyResult, error) {
	v.calls++
	if encoding != crypto.Armor {
		return nil, errors.New("probe: detached MIME signature must use armor encoding")
	}
	return v.PGPVerify.VerifyDetached(data, signature, encoding)
}

type probeEnv struct {
	pgp      *crypto.PGPHandle
	priv     *crypto.Key
	pub      *crypto.Key
	privRing *crypto.KeyRing
	pubRing  *crypto.KeyRing
}

func newProbeEnv(t *testing.T) *probeEnv {
	t.Helper()
	pgp := crypto.PGP()
	key, err := pgp.KeyGeneration().AddUserId("F2P2 Probe", "f2p2@example.invalid").New().GenerateKey()
	if err != nil {
		t.Fatalf("keygen: %v", err)
	}
	pub, err := key.ToPublic()
	if err != nil {
		t.Fatalf("topub: %v", err)
	}
	privRing, err := crypto.NewKeyRing(key)
	if err != nil {
		t.Fatalf("privring: %v", err)
	}
	pubRing, err := crypto.NewKeyRing(pub)
	if err != nil {
		t.Fatalf("pubring: %v", err)
	}
	return &probeEnv{pgp: pgp, priv: key, pub: pub, privRing: privRing, pubRing: pubRing}
}

func (e *probeEnv) encrypt(t *testing.T, plain []byte) string {
	t.Helper()
	handle, err := e.pgp.Encryption().Recipient(e.pub).New()
	if err != nil {
		t.Fatalf("enc handle: %v", err)
	}
	msg, err := handle.Encrypt(plain)
	if err != nil {
		t.Fatalf("encrypt: %v", err)
	}
	armored, err := msg.Armor()
	if err != nil {
		t.Fatalf("armor: %v", err)
	}
	return armored
}

func (e *probeEnv) run(t *testing.T, plain []byte, withVerifier bool) *probeRecorder {
	t.Helper()
	armored := e.encrypt(t, plain)
	dec, err := e.pgp.Decryption().DecryptionKeys(e.privRing).VerificationKeys(e.pubRing).New()
	if err != nil {
		t.Fatalf("dec handle: %v", err)
	}
	var vh crypto.PGPVerify
	if withVerifier {
		vh, err = e.pgp.Verify().VerificationKeys(e.pubRing).New()
		if err != nil {
			t.Fatalf("ver handle: %v", err)
		}
	}
	r := &probeRecorder{}
	mime.Decrypt([]byte(armored), crypto.Armor, dec, vh, r)
	return r
}

func multiMixedQP() []byte {
	return []byte("Content-Type: multipart/mixed; boundary=\"b1\"" + probeCRLF + probeCRLF +
		"--b1" + probeCRLF +
		"Content-Type: text/plain; charset=iso-8859-1" + probeCRLF +
		"Content-Transfer-Encoding: quoted-printable" + probeCRLF + probeCRLF +
		"caf=E9" + probeCRLF +
		"--b1" + probeCRLF +
		"Content-Type: application/octet-stream" + probeCRLF +
		"Content-Transfer-Encoding: base64" + probeCRLF + probeCRLF +
		"AQID" + probeCRLF +
		"--b1--" + probeCRLF)
}

func multiAlternative() []byte {
	return []byte("Content-Type: multipart/alternative; boundary=\"b1\"" + probeCRLF + probeCRLF +
		"--b1" + probeCRLF +
		"Content-Type: text/plain; charset=utf-8" + probeCRLF + probeCRLF +
		"plain" + probeCRLF +
		"--b1" + probeCRLF +
		"Content-Type: text/html; charset=utf-8" + probeCRLF + probeCRLF +
		"<p>html</p>" + probeCRLF +
		"--b1--" + probeCRLF)
}

func multiUnknownCharset() []byte {
	return []byte("Content-Type: multipart/mixed; boundary=\"b1\"" + probeCRLF + probeCRLF +
		"--b1" + probeCRLF +
		"Content-Type: text/plain; charset=x-unknown" + probeCRLF + probeCRLF +
		"hello" + probeCRLF +
		"--b1" + probeCRLF +
		"Content-Type: application/octet-stream" + probeCRLF +
		"Content-Transfer-Encoding: base64" + probeCRLF + probeCRLF +
		"AQID" + probeCRLF +
		"--b1--" + probeCRLF)
}

func multiUnknownEncoding() []byte {
	return []byte("Content-Type: multipart/mixed; boundary=\"b1\"" + probeCRLF + probeCRLF +
		"--b1" + probeCRLF +
		"Content-Type: text/plain" + probeCRLF +
		"Content-Transfer-Encoding: x-unknown" + probeCRLF + probeCRLF +
		"hello" + probeCRLF +
		"--b1" + probeCRLF +
		"Content-Type: application/octet-stream" + probeCRLF +
		"Content-Transfer-Encoding: base64" + probeCRLF + probeCRLF +
		"AQID" + probeCRLF +
		"--b1--" + probeCRLF)
}

func multiDisposition() []byte {
	return []byte("Content-Type: multipart/mixed; boundary=\"b1\"" + probeCRLF + probeCRLF +
		"--b1" + probeCRLF +
		"Content-Type: text/plain; charset=utf-8" + probeCRLF +
		"Content-Disposition: attachment; filename=a.txt" + probeCRLF + probeCRLF +
		"dispositioned" + probeCRLF +
		"--b1" + probeCRLF +
		"Content-Type: text/plain; charset=utf-8" + probeCRLF + probeCRLF +
		"plain body" + probeCRLF +
		"--b1--" + probeCRLF)
}

// F2P-025/F2P-027/F2P-033/F2P-043/F2P-045: non-UTF-8 charsets decode for text
// parts once the migrated decoder registers the charset readers.
func TestProbeGoMessageCharsetDecoding(t *testing.T) {
	e := newProbeEnv(t)
	r := e.run(t, []byte("Content-Type: text/plain; charset=iso-8859-1"+probeCRLF+probeCRLF+"caf\xE9"+probeCRLF), true)
	if len(r.body) != 1 || !strings.Contains(r.body[0], "caf\u00e9") {
		t.Fatalf("charset not decoded through the migrated decoder: body=%q errs=%v", r.body, r.errs)
	}
}

// F2P-037/F2P-038/F2P-042/F2P-046: unknown part charsets are tolerated.
func TestProbeUnknownCharsetTolerated(t *testing.T) {
	e := newProbeEnv(t)
	r := e.run(t, multiUnknownCharset(), false)
	if len(r.attData) != 1 || len(r.errs) != 0 {
		t.Fatalf("unknown charset not tolerated: att=%d errs=%v", len(r.attData), r.errs)
	}
}

// F2P-040/F2P-031: unknown transfer encodings are tolerated.
func TestProbeUnknownEncodingTolerated(t *testing.T) {
	e := newProbeEnv(t)
	r := e.run(t, multiUnknownEncoding(), false)
	if len(r.attData) != 1 || len(r.errs) != 0 {
		t.Fatalf("unknown encoding not tolerated: att=%d errs=%v", len(r.attData), r.errs)
	}
}

// F2P-026/F2P-032/F2P-036/F2P-048: html body is selected over plain text.
func TestProbeBodySelection(t *testing.T) {
	e := newProbeEnv(t)
	r := e.run(t, multiAlternative(), true)
	if len(r.body) != 1 || r.body[0] != "<p>html</p>" || r.mtype[0] != "text/html" {
		t.Fatalf("html-over-plain selection failed: body=%q type=%q", r.body, r.mtype)
	}
	for _, mediaType := range []string{"text/plain", "text/html"} {
		r := e.run(t, []byte("Content-Type: "+mediaType+"\r\n\r\n"), false)
		if len(r.body) != 1 || len(r.mtype) != 1 || r.body[0] != "" || r.mtype[0] != mediaType || len(r.errs) != 0 {
			t.Fatalf("empty %s body lost its MIME type: body=%q type=%q errs=%v", mediaType, r.body, r.mtype, r.errs)
		}
	}
	t.Run("empty HTML keeps its MIME type", func(t *testing.T) {
		input := bytes.Replace(multiAlternative(), []byte("<p>html</p>"), nil, 1)
		r := e.run(t, input, false)
		if len(r.body) != 1 || len(r.mtype) != 1 || r.body[0] != "" || r.mtype[0] != "text/html" || len(r.errs) != 0 {
			t.Fatalf("empty HTML selection failed: body=%q type=%q errs=%v", r.body, r.mtype, r.errs)
		}
	})
}

// F2P-028/F2P-030/F2P-035/F2P-041: Content-Disposition attachments and raw
// header block re-serialization.
func TestProbeContentDispositionAndHeaders(t *testing.T) {
	e := newProbeEnv(t)
	r := e.run(t, multiDisposition(), false)
	if len(r.attData) != 1 || len(r.body) != 1 || !strings.Contains(r.body[0], "plain body") {
		t.Fatalf("Content-Disposition handling wrong: att=%d body=%q", len(r.attData), r.body)
	}
	rh := e.run(t, multiMixedQP(), false)
	if len(rh.attHdr) != 1 || !strings.Contains(rh.attHdr[0], "Content-Type: application/octet-stream") {
		t.Fatalf("raw header block not re-serialized from the migrated parser: %q", rh.attHdr)
	}
	t.Run("raw header fields and binary bytes", func(t *testing.T) {
		// Order, casing, duplicate fields and folding belong to the raw block.
		// Both conventions for including the final header/body separator are
		// accepted; every header field byte must otherwise be preserved.
		rawHeaders := "x-Z: last alphabetically\r\n" +
			"Content-Type: application/octet-stream\r\n" +
			"X-Repeat: first\r\n" +
			"x-repeat: second\r\n" +
			"X-Folded: first line\r\n\tsecond line\r\n" +
			"Content-Transfer-Encoding: base64\r\n" +
			"Content-Disposition: attachment; filename=a.bin\r\n"
		input := []byte("Content-Type: multipart/mixed; boundary=raw\r\n\r\n" +
			"--raw\r\nContent-Type: text/plain\r\n\r\nbody\r\n" +
			"--raw\r\n" + rawHeaders + "\r\nAAH/\r\n--raw--\r\n")
		r := e.run(t, input, false)
		if len(r.attHdr) != 1 || (r.attHdr[0] != rawHeaders && r.attHdr[0] != rawHeaders+"\r\n") {
			t.Fatalf("attachment raw headers changed: got=%q want=%q", r.attHdr, rawHeaders)
		}
		if len(r.attData) != 1 || !bytes.Equal(r.attData[0], []byte{0, 1, 255}) || len(r.errs) != 0 {
			t.Fatalf("attachment decoding changed binary bytes: data=%v errs=%v", r.attData, r.errs)
		}
	})
}

// F2P-029/F2P-034: the raw multipart/signed first part is verified after
// trim/CRLF canonicalization (public verification path).
func TestProbeRawSignedPart(t *testing.T) {
	e := newProbeEnv(t)
	rawFirst := []byte("Content-Type: text/plain; charset=utf-8" + probeCRLF + probeCRLF + "line with trailing  \t \r")
	canonical := canonicalizeBytes(trimEachLineBytes(rawFirst))
	signer, err := e.pgp.Sign().SigningKey(e.priv).Detached().New()
	if err != nil {
		t.Fatalf("signer: %v", err)
	}
	sig, err := signer.Sign(canonical, crypto.Armor)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	msg := []byte("Content-Type: multipart/signed; micalg=pgp-sha256; protocol=\"application/pgp-signature\"; boundary=\"sig\"" + probeCRLF + probeCRLF +
		"--sig" + probeCRLF + string(rawFirst) + probeCRLF +
		"--sig" + probeCRLF +
		"Content-Type: application/pgp-signature" + probeCRLF + probeCRLF +
		string(sig) + probeCRLF +
		"--sig--" + probeCRLF)
	r := e.run(t, msg, true)
	if len(r.verified) != 1 || r.verified[0] != constants.SIGNATURE_OK {
		t.Fatalf("canonicalized multipart/signed part not verified: verified=%v errs=%v", r.verified, r.errs)
	}
	// A multipart/signed part has the same contract at every MIME depth.
	// The surrounding message is unsigned, so ignoring the nested signature
	// cannot accidentally pass by relying on an embedded OpenPGP signature.
	wrap := func(part []byte, boundary string) []byte {
		return []byte("Content-Type: multipart/mixed; boundary=" + boundary + "\r\n\r\n" +
			"--" + boundary + "\r\n" + string(part) + "\r\n--" + boundary + "--\r\n")
	}
	for _, depth := range []int{1, 2} {
		input := wrap(msg, "outer")
		if depth == 2 {
			input = wrap(input, "outermost")
		}
		for _, withVerifier := range []bool{false, true} {
			r := e.run(t, input, withVerifier)
			if len(r.body) != 1 || len(r.attData) != 0 || len(r.encHdr) != 1 || r.encHdr[0] != "" || len(r.errs) != 0 {
				t.Fatalf("nested signed collection depth=%d verifier=%t: body=%q att=%d headers=%q errs=%v", depth, withVerifier, r.body, len(r.attData), r.encHdr, r.errs)
			}
			if withVerifier {
				if len(r.verified) != 1 || r.verified[0] != constants.SIGNATURE_OK {
					t.Fatalf("nested signature not verified at depth %d: verified=%v errs=%v", depth, r.verified, r.errs)
				}
			} else if len(r.verified) != 0 {
				t.Fatalf("nil verifier emitted signature status at depth %d: %v", depth, r.verified)
			}
		}
		// A real bad signature must still allow the body to be delivered, with
		// typed embedded and MIME errors reported in that order.
		tampered := bytes.Replace(input, []byte("line with trailing"), []byte("tampered content"), 1)
		r := e.run(t, tampered, true)
		if len(r.errs) != 2 || len(r.verified) != 1 || r.verified[0] != constants.SIGNATURE_FAILED || len(r.body) != 1 || len(r.attData) != 0 {
			t.Fatalf("nested bad signature depth=%d: verified=%v body=%q att=%d errs=%v", depth, r.verified, r.body, len(r.attData), r.errs)
		}
		for i, status := range []int{constants.SIGNATURE_NOT_SIGNED, constants.SIGNATURE_FAILED} {
			var sigErr *crypto.SignatureVerificationError
			if !errors.As(r.errs[i], &sigErr) || sigErr.Status != status {
				t.Fatalf("nested error %d depth=%d: got=%v want signature status=%d", i, depth, r.errs[i], status)
			}
		}
	}
	// A later ordinary attachment cannot erase the result of a signed part.
	// The signature still covers the first part, and the outer container is
	// otherwise unsigned.
	withLaterAttachment := []byte("Content-Type: multipart/mixed; boundary=trail\r\n\r\n" +
		"--trail\r\n" + string(msg) + "\r\n" +
		"--trail\r\nContent-Type: application/octet-stream\r\n" +
		"Content-Transfer-Encoding: base64\r\n\r\nAQID\r\n" +
		"--trail--\r\n")
	r = e.run(t, withLaterAttachment, true)
	if len(r.verified) != 1 || r.verified[0] != constants.SIGNATURE_OK ||
		len(r.errs) != 0 || len(r.body) != 1 || len(r.attData) != 1 ||
		!bytes.Equal(r.attData[0], []byte{1, 2, 3}) {
		t.Fatalf("later attachment erased signed status: verified=%v errs=%v body=%q att=%v", r.verified, r.errs, r.body, r.attData)
	}
	tamperedLater := bytes.Replace(withLaterAttachment, []byte("line with trailing"), []byte("tampered content"), 1)
	r = e.run(t, tamperedLater, true)
	if len(r.errs) != 2 || len(r.verified) != 1 || r.verified[0] != constants.SIGNATURE_FAILED ||
		len(r.body) != 1 || len(r.attData) != 1 {
		t.Fatalf("later attachment erased failed signature: verified=%v errs=%v body=%q att=%v", r.verified, r.errs, r.body, r.attData)
	}
	for i, status := range []int{constants.SIGNATURE_NOT_SIGNED, constants.SIGNATURE_FAILED} {
		var sigErr *crypto.SignatureVerificationError
		if !errors.As(r.errs[i], &sigErr) || sigErr.Status != status {
			t.Fatalf("later attachment error %d: got=%v want signature status=%d", i, r.errs[i], status)
		}
	}
	// The signed content can itself be multipart/signed. Its detached
	// signature must be consumed by signature extraction, not exposed as an
	// ordinary attachment by a traversal that only recognizes non-root parts.
	outerSig, err := signer.Sign(canonicalizeBytes(trimEachLineBytes(msg)), crypto.Armor)
	if err != nil {
		t.Fatalf("outer sign: %v", err)
	}
	doubleSigned := []byte("Content-Type: multipart/signed; protocol=\"application/pgp-signature\"; boundary=double\r\n\r\n" +
		"--double\r\n" + string(msg) + "\r\n--double\r\n" +
		"Content-Type: application/pgp-signature\r\n\r\n" + string(outerSig) + "\r\n--double--\r\n")
	verifier, err := e.pgp.Verify().VerificationKeys(e.pubRing).New()
	if err != nil {
		t.Fatalf("double-signed verifier: %v", err)
	}
	counter := &probeCountingVerifier{PGPVerify: verifier}
	decrypter, err := e.pgp.Decryption().DecryptionKeys(e.privRing).VerificationKeys(e.pubRing).New()
	if err != nil {
		t.Fatalf("double-signed decrypter: %v", err)
	}
	r = &probeRecorder{}
	mime.Decrypt([]byte(e.encrypt(t, doubleSigned)), crypto.Armor, decrypter, counter, r)
	if len(r.verified) != 1 || r.verified[0] != constants.SIGNATURE_OK || len(r.errs) != 0 || len(r.body) != 1 || len(r.attData) != 0 {
		t.Fatalf("signed content inside signed content: verified=%v body=%q att=%d errs=%v", r.verified, r.body, len(r.attData), r.errs)
	}
	if counter.calls != 2 {
		t.Fatalf("every multipart/signed part must be verified: got %d calls, want 2", counter.calls)
	}
}

func canonicalizeBytes(text []byte) []byte {
	return bytes.ReplaceAll(bytes.ReplaceAll(text, []byte("\r\n"), []byte("\n")), []byte("\n"), []byte("\r\n"))
}

func trimEachLineBytes(text []byte) []byte {
	lines := bytes.Split(text, []byte("\n"))
	for i := range lines {
		lines[i] = bytes.TrimRight(lines[i], " \t\r")
	}
	return bytes.Join(lines, []byte("\n"))
}
