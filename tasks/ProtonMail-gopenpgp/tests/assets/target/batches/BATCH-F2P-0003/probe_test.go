package mime_test

// These observations exercise the candidate's public MIME path. The shared
// black-box probes are staged alongside this file; no library-only result can
// stand in for candidate traversal, decoding, collection or verification.
import (
    "bytes"
    "strings"
    "testing"
)

func TestProbeGoMessageCharset(t *testing.T) {
    TestProbeGoMessageCharsetDecoding(t)
}

func TestProbeGoMessageAttachmentDisposition(t *testing.T) {
    TestProbeContentDispositionAndHeaders(t)
}

func TestProbeGoMessageAttachmentHeaderBlock(t *testing.T) {
    TestProbeContentDispositionAndHeaders(t)
}

func TestProbeGoMessageMultipartSignedRaw(t *testing.T) {
    TestProbeRawSignedPart(t)
}

func TestProbeGoMessageUnknownCharset(t *testing.T) {
    TestProbeUnknownCharsetTolerated(t)
}

func TestProbeGoMessageUnknownEncoding(t *testing.T) {
    TestProbeUnknownEncodingTolerated(t)
}

func TestProbeGoMessageMixedTraversal(t *testing.T) {
    e := newProbeEnv(t)
    r := e.run(t, multiMixedQP(), false)
    if len(r.body) != 1 || !strings.Contains(r.body[0], "café") ||
        len(r.attData) != 1 || !bytes.Equal(r.attData[0], []byte{1, 2, 3}) || len(r.errs) != 0 {
        t.Fatalf("candidate mixed traversal/decoding failed: body=%q att=%v errs=%v", r.body, r.attData, r.errs)
    }

    t.Run("malformed transfer encoding reports a MIME error", func(t *testing.T) {
        malformed := []byte("Content-Type: multipart/mixed; boundary=bad\r\n\r\n" +
            "--bad\r\nContent-Type: text/plain\r\n\r\nbody\r\n" +
            "--bad\r\nContent-Type: application/octet-stream\r\n" +
            "Content-Transfer-Encoding: base64\r\n\r\n%%%\r\n--bad--\r\n")
        got := e.run(t, malformed, false)
        if len(got.errs) == 0 || !strings.HasPrefix(got.errs[0].Error(), "mime: ") {
            t.Fatalf("unreadable encoded part must report a MIME error: body=%q att=%v errs=%v", got.body, got.attData, got.errs)
        }
    })
}

func TestProbeGoMessageAlternativeSelection(t *testing.T) {
    TestProbeBodySelection(t)
}
