# Migrate the rust-url libraries from Rust to Go

## Background

`/workspace/repo` is the `servo/rust-url` workspace: a set of Rust (edition 2018, `#![no_std]` with `alloc`) crates that implement URL handling. The `url` 2.5.8 crate parses and serializes URLs per the WHATWG URL Standard and depends on three sibling crates:

- `idna` 1.1.0: UTS #46 IDNA processing and RFC 3492 Punycode.
- `percent-encoding` 2.3.2: percent-encoding and decoding.
- `form_urlencoded` 1.2.2: `application/x-www-form-urlencoded` parsing and serialization.

The workspace also contains the out-of-scope `data-url` and `url_debug_tests` crates.

## Goal

- Migrate the URL parsing/serialization, IDNA/UTS #46/Punycode, percent-encoding, and form-urlencoded behavior of the Rust crates above to Go 1.23.0.
- Add a Go module at the working directory `/workspace/repo`, declared as `module github.com/servo/rust-url` with `go 1.23`.
- The Go implementation MUST implement the migrated behavior itself, using only the Go standard library.
- Production Go code MUST NOT call the Rust crates, `cargo`, or any Rust binary, and MUST NOT use cgo, FFI, subprocesses, RPC, or dynamic loading to perform migrated responsibilities.
- Out of scope: the `data-url` and `url_debug_tests` crates, the Natvis debugger visualizer, CI workflows, fuzz targets, benchmarks, tester harnesses, and the optional `serde` Serialize/Deserialize surface.

## Public contract to preserve

Create these Go packages under the module above. The Rust crate behavior they replace is given only to identify the migrated responsibilities.

| Import path | Directory | Replaces |
| --- | --- | --- |
| `github.com/servo/rust-url/url` | `url/` | `url` |
| `github.com/servo/rust-url/idna` | `idna/` | `idna` |
| `github.com/servo/rust-url/idna/punycode` | `idna/punycode/` | `idna::punycode` |
| `github.com/servo/rust-url/percentencoding` | `percentencoding/` | `percent-encoding` |
| `github.com/servo/rust-url/formurlencoded` | `formurlencoded/` | `form_urlencoded` |

The signatures below define the required public surface; internal representation and helper declarations are free.

### `url` package

- `type URL` is a parsed URL record. Every method below is defined on `*URL`.
- `func Parse(input string) (*URL, error)` parses an absolute URL.
- `func (u *URL) Join(input string) (*URL, error)` resolves `input` as a relative or absolute reference against `u`.
- `func (u *URL) AsStr() string` returns the URL Standard serialization.
- Component accessors: `Scheme() string`, `Username() string`, `Password() (string, bool)`, `HasHost() bool`, `HostStr() (string, bool)`, `Host() (Host, bool)`, `Domain() (string, bool)`, `Port() (uint16, bool)`, `PortOrKnownDefault() (uint16, bool)`, `Path() string`, `PathSegments() ([]string, bool)`, `Query() (string, bool)`, `QueryPairs() *formurlencoded.Parser`, `Fragment() (string, bool)`, `CannotBeABase() bool`, `IsSpecial() bool`, `HasAuthority() bool`, `Authority() string`, `Origin() Origin`.
- Mutators: `SetFragment(fragment *string)`, `SetQuery(query *string)`, `SetPath(path string)`, `SetPort(port *uint16) error`, `SetHost(host *string) error`, `SetUsername(username string) error`, `SetPassword(password *string) error`, `SetScheme(scheme string) error`.
- `func (u *URL) QueryPairsMut() *QueryMutator`, with `AppendPair(name, value string) *QueryMutator`, `AppendKeyOnly(name string) *QueryMutator`, `ExtendPairs(pairs [][2]string) *QueryMutator`, and `Clear() *QueryMutator`, each returning the mutator for chaining, and `Finish() *URL` returning the URL with the pending query rewrite applied.
- `func (u *URL) PathSegmentsMut() (*PathSegmentsMut, error)`, with `Clear()`, `PopIfEmpty()`, `Pop()`, `Push(segment string)`, and `Extend(segments []string)`, each returning the mutator for chaining.
- `type HostKind uint8` with `HostDomain`, `HostIPv4`, `HostIPv6`. `type Host` exposes `Kind() HostKind`, `Domain() string`, `IP() net.IP`, and `String() string`. `func ParseHost(input string) (Host, error)` and `func ParseOpaqueHost(input string) (Host, error)` parse hosts.
- `type Origin` with `func NewOpaqueOrigin() *Origin`, `IsTuple() bool`, `ASCIISerialization() string`, and `UnicodeSerialization() string`.
- Parse-failure sentinels, comparable with `errors.Is`: `ErrEmptyHost`, `ErrIdna`, `ErrInvalidPort`, `ErrInvalidIPv4Address`, `ErrInvalidIPv6Address`, `ErrInvalidDomainCharacter`, `ErrRelativeURLWithoutBase`, `ErrRelativeURLWithCannotBeABaseBase`, `ErrSetHostOnCannotBeABaseURL`, `ErrOverflow`.

