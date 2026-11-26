package differentialprobe

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"log"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/huseyinbabal/taws/internal/aws"
	"github.com/huseyinbabal/taws/internal/completion"
	"github.com/huseyinbabal/taws/internal/config"
	"github.com/huseyinbabal/taws/internal/resource"
)

var tawsBin string

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "taws-diff-probe")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	tawsBin = filepath.Join(dir, "taws")
	cmd := exec.Command("go", "build", "-o", tawsBin, "github.com/huseyinbabal/taws")
	cmd.Env = os.Environ()
	if out, err := cmd.CombinedOutput(); err != nil {
		fmt.Fprintf(os.Stderr, "building taws failed: %v\n%s\n", err, out)
		os.Exit(1)
	}
	code := m.Run()
	_ = os.RemoveAll(dir)
	os.Exit(code)
}

func runTaws(t *testing.T, env []string, args ...string) (int, string, string) {
	t.Helper()
	cmd := exec.Command(tawsBin, args...)
	cmd.Env = append(os.Environ(), env...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	code := 0
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			code = ee.ExitCode()
		} else {
			t.Fatalf("running taws %v: %v", args, err)
		}
	}
	return code, stdout.String(), stderr.String()
}

func isolatedHome(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", filepath.Join(home, ".config"))
	t.Setenv("AWS_CONFIG_FILE", "")
	t.Setenv("AWS_SHARED_CREDENTIALS_FILE", "")
	t.Setenv("AWS_ENDPOINT_URL", "")
	return home
}

// --- DIFF-011 is placed first so that LoadCACertificates' sync.Once sees the
// configured bundle. ---

