// Command diffprobe runs a shared differential corpus against the mime package
// of the repository it is compiled in and prints a normalized JSON report.
package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/textproto"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"

	"github.com/ProtonMail/gopenpgp/v3/crypto"
	"github.com/ProtonMail/gopenpgp/v3/mime"
)

type bodyCall struct {
	Body     string `json:"body"`
	MimeType string `json:"mimetype"`
}

type attCall struct {
	Headers map[string]string `json:"headers"`
	Data    string            `json:"data"`
}

type errCall struct {
	Kind   string `json:"kind"`
	Status int    `json:"status"`
}

type scenarioResult struct {
	Name               string   `json:"name"`
	OnBody             []bodyCall `json:"on_body"`
	OnAttachment       []attCall  `json:"on_attachment"`
	OnEncryptedHeaders []string   `json:"on_encrypted_headers"`
	OnVerified         []int      `json:"on_verified"`
	OnError            []errCall  `json:"on_error"`
}

type report struct {
	DecryptSignature        string            `json:"decrypt_signature"`
	CallbackMethods         []string          `json:"callback_methods"`
	GoModRequires           []string          `json:"go_mod_requires"`
	GoModRequiresNormalized []string          `json:"go_mod_requires_normalized"`
	GoCryptoPresent         bool              `json:"go_crypto_present"`
	TestdataHashes          map[string]string `json:"testdata_hashes"`
	Scenarios               []scenarioResult  `json:"scenarios"`
}

// swappedModules are the module coordinates that are intentionally different
// between the source (go-mime) and the reference target (go-message). They are
// removed before the normalized requirement sets are compared so that the
// dedicated residue and deceptive-fallback rules, and not the differential
// equality gate, are responsible for the migration itself.
var swappedModules = []string{
	"github.com/ProtonMail/go-mime@",
	"github.com/emersion/go-message@",
	"github.com/emersion/go-textwrapper@",
}

func normalizeRequires(reqs []string) []string {
	out := make([]string, 0, len(reqs))
	for _, r := range reqs {
		swapped := false
		for _, m := range swappedModules {
			if strings.HasPrefix(r, m) {
				swapped = true
				break
			}
		}
		if !swapped {
			out = append(out, r)
		}
	}
	sort.Strings(out)
	return out
}

type recorder struct {
	res scenarioResult
}

func (r *recorder) OnBody(body string, mimetype string) {
	r.res.OnBody = append(r.res.OnBody, bodyCall{body, mimetype})
}

func (r *recorder) OnAttachment(headers string, data []byte) {
	hdrs := map[string]string{}
	mh, err := textproto.NewReader(bufio.NewReader(strings.NewReader(headers))).ReadMIMEHeader()
	if err == nil {
		for k, vs := range mh {
			hdrs[strings.ToLower(k)] = strings.Join(vs, ",")
		}
	}
	sum := sha256.Sum256(data)
	r.res.OnAttachment = append(r.res.OnAttachment, attCall{hdrs, hex.EncodeToString(sum[:])})
}

func (r *recorder) OnEncryptedHeaders(headers string) {
	r.res.OnEncryptedHeaders = append(r.res.OnEncryptedHeaders, headers)
}

func (r *recorder) OnVerified(verified int) {
	r.res.OnVerified = append(r.res.OnVerified, verified)
}

func (r *recorder) OnError(err error) {
	var valueErr crypto.SignatureVerificationError
	if errors.As(err, &valueErr) {
		r.res.OnError = append(r.res.OnError, errCall{"signature", valueErr.Status})
		return
	}
	var ptrErr *crypto.SignatureVerificationError
	if errors.As(err, &ptrErr) {
		r.res.OnError = append(r.res.OnError, errCall{"signature", ptrErr.Status})
		return
	}
	if strings.HasPrefix(err.Error(), "mime: ") {
		r.res.OnError = append(r.res.OnError, errCall{"mime", 0})
		return
	}
	r.res.OnError = append(r.res.OnError, errCall{"other", 0})
}

func loadPrivateKeyRing(file, passphrase string) (*crypto.KeyRing, error) {
	armored, err := os.ReadFile(filepath.Clean(file))
	if err != nil {
		return nil, err
	}
	unlockedKey, err := crypto.NewPrivateKeyFromArmored(string(armored), []byte(passphrase))
	if err != nil {
		return nil, err
	}
	return crypto.NewKeyRing(unlockedKey)
}

func loadPublicKeyRing(file string) (*crypto.KeyRing, error) {
	armored, err := os.ReadFile(filepath.Clean(file))
	if err != nil {
		return nil, err
	}
	key, err := crypto.NewKeyFromArmored(string(armored))
	if err != nil {
		return nil, err
	}
	return crypto.NewKeyRing(key)
}