### `idna` package

- `func DomainToASCII(domain string) (string, error)`, equivalent to `domain_to_ascii` (no ASCII deny list, hyphens allowed, DNS length ignored).
- `func DomainToASCIIStrict(domain string) (string, error)`, equivalent to `domain_to_ascii_strict`.
- `func DomainToUnicode(domain string) (string, error)`: the returned string is meaningful even when the error is non-nil.
- `var ErrInvalidDomain error`, returned for input rejected by UTS #46 processing.

### `idna/punycode` package

- `func Encode(input []rune) (string, bool)`, `func EncodeString(input string) (string, bool)`, `func Decode(input string) ([]rune, bool)`, `func DecodeToString(input string) (string, bool)`. The boolean reports success.

### `percentencoding` package

- `type ASCIIStringSet` with value semantics; `var Controls ASCIIStringSet` and `var NonAlphanumeric ASCIIStringSet`; methods `Add(b byte) ASCIIStringSet`, `Remove(b byte) ASCIIStringSet`, `Union(other ASCIIStringSet) ASCIIStringSet`, `Complement() ASCIIStringSet`.
- `func PercentEncodeByte(b byte) string`.
- `func PercentEncode(input []byte, set *ASCIIStringSet) string` and `func UTF8PercentEncode(input string, set *ASCIIStringSet) string`.
- `func Decode(input []byte) *PercentDecode` and `func DecodeString(input string) *PercentDecode`.
- `type PercentDecode` with `Bytes() []byte`, `DecodeUTF8() (string, error)`, and `DecodeUTF8Lossy() string`.
- `type UTF8Error` with `Error() string`, `ValidUpTo() int`, and `ErrorLen() (int, bool)`.

### `formurlencoded` package

- `func Parse(input []byte) *Parser` and `func ParseString(input string) *Parser`; `type Parser` with `Next() (name string, value string, ok bool)`.
- `func ByteSerialize(input []byte) string`.
- `type Serializer` with `func NewSerializer(target *string) *Serializer`, `AppendPair(name, value string)`, `AppendKeyOnly(name string)`, `ExtendPairs(pairs [][2]string)`, `ExtendKeysOnly(keys []string)`, `Clear()`, and `Finish() string`.

## Behavior requirements