func TestDiff011EnvironmentOverrides(t *testing.T) {
	home := isolatedHome(t)

	// AWS_SHARED_CREDENTIALS_FILE redirects credential reads.
	credsFile := filepath.Join(home, "creds")
	if err := os.WriteFile(credsFile, []byte("[dev]\naws_access_key_id = AKIADEV\naws_secret_access_key = secret\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_SHARED_CREDENTIALS_FILE", credsFile)
	t.Setenv("AWS_CONFIG_FILE", filepath.Join(home, "config"))
	profiles, err := aws.ListProfiles()
	if err != nil {
		t.Fatal(err)
	}
	if !contains(profiles, "dev") {
		t.Fatalf("AWS_SHARED_CREDENTIALS_FILE not honored, profiles = %v", profiles)
	}

	// AWS_CONFIG_FILE redirects config reads and AwsConfigDir/GetAWSConfigFilePath.
	cfgFile := filepath.Join(home, "awsconfig")
	if err := os.WriteFile(cfgFile, []byte("[profile prod]\nregion = eu-west-1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_CONFIG_FILE", cfgFile)
	got, err := aws.GetAWSConfigFilePath()
	if err != nil {
		t.Fatal(err)
	}
	if got != cfgFile {
		t.Fatalf("GetAWSConfigFilePath = %q, want %q", got, cfgFile)
	}
	dir, err := aws.AwsConfigDir()
	if err != nil {
		t.Fatal(err)
	}
	if dir != home {
		t.Fatalf("AwsConfigDir = %q, want %q", dir, home)
	}

	// AWS_LOGIN_CACHE_DIRECTORY relocates the console-login cache.
	loginConfig := filepath.Join(home, "loginconfig")
	if err := os.WriteFile(loginConfig, []byte("[profile cacheprof]\nlogin_session = mysession\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_CONFIG_FILE", loginConfig)
	cacheDir := filepath.Join(home, "logincache")
	if err := os.MkdirAll(cacheDir, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_LOGIN_CACHE_DIRECTORY", cacheDir)
	sum := sha256.Sum256([]byte("mysession"))
	cacheBody := `{"accessToken":{"accessKeyId":"AKIACACHE","secretAccessKey":"s","sessionToken":"t","expiresAt":"2099-01-01T00:00:00Z"}}`
	if err := os.WriteFile(filepath.Join(cacheDir, hex.EncodeToString(sum[:])+".json"), []byte(cacheBody), 0o644); err != nil {
		t.Fatal(err)
	}
	creds, credErr := aws.LoadCredentialsWithSSOCheck("cacheprof")
	if credErr != nil {
		t.Fatalf("AWS_LOGIN_CACHE_DIRECTORY not honored: %v", credErr)
	}
	if creds.AccessKeyID != "AKIACACHE" {
		t.Fatalf("access key = %q", creds.AccessKeyID)
	}

	// AWS_CA_BUNDLE loads a custom CA bundle instead of the system pool.
	pemBytes := makeCertPEM(t)
	bundle := filepath.Join(home, "ca.pem")
	if err := os.WriteFile(bundle, pemBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_CA_BUNDLE", bundle)
	if got := aws.ParsePEMCertificates(pemBytes); len(got) != 1 {
		t.Fatalf("ParsePEMCertificates len = %d, want 1", len(got))
	}
	if got := aws.LoadCACertificates(); len(got) != 1 {
		t.Fatalf("LoadCACertificates len = %d, want 1 (AWS_CA_BUNDLE)", len(got))
	}
}

func makeCertPEM(t *testing.T) []byte {
	t.Helper()
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "taws-probe"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		IsCA:         true,
	}
	der, err := x509.CreateCertificate(rand.Reader, &tmpl, &tmpl, &priv.PublicKey, priv)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

func TestDiff001RootFlags(t *testing.T) {
	code, stdout, _ := runTaws(t, nil, "--log-level", "off", "--readonly", "--help")
	if code != 0 {
		t.Fatalf("help exit = %d", code)
	}
	for _, want := range []string{"--profile", "--region", "--log-level", "--readonly", "--endpoint-url", "--help", "--version", "-p", "-r", "-h", "-V"} {
		if !strings.Contains(stdout, want) {
			t.Fatalf("help output missing %q:\n%s", want, stdout)
		}
	}

	if code, _, _ := runTaws(t, nil, "--definitely-not-a-flag"); code == 0 {
		t.Fatal("unknown flag should exit non-zero")
	}

	code, _, stderr := runTaws(t, nil, "--log-level", "bogus")
	if code != 2 {
		t.Fatalf("invalid log level exit = %d, want 2", code)
	}
	if !strings.Contains(stderr, "invalid value 'bogus' for '--log-level <LOG_LEVEL>'") {
		t.Fatalf("stderr = %q", stderr)
	}
	if !strings.Contains(stderr, "[possible values: off, error, warn, info, debug, trace]") {
		t.Fatalf("stderr = %q", stderr)
	}

	for _, flag := range []string{"--version", "-V"} {
		code, stdout, _ := runTaws(t, nil, flag)
		if code != 0 || stdout != "taws 1.3.0-rc.8\n" {
			t.Fatalf("%s => code=%d stdout=%q", flag, code, stdout)
		}
	}
}

func TestDiff002SubcommandTree(t *testing.T) {
	if !strings.Contains(completion.Generate("bash"), "_taws") {
		t.Fatal("bash completion missing _taws")
	}
	if !strings.Contains(completion.Generate("zsh"), "#compdef taws") {
		t.Fatal("zsh completion missing #compdef taws")
	}
	if !strings.Contains(completion.Generate("fish"), "complete -c taws") {
		t.Fatal("fish completion missing complete -c taws")
	}
	if !strings.Contains(completion.Generate("powershell"), "Register-ArgumentCompleter") {
		t.Fatal("powershell completion missing Register-ArgumentCompleter")
	}
	if completion.Generate("elvish") != completion.Default() {
		t.Fatal("elvish should fall back to the default script")
	}

	code, stdout, stderr := runTaws(t, nil, "completion", "elvish")
	if code != 0 {
		t.Fatalf("completion elvish exit = %d stderr=%q", code, stderr)
	}
	if !strings.Contains(stdout, "_taws") {
		t.Fatalf("completion elvish did not print default script: %q", stdout)
	}

	code, stdout, _ = runTaws(t, nil, "list-regions")
	if code != 0 {
		t.Fatalf("list-regions exit = %d", code)
	}
	lines := nonEmptyLines(stdout)
	if len(lines) < 5 || lines[0] != "us-east-1" {
		t.Fatalf("list-regions first=%q count=%d", lines[0], len(lines))
	}

	_, help, _ := runTaws(t, nil, "--help")
	if strings.Contains(help, "list-profiles") || strings.Contains(help, "list-regions") {
		t.Fatalf("hidden subcommands leaked into help:\n%s", help)
	}
}

func TestDiff003ConfigKeysLoad(t *testing.T) {
	home := isolatedHome(t)
	dir := filepath.Join(home, ".config", "taws")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	doc := "profile: dev\nregion: eu-west-1\nlast_resource: ec2-instances\nrecently_used_regions:\n  - eu-west-1\n  - us-east-1\n"
	if err := os.WriteFile(filepath.Join(dir, "config.yaml"), []byte(doc), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := config.Load()
	if cfg.Profile == nil || *cfg.Profile != "dev" {
		t.Fatalf("profile = %v", cfg.Profile)
	}
	if cfg.Region == nil || *cfg.Region != "eu-west-1" {
		t.Fatalf("region = %v", cfg.Region)
	}
	if cfg.LastResource == nil || *cfg.LastResource != "ec2-instances" {
		t.Fatalf("last_resource = %v", cfg.LastResource)
	}
	if len(cfg.RecentlyUsedRegions) != 2 || cfg.RecentlyUsedRegions[0] != "eu-west-1" {
		t.Fatalf("recently_used_regions = %v", cfg.RecentlyUsedRegions)
	}

	if err := os.WriteFile(filepath.Join(dir, "config.yaml"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	empty := config.Load()
	if empty.Profile != nil || empty.Region != nil || empty.LastResource != nil || len(empty.RecentlyUsedRegions) != 0 {
		t.Fatalf("empty document should yield defaults: %+v", empty)
	}
}

func TestDiff004ConfigLoadFailure(t *testing.T) {
	home := isolatedHome(t)
	dir := filepath.Join(home, ".config", "taws")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}

	var buf bytes.Buffer
	prev := log.Writer()
	log.SetOutput(&buf)
	defer log.SetOutput(prev)

	if err := os.WriteFile(filepath.Join(dir, "config.yaml"), []byte(": [unterminated"), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := config.Load()
	if cfg.Profile != nil || cfg.Region != nil {
		t.Fatalf("invalid YAML should yield defaults: %+v", cfg)
	}
	if !strings.Contains(buf.String(), "Failed to parse config") {
		t.Fatalf("missing parse warning: %q", buf.String())
	}

	buf.Reset()
	if err := os.Remove(filepath.Join(dir, "config.yaml")); err != nil {
		t.Fatal(err)
	}
	_ = config.Load()
	if !strings.Contains(buf.String(), "Config file does not exist") {
		t.Fatalf("missing absent-file warning: %q", buf.String())
	}
}

func TestDiff005RecentRegionOrdering(t *testing.T) {
	var cfg config.Config
	cfg.RecentlyUsedRegions = []string{"eu-west-1", "us-east-1"}
	cfg.AddRecentRegion("us-east-1")
	if fmt.Sprint(cfg.RecentlyUsedRegions) != "[us-east-1 eu-west-1]" {
		t.Fatalf("dedupe/front = %v", cfg.RecentlyUsedRegions)
	}
	for _, r := range []string{"r1", "r2", "r3", "r4", "r5", "r6", "r7"} {
		cfg.AddRecentRegion(r)
	}
	if len(cfg.RecentlyUsedRegions) != 6 {
		t.Fatalf("len = %d, want 6", len(cfg.RecentlyUsedRegions))
	}
	if cfg.RecentlyUsedRegions[0] != "r7" {
		t.Fatalf("newest = %q", cfg.RecentlyUsedRegions[0])
	}
}

func TestDiff006ResourceKeys(t *testing.T) {
	keys := resource.GetAllResourceKeys()
	if len(keys) < 30 {
		t.Fatalf("key count = %d, want >= 30", len(keys))
	}
	for _, want := range []string{"ec2-instances", "lambda-functions", "s3-buckets"} {
		if !contains(keys, want) {
			t.Fatalf("missing key %q", want)
		}
	}
	if contains(keys, "iam-user-policies") {
		t.Fatal("requires_parent resource iam-user-policies must be excluded")
	}
	for _, k := range keys {
		if def := resource.GetResource(k); def == nil || def.RequiresParent {
			t.Fatalf("key %q is not directly addressable", k)
		}
	}
}

func TestDiff007ProtocolDispatch(t *testing.T) {
	cases := map[string]resource.APIProtocol{
		`"query"`:     resource.ProtocolQuery,
		`"json"`:      resource.ProtocolJSON,
		`"rest-json"`: resource.ProtocolRestJSON,
		`"rest-xml"`:  resource.ProtocolRestXML,
	}
	for input, want := range cases {
		var got resource.APIProtocol
		if err := json.Unmarshal([]byte(input), &got); err != nil || got != want {
			t.Fatalf("unmarshal %s = %q err=%v", input, got, err)
		}
	}
	var bad resource.APIProtocol
	if err := json.Unmarshal([]byte(`"bogus"`), &bad); err == nil {
		t.Fatal("unknown protocol must be rejected")
	}
	if _, err := resource.GetProtocolHandler("bogus").Execute(nil, "ec2", &resource.APIConfig{}, nil); err == nil || !strings.Contains(err.Error(), "unsupported protocol") {
		t.Fatalf("unknown protocol execute err = %v", err)
	}

	var captured *http.Request
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		captured = r
		w.Write([]byte(`<ok/>`))
	}))
	defer server.Close()
	endpoint := server.URL
	client := aws.NewHTTPClient(aws.Credentials{AccessKeyID: "AKID", SecretAccessKey: "secret"}, "us-east-1", &endpoint)
	if _, err := client.QueryRequest("ec2", "DescribeInstances", [][2]string{{"MaxResults", "10"}}); err != nil {
		t.Fatal(err)
	}
	if captured.Method != "POST" || captured.URL.Query().Get("Action") != "DescribeInstances" || captured.URL.Query().Get("Version") != "2016-11-15" {
		t.Fatalf("query wire form: method=%s action=%s version=%s", captured.Method, captured.URL.Query().Get("Action"), captured.URL.Query().Get("Version"))
	}

	var jsonReq *http.Request
	jsonServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		jsonReq = r
		w.Write([]byte(`{}`))
	}))
	defer jsonServer.Close()
	jsonEndpoint := jsonServer.URL
	jsonClient := aws.NewHTTPClient(aws.Credentials{AccessKeyID: "AKID", SecretAccessKey: "secret"}, "us-east-1", &jsonEndpoint)
	if _, err := jsonClient.JSONRequest("dynamodb", "ListTables", `{}`); err != nil {
		t.Fatal(err)
	}
	if jsonReq.Header.Get("X-Amz-Target") != "DynamoDB_20120810.ListTables" || jsonReq.Header.Get("Content-Type") != "application/x-amz-json-1.0" {
		t.Fatalf("json wire form: target=%q ct=%q", jsonReq.Header.Get("X-Amz-Target"), jsonReq.Header.Get("Content-Type"))
	}
}

func TestDiff008RequestConstruction(t *testing.T) {
	if got := resource.ResolveStaticParamTemplate("taws-{resource_id}-{timestamp}", "cluster", "20260309T143000"); got != "taws-cluster-20260309T143000" {
		t.Fatalf("template = %q", got)
	}
	lambda := resource.GetResource("lambda-functions")
	if lambda == nil || lambda.APIConfig == nil {
		t.Fatal("lambda-functions missing api_config")
	}
	if lambda.APIConfig.Method == nil || *lambda.APIConfig.Method != "GET" {
		t.Fatalf("lambda method = %v", lambda.APIConfig.Method)
	}
	if lambda.APIConfig.Path == nil || *lambda.APIConfig.Path != "/2015-03-31/functions" {
		t.Fatalf("lambda path = %v", lambda.APIConfig.Path)
	}
}

func TestDiff009PaginationParsing(t *testing.T) {
	handler := resource.GetProtocolHandler(resource.ProtocolJSON)
	root := "/Parameters"
	output := "/NextToken"
	cfg := &resource.APIConfig{ResponseRoot: &root, Pagination: &resource.PaginationConfig{OutputToken: &output}}
	items, token, err := handler.ParseItems(`{"Parameters":[{"Name":"a"}],"NextToken":"tok1"}`, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || token == nil || *token != "tok1" {
		t.Fatalf("items=%d token=%v", len(items), token)
	}
	_, token, err = handler.ParseItems(`{"Parameters":[{"Name":"a"}]}`, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if token != nil {
		t.Fatalf("absent output token should be nil, got %v", *token)
	}
}

func TestDiff010CredentialPrecedence(t *testing.T) {
	home := isolatedHome(t)
	t.Setenv("AWS_ACCESS_KEY_ID", "AKIAENV")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "envsecret")
	t.Setenv("AWS_SESSION_TOKEN", "envtoken")
	creds, err := aws.LoadCredentials("default")
	if err != nil {
		t.Fatal(err)
	}
	if creds.AccessKeyID != "AKIAENV" || creds.SecretAccessKey != "envsecret" {
		t.Fatalf("env credentials = %+v", creds)
	}
	// Login-required cases apply only after the first credential source is
	// absent. Keep the environment-precedence assertion above independent.
	t.Setenv("AWS_ACCESS_KEY_ID", "")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "")
	t.Setenv("AWS_SESSION_TOKEN", "")

	cfgFile := filepath.Join(home, "sso-config")
	ssoDoc := "[profile dev]\nsso_account_id = 111\nsso_role_name = Admin\nsso_session = mysess\n\n[sso-session mysess]\nsso_start_url = https://example.awsapps.com/start\nsso_region = us-east-1\n"
	if err := os.WriteFile(cfgFile, []byte(ssoDoc), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_CONFIG_FILE", cfgFile)
	_, credErr := aws.LoadCredentialsWithSSOCheck("dev")
	if credErr == nil || credErr.Kind != aws.ErrSSOLoginRequired {
		t.Fatalf("sso error = %v", credErr)
	}
	if !strings.Contains(credErr.Error(), "SSO login required for profile 'dev'") {
		t.Fatalf("sso message = %q", credErr.Error())
	}

	loginConfig := filepath.Join(home, "login-config")
	if err := os.WriteFile(loginConfig, []byte("[profile loginprof]\nlogin_session = mysession\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_CONFIG_FILE", loginConfig)
	_, credErr = aws.LoadCredentialsWithSSOCheck("loginprof")
	if credErr == nil || credErr.Kind != aws.ErrConsoleLoginRequired {
		t.Fatalf("console error = %v", credErr)
	}
	if !strings.Contains(credErr.Error(), "Console login required for profile 'loginprof'") {
		t.Fatalf("console message = %q", credErr.Error())
	}
}

func TestDiff012ListProfilesAndRegions(t *testing.T) {
	home := isolatedHome(t)
	credsFile := filepath.Join(home, "credentials")
	if err := os.WriteFile(credsFile, []byte("[default]\naws_access_key_id = A\n\n[dev]\naws_access_key_id = B\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cfgFile := filepath.Join(home, "config")
	if err := os.WriteFile(cfgFile, []byte("[profile prod]\nregion = eu-west-1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_SHARED_CREDENTIALS_FILE", credsFile)
	t.Setenv("AWS_CONFIG_FILE", cfgFile)
	profiles, err := aws.ListProfiles()
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"default", "dev", "prod"} {
		if !contains(profiles, want) {
			t.Fatalf("profiles %v missing %q", profiles, want)
		}
	}
	regions := aws.ListRegions()
	if len(regions) == 0 || regions[0] != "us-east-1" {
		t.Fatalf("regions = %v", regions)
	}
}

func TestDiff013OperationResults(t *testing.T) {
	resp := resource.BuildResponse([]any{map[string]any{"a": "b"}}, "items", nil)
	m, ok := resp.(map[string]any)
	if !ok || m["items"] == nil {
		t.Fatalf("BuildResponse = %v", resp)
	}
	if _, exists := m["_next_token"]; exists {
		t.Fatal("no _next_token expected without a token")
	}
	tok := "abc"
	resp = resource.BuildResponse(nil, "items", &tok)
	m = resp.(map[string]any)
	if m["_next_token"] != "abc" {
		t.Fatalf("_next_token = %v", m["_next_token"])
	}
	if arr, ok := m["items"].([]any); !ok || len(arr) != 0 {
		t.Fatalf("items = %v", m["items"])
	}

	ssm := resource.GetResource("ssm-parameters")
	if ssm == nil || !actionHasSDKMethod(ssm, "get_parameter") {
		t.Fatal("ssm-parameters must expose the get_parameter read action")
	}
	secrets := resource.GetResource("secretsmanager-secrets")
	if secrets == nil || !actionHasSDKMethod(secrets, "get_secret_value") {
		t.Fatal("secretsmanager-secrets must expose the get_secret_value read action")
	}
	ec2 := resource.GetResource("ec2-instances")
	if ec2 == nil {
		t.Fatal("ec2-instances missing")
	}
	terminate := ec2.ActionConfigs["terminate_instance"]
	if terminate.Action == nil || *terminate.Action != "TerminateInstances" {
		t.Fatalf("ec2 terminate action config = %v", terminate.Action)
	}
}

func actionHasSDKMethod(def *resource.ResourceDef, method string) bool {
	for _, a := range def.Actions {
		if a.SDKMethod == method {
			return true
		}
	}
	return false
}

func TestDiff014StartupToleratesBadConfig(t *testing.T) {
	home := isolatedHome(t)
	dir := filepath.Join(home, ".config", "taws")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "config.yaml"), []byte(": [unterminated"), 0o644); err != nil {
		t.Fatal(err)
	}
	env := []string{"HOME=" + home, "XDG_CONFIG_HOME=" + filepath.Join(home, ".config")}
	code, stdout, stderr := runTaws(t, env, "list-regions")
	if code != 0 {
		t.Fatalf("taws with invalid config exit = %d stderr=%q", code, stderr)
	}
	if len(nonEmptyLines(stdout)) < 5 {
		t.Fatalf("taws did not proceed with defaults: %q", stdout)
	}
}

func TestDiff015ReadOnlySuppression(t *testing.T) {
	resource.SetReadOnly(true)
	defer resource.SetReadOnly(false)
	err := resource.ExecuteAction("ec2", "terminate_instance", nil, "i-123")
	if err == nil {
		t.Fatal("mutating action must be blocked in read-only mode")
	}
	if !strings.Contains(err.Error(), "read-only") {
		t.Fatalf("error = %q", err.Error())
	}
}

func TestDiff016EndpointOverride(t *testing.T) {
	t.Setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
	client := aws.NewHTTPClient(aws.Credentials{AccessKeyID: "AKID", SecretAccessKey: "secret"}, "us-east-1", nil)
	svc, ok := aws.GetService("ec2")
	if !ok {
		t.Fatal("ec2 service missing")
	}
	endpoint, err := client.GetEndpoint(svc)
	if err != nil {
		t.Fatal(err)
	}
	if endpoint != "http://localhost:4566" {
		t.Fatalf("env endpoint = %q", endpoint)
	}

	explicit := "http://explicit:1234"
	client = aws.NewHTTPClient(aws.Credentials{AccessKeyID: "AKID", SecretAccessKey: "secret"}, "us-east-1", &explicit)
	endpoint, err = client.GetEndpoint(svc)
	if err != nil {
		t.Fatal(err)
	}
	if endpoint != explicit {
		t.Fatalf("explicit endpoint = %q, want %q", endpoint, explicit)
	}
}

func TestDiff017NextPageAndMaxResults(t *testing.T) {
	var body string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		buf, _ := io.ReadAll(r.Body)
		body = string(buf)
		w.Write([]byte(`{"Parameters":[{"Name":"p1"}],"NextToken":"next-page"}`))
	}))
	defer server.Close()
	endpoint := server.URL
	client := aws.NewHTTPClient(aws.Credentials{AccessKeyID: "AKID", SecretAccessKey: "secret"}, "us-east-1", &endpoint)
	clients := &aws.AwsClients{HTTP: client, Region: "us-east-1", Profile: "default"}
	pageToken := "tok-from-previous"
	result, err := resource.FetchResourcesPaginated("ssm-parameters", clients, nil, &pageToken)
	if err != nil {
		t.Fatal(err)
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(body), &payload); err != nil {
		t.Fatalf("request body %q: %v", body, err)
	}
	if payload["NextToken"] != "tok-from-previous" {
		t.Fatalf("input token not sent: %v", payload)
	}
	if fmt.Sprint(payload["MaxResults"]) != "50" {
		t.Fatalf("max_results not sent: %v", payload["MaxResults"])
	}
	if result.NextToken == nil || *result.NextToken != "next-page" {
		t.Fatalf("next token = %v", result.NextToken)
	}
}

func TestDiff018LoginRequirementReporting(t *testing.T) {
	home := isolatedHome(t)
	credsFile := filepath.Join(home, "credentials")
	if err := os.WriteFile(credsFile, []byte("[default]\naws_access_key_id = AKIADEFAULT\naws_secret_access_key = secret\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_SHARED_CREDENTIALS_FILE", credsFile)
	cfgFile := filepath.Join(home, "config")
	ssoDoc := "[profile dev]\nsso_account_id = 111\nsso_role_name = Admin\nsso_session = mysess\n\n[sso-session mysess]\nsso_start_url = https://example.awsapps.com/start\nsso_region = us-east-1\n"
	if err := os.WriteFile(cfgFile, []byte(ssoDoc), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_CONFIG_FILE", cfgFile)
	_, credErr := aws.LoadCredentialsWithSSOCheck("dev")
	if credErr == nil || credErr.Kind != aws.ErrSSOLoginRequired || credErr.SSOSession != "mysess" {
		t.Fatalf("sso error = %+v", credErr)
	}
}

func TestDiff020RetainedCapabilities(t *testing.T) {
	_, help, _ := runTaws(t, nil, "--help")
	if !strings.Contains(help, "completion") {
		t.Fatalf("help missing completion subcommand:\n%s", help)
	}
	if len(resource.GetAllResourceKeys()) == 0 {
		t.Fatal("resource registry returned no keys")
	}
	if cfg := config.Load(); cfg == nil {
		t.Fatal("config loader returned nil")
	}
}

func TestDiff021PortedTestSuite(t *testing.T) {
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	root := filepath.Dir(wd)
	checks := map[string]string{
		"internal/config/config_test.go":         "func TestAddRecentRegion",
		"internal/resource/registry_test.go":     "func TestGetAllResourceKeys",
		"internal/resource/field_mapper_test.go": "func TestTransformFormatBytes",
	}
	for rel, needle := range checks {
		data, err := os.ReadFile(filepath.Join(root, rel))
		if err != nil {
			t.Fatalf("read %s: %v", rel, err)
		}
		if !strings.Contains(string(data), needle) {
			t.Fatalf("%s missing behavioral assertion %q", rel, needle)
		}
	}
}

func contains(haystack []string, needle string) bool {
	for _, s := range haystack {
		if s == needle {
			return true
		}
	}
	return false
}

func nonEmptyLines(s string) []string {
	var out []string
	for _, line := range strings.Split(s, "\n") {
		if strings.TrimSpace(line) != "" {
			out = append(out, line)
		}
	}
	return out
}