func runScenario(decRing, verRing *crypto.KeyRing, messageFile string, verify bool) (scenarioResult, error) {
	message, err := os.ReadFile(filepath.Clean(messageFile))
	if err != nil {
		return scenarioResult{}, err
	}
	pgp := crypto.PGP()
	decHandle, err := pgp.Decryption().
		DecryptionKeys(decRing).
		VerificationKeys(verRing).
		VerifyTime(1557754627).
		New()
	if err != nil {
		return scenarioResult{}, err
	}
	res := scenarioResult{Name: filepath.Base(messageFile)}
	if !verify {
		res.Name += ".nilverify"
	}
	rec := &recorder{res: res}
	if verify {
		verifyHandle, err := pgp.Verify().
			VerificationKeys(verRing).
			VerifyTime(1557754627).
			New()
		if err != nil {
			return scenarioResult{}, err
		}
		mime.Decrypt(message, crypto.Armor, decHandle, verifyHandle, rec)
	} else {
		mime.Decrypt(message, crypto.Armor, decHandle, nil, rec)
	}
	return rec.res, nil
}

func goModRequires(path string) []string {
	f, err := os.Open(filepath.Clean(path))
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []string
	scanner := bufio.NewScanner(f)
	inBlock := false
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "require (" {
			inBlock = true
			continue
		}
		if inBlock && line == ")" {
			inBlock = false
			continue
		}
		if strings.HasPrefix(line, "require ") {
			line = strings.TrimSpace(strings.TrimPrefix(line, "require "))
		} else if !inBlock {
			continue
		}
		if line == "" {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) >= 2 {
			out = append(out, fields[0]+"@"+fields[1])
		}
	}
	sort.Strings(out)
	return out
}

func testdataHashes(root string) map[string]string {
	hashes := map[string]string{}
	_ = filepath.Walk(filepath.Clean(root), func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		sum := sha256.Sum256(data)
		rel, _ := filepath.Rel(root, path)
		hashes[filepath.ToSlash(rel)] = hex.EncodeToString(sum[:])
		return nil
	})
	return hashes
}

func callbackMethods() []string {
	typ := reflect.TypeOf((*mime.MIMECallbacks)(nil)).Elem()
	var out []string
	for i := 0; i < typ.NumMethod(); i++ {
		m := typ.Method(i)
		out = append(out, m.Name+m.Type.String()[len("func"):])
	}
	sort.Strings(out)
	return out
}

func main() {
	var rep report
	rep.DecryptSignature = reflect.TypeOf(mime.Decrypt).String()
	rep.CallbackMethods = callbackMethods()
	rep.GoModRequires = goModRequires("go.mod")
	rep.GoModRequiresNormalized = normalizeRequires(rep.GoModRequires)
	for _, r := range rep.GoModRequires {
		if strings.HasPrefix(r, "github.com/ProtonMail/go-crypto@") {
			rep.GoCryptoPresent = true
		}
	}
	rep.TestdataHashes = testdataHashes("mime/testdata")

	decRing, err := loadPrivateKeyRing("mime/testdata/mime/decryption-key.asc", "test_passphrase")
	if err != nil {
		fmt.Fprintln(os.Stderr, "load decryption key:", err)
		os.Exit(1)
	}
	verRing, err := loadPublicKeyRing("mime/testdata/mime/verification-key.asc")
	if err != nil {
		fmt.Fprintln(os.Stderr, "load verification key:", err)
		os.Exit(1)
	}

	scenarios, _ := filepath.Glob("mime/testdata/mime/scenario_*.asc")
	sort.Strings(scenarios)
	for _, s := range scenarios {
		res, err := runScenario(decRing, verRing, s, true)
		if err != nil {
			fmt.Fprintln(os.Stderr, "scenario", s, err)
			os.Exit(1)
		}
		rep.Scenarios = append(rep.Scenarios, res)
	}
	nilRes, err := runScenario(decRing, verRing, "mime/testdata/mime/scenario_00.asc", false)
	if err != nil {
		fmt.Fprintln(os.Stderr, "nilverify scenario", err)
		os.Exit(1)
	}
	rep.Scenarios = append(rep.Scenarios, nilRes)

	out := json.NewEncoder(os.Stdout)
	out.SetIndent("", "  ")
	if err := out.Encode(rep); err != nil {
		fmt.Fprintln(os.Stderr, "encode report:", err)
		os.Exit(1)
	}
}