- Parsing a valid absolute URL MUST return a record whose `AsStr`, component accessors, and `PathSegments` match the WHATWG URL Standard. For `https://github.com/rust-lang/rust/issues?labels=E-easy&state=open`: scheme `https`, username `""`, password absent, `HostStr` `github.com`, host a domain `github.com`, port absent, path `/rust-lang/rust/issues`, segments `rust-lang`, `rust`, `issues`, query `labels=E-easy&state=open`, fragment absent, `CannotBeABase` false.
- A cannot-be-a-base URL MUST report no host, port, or path segments. For `data:text/plain,Hello?World#`: `CannotBeABase` true, scheme `data`, path `text/plain,Hello`, `PathSegments` ok false, query `World`, fragment `""`.
- `Parse("http://[:::1]")` MUST return a nil record and an error satisfying `errors.Is(err, ErrInvalidIPv6Address)`.
- Parsing a relative reference without a base MUST return `ErrRelativeURLWithoutBase`. `Join` MUST resolve relative references per the URL Standard; joining `../main.css` against `http://servo.github.io/rust-url/url/index.html` MUST yield `http://servo.github.io/rust-url/main.css`.
- `ParseHost` on `[...]` input MUST parse an IPv6 address and MUST use lowercase hexadecimal with maximal `::` compression (RFC 5952) for its `String` and for `HostStr`; a malformed bracket or IPv6 address MUST return `ErrInvalidIpv6Address`.
- `Port()` MUST return the explicitly serialized port only; `PortOrKnownDefault()` MUST return the known default for `http`/`ws` (80), `https`/`wss` (443), and `ftp` (21) when no port is present. Setting a port equal to the scheme's default MUST leave it absent from `AsStr`.
- `SetPath` MUST percent-encode characters that are not already percent-encoded and MUST NOT double percent-encode existing `%XX` sequences: setting `api/some comments` yields `/api/some%20comments`, and setting `api/some%20comments` yields the same path.
- `PathSegmentsMut` MUST keep the path `/`-separated with at least one segment, MUST ignore segments equal to `.` or `..`, and MUST percent-encode `%` and `/` inside a segment: pushing `2/100%.png` after `pop` yields `/2%2F100%25.png`. Query and fragment MUST be unchanged after mutation.
- `QueryPairsMut` MUST rewrite the query in place, separating appended pairs with `&`, and MUST leave the URL fragment unchanged. `Clear` followed by append MUST behave like setting an empty query, not removing the query.
- `QueryPairs` and `formurlencoded.Parse` MUST split the input on `&` and each pair on the first `=`, treat `+` as a space, percent-decode names and values, and decode invalid UTF-8 lossily. `foo=bar&baz` MUST yield pairs `("foo","bar")` and `("baz","")`; `%23first=%25try%25` MUST yield `("#first","%try%")`.
- `PercentEncode` and `UTF8PercentEncode` MUST percent-encode every non-ASCII byte and every byte contained in the supplied set, leaving other bytes unchanged: `PercentEncode([]byte("foo bar?"), &NonAlphanumeric)` MUST equal `foo%20bar%3F`, and `PercentEncodeByte` MUST produce `%XX` with uppercase hexadecimal digits. `Add` MUST return a set that additionally encodes the given byte without mutating the receiver.
- `Decode(...).DecodeUTF8()` MUST decode valid UTF-8 and MUST return a `*UTF8Error` for invalid percent-decoded bytes. For `%00%9F%92%96`, `ValidUpTo()` MUST be `1` and `ErrorLen()` MUST be `(1, true)`. `DecodeUTF8Lossy` MUST replace each invalid sequence with U+FFFD.
- `DomainToASCII` MUST canonicalize valid domains (case folding, UTS #46 mapping, Punycode encoding when needed) and MUST return `ErrInvalidDomain` for invalid input. `DomainToASCIIStrict` MUST additionally apply the STD3 deny list, hyphen checking, and DNS-length verification. `DomainToUnicode` MUST normalize and Punycode-decode valid domains, and MUST still return a replacement-character rendering alongside a non-nil error for invalid input.
- Punycode `Encode`/`Decode` MUST implement RFC 3492 and report the boolean as false for overflow or invalid input.
- `URL.Origin()` MUST return an opaque origin when the scheme is not `blob`, `ftp`, `http`, `https`, `ws`, or `wss` (including `file`), and otherwise a scheme/host/port tuple; `blob` origins MUST derive from the URL in the path or be opaque on failure. `ASCIISerialization` MUST be `null` for an opaque origin and MUST omit a port equal to the scheme's default.

## Constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production Go code MUST NOT import, call, or otherwise use the Rust crates for the migrated responsibilities.
3. Production Go code MUST NOT delegate the migrated responsibilities to the Rust runtime through cgo, FFI, subprocesses, RPC, or dynamic loading.
4. The Go implementation MUST build using only the Go standard library; it MUST NOT add third-party module dependencies.
5. Changes MUST be limited to adding the Go module (Go source files and `go.mod`) and Go test assets under `/workspace/repo`.
6. The out-of-scope crates and assets (`data-url`, `url_debug_tests`, the Natvis visualizer, CI workflows, fuzz targets, benchmarks, tester harnesses, and the `serde` feature) MUST NOT be migrated or modified.
7. The existing Rust crates may remain in the repository, but they MUST NOT be modified to affect the outcome of the Go implementation.
8. The public conformance data files `url/tests/urltestdata.json`, `url/tests/setters_tests.json`, `idna/tests/IdnaTestV2.txt`, `idna/tests/punycode_tests.json`, and `idna/tests/bad_punycode_tests.json` are protected inputs; you MUST NOT modify them or hardcode their expected values.
9. You MUST NOT skip checks, remove assertions, weaken comparisons, or special-case inputs to fake compliance.
10. The working directory is `/workspace/repo`. The evaluation collects the repository state as a patch relative to its initial baseline, so keep all new and modified files in the repository working tree.
