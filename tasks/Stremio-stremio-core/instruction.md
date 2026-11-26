# Migrate the stremio-core engine from Rust to Go

## Background

`stremio-core` is a Rust library (edition 2021, MSRV `1.77`) that implements the Stremio client engine: a single-directional update runtime, the addon protocol client, deep-link generation, and persistence of user state. Host applications embed the engine and exchange serialized messages with it. The engine implementation is being ported to Go.

## Goal

- Migrate the engine currently implemented in these modules from Rust (edition 2021, MSRV `1.77`) to Go `1.23.0`:
  - `src/lib.rs`, `src/constants.rs`
  - `src/runtime/` (`mod.rs`, `runtime.rs`, `env.rs`, `effects.rs`, `update.rs`, `msg/mod.rs`, `msg/msg.rs`, `msg/action.rs`, `msg/event.rs`, `msg/internal.rs`)
  - `src/models/` (`mod.rs`, `ctx/mod.rs`, `ctx/ctx.rs`, `ctx/update_library.rs`, `ctx/update_notifications.rs`, `ctx/update_search_history.rs`, `ctx/update_streaming_server_urls.rs`, `player.rs`, `streaming_server.rs`)
  - `src/addon_transport/` (`mod.rs`, `http_transport/mod.rs`, `http_transport/legacy/mod.rs`, `unsupported_transport.rs`)
  - `src/types/mod.rs`
- The Go implementation MUST provide the migrated responsibilities: the update/effects runtime, the message and event types, the `Ctx` and model state, the player and streaming-server models, the addon transports, and persistence.
- The migrated responsibilities MUST be implemented in Go, not delegated back to the Rust engine.
- Out of scope: `stremio-core-web` and all browser/client UI, Web Worker, and `wasm-bindgen`/`web-sys`/`js-sys` binding code. They MUST NOT be migrated or modified.

## Go module, package layout and public API contract

The migration is delivered as a Go module, and the evaluation compiles and runs its checks against the migrated packages. The module path, the package layout and the exported identifiers below are fixed parts of the contract: a port that is behaviorally correct but uses a different module path, a different package/file layout, or different public names fails the checks even when the runtime behavior is equivalent.

### Module and build

- The Go module path MUST be exactly `github.com/stremio/stremio-core` (all lowercase). The upstream repository URL is `https://github.com/Stremio/stremio-core` (capital `S`), but the Go module path and every import path MUST use lowercase `stremio`. Do NOT declare `github.com/Stremio/stremio-core`, a bare name, or a `-go` suffix.
- `go.mod` MUST declare exactly:
  ```
  module github.com/stremio/stremio-core

  go 1.23.0
  ```
- The module MUST build standalone with `CGO_ENABLED=0`, and `go list -deps ./...` MUST resolve only `github.com/stremio/stremio-core/...` and the Go standard library. No external module, cgo/FFI, subprocess, RPC, or dynamic-loading dependency is allowed.

### Package and import layout

The following packages MUST exist (directory → import path → package name):

| Directory | Import path | Package name |
|---|---|---|
| `.` | `github.com/stremio/stremio-core` | `stremiocore` |
| `runtime` | `github.com/stremio/stremio-core/runtime` | `runtime` |
| `runtime/msg` | `github.com/stremio/stremio-core/runtime/msg` | `msg` |
| `models` | `github.com/stremio/stremio-core/models` | `models` |
| `models/common` | `github.com/stremio/stremio-core/models/common` | `common` |
| `models/ctx` | `github.com/stremio/stremio-core/models/ctx` | `ctx` |
| `types` | `github.com/stremio/stremio-core/types` | `types` |
| `addon_transport` | `github.com/stremio/stremio-core/addon_transport` | `addontransport` |
| `deep_links` | `github.com/stremio/stremio-core/deep_links` | `deeplinks` |
| `analytics` | `github.com/stremio/stremio-core/analytics` | `analytics` |
| `internal/testenv` | `github.com/stremio/stremio-core/internal/testenv` | `testenv` |

The evaluation's migration-scope check requires the migrated production source to live at these exact paths. Every listed `.go` file MUST exist and carry at least one substantive top-level declaration; `*_test.go` files are not required.

- root: `constants.go`
- `runtime/`: `runtime.go`, `update.go`, `effects.go`, `env.go`
- `runtime/msg/`: `action.go`, `event.go`, `internal.go`, `msg.go`
- `models/`: `model.go`, `api.go`, `resources.go`, `player.go`, `streaming_server.go`
- `models/common/`: `loadable.go`, `resource_loadable.go`, `eq_update.go`
- `models/ctx/`: `ctx.go`, `error.go`, `update_events.go`, `update_library.go`, `update_notifications.go`, `update_profile.go`, `update_search_history.go`, `update_streaming_server_urls.go`, `update_streams.go`
- `types/`: `types.go`, `api.go`, `player.go`, `resource.go`, `user_state.go`, `query_params_encode.go`, `streaming_server.go`, `torrent.go`, `rating.go`, `watched_bitfield.go`, `official_addons.go`, `official_addons.json`
- `addon_transport/`: `transport.go`, `http_transport.go`, `legacy.go`, `legacy_manifest.go`
- `deep_links/`: `deep_links.go`, `external_player.go`
- `analytics/`: `analytics.go`
- `internal/testenv/`: `env.go`
- module file: `go.mod`

### Fixed exported API

The names and shapes below are compiled against by the checks. (Package-qualified notation; the package alias is shown where it differs from the directory name.)

**`github.com/stremio/stremio-core` (package `stremiocore`)**

- Storage-key constants: `SCHEMA_VERSION_STORAGE_KEY="schema_version"`, `PROFILE_STORAGE_KEY="profile"`, `LIBRARY_STORAGE_KEY="library"`, `LIBRARY_RECENT_STORAGE_KEY="library_recent"`, `STREAMS_STORAGE_KEY="streams"`, `SEARCH_HISTORY_STORAGE_KEY="search_history"`, `STREAMING_SERVER_URLS_STORAGE_KEY="streaming_server_urls"`, `NOTIFICATIONS_STORAGE_KEY="notifications"`, `CALENDAR_STORAGE_KEY="calendar"`, `DISMISSED_EVENTS_STORAGE_KEY="dismissed_events"`.
- `SCHEMA_VERSION` = `25`.

**`.../runtime`**

- `type RuntimeEvent struct { Name string; Args json.RawMessage }`
- `func NewStateEvent(fields []string) RuntimeEvent`
- `func CoreEvent(event *msg.Event) RuntimeEvent`
- `type RuntimeAction struct { Field *string; Action *msg.Action }`
- `type Runtime struct { ... }`; `func New(model Model, effects []Effect, buffer int, env Env) (*Runtime, <-chan RuntimeEvent)`
- `func (r *Runtime) Model() Model`; `func (r *Runtime) Dispatch(action RuntimeAction)`
- `type Model interface { Update(m *msg.Msg) ([]Effect, []string); UpdateField(m *msg.Msg, field string) ([]Effect, []string) }`
- `type Effect struct { Msg *msg.Msg; Future *EffectFuture }`; `type EffectFuture struct { Concurrent bool; Run func() *msg.Msg }`; `func MsgEffect(*msg.Msg) Effect`
- `type Effects struct { Effects []Effect; HasChanged bool }`
- `type Env interface { Fetch(method, url string, headers map[string]string, body []byte) ([]byte, *EnvError); GetStorage(key string) (json.RawMessage, bool, *EnvError); SetStorage(key string, value any) *EnvError; ExecConcurrent(f func()); ExecSequential(f func()); Now() time.Time; LocalNow() time.Time; FlushAnalytics(); AnalyticsContext(ctx any, streamingServer any, path string) any; Log(message string); AddonTransport(transportURL string) AddonTransport; MigrateStorageSchema() *EnvError }`
- `type AddonTransport interface { Resource(path types.ResourcePath) (types.ResourceResponse, *EnvError); Manifest() (types.Manifest, *EnvError) }`
- `type EnvError struct { Code int; Message string }` (serializes as `{"code":<int>,"message":<string>}`)
- `func EnvErrorFetch(string) *EnvError`, `func EnvErrorAddonTransport(string) *EnvError`, `func EnvErrorSerde(string) *EnvError`, `func EnvErrorStorageUnavailable() *EnvError`, `func EnvErrorStorageSchemaVersionDowngrade(from, to uint32) *EnvError`, `func EnvErrorStorageSchemaVersionUpgrade(source *EnvError) *EnvError`, `func EnvErrorStorageReadError(string) *EnvError`, `func EnvErrorStorageWriteError(string) *EnvError`, `func EnvErrorOther(string) *EnvError`
- Code constants: `EnvErrorCodeFetch=1`, `EnvErrorCodeAddonTransport=2`, `EnvErrorCodeSerde=3`, `EnvErrorCodeStorageUnavailable=4`, `EnvErrorCodeStorageSchemaVersionDowngrade=5`, `EnvErrorCodeStorageSchemaVersionUpgrade=6`, `EnvErrorCodeStorageReadError=7`, `EnvErrorCodeStorageWriteError=8`, `EnvErrorCodeOther=1001`
- `func MigrateStorageSchema(env Env) *EnvError` (migrates v1..v25, downgrade returns code 5)

**`.../runtime/msg`**

- `type Kind int`; constants `KindAction`, `KindInternal`, `KindEvent`
- `type Msg struct { Kind Kind; Action *Action; Internal *Internal; Event *Event }`
- `func ActionMsg(*Action) *Msg`, `func InternalMsg(*Internal) *Msg`, `func EventMsg(*Event) *Msg`
- `type Action struct { Action string; Args json.RawMessage }` (wire `{"action":...,"args":...}`)
- `func NewAction(name string, args any) *Action`, `func LoadAction(model string, args any) *Action` (wire `{"action":"Load","args":{"model":...,"args":...}}`), `func UnloadAction() *Action`
- `type Event struct { Event string; Args json.RawMessage }` (wire `{"event":...,"args":...}`); `func NewEvent(name string, args any) *Event`
- `type Internal struct { Name string; Data any }`; `func NewInternal(name string, data any) *Internal`
- `type ResourceRequestResult struct { Request any; Value any; Err error }`
- Constants: `ModelPlayer="Player"`, `ActionNameUnload="Unload"`, `ActionNamePlayer="Player"`, `ActionPlayerMarkVideoAsWatched="MarkVideoAsWatched"`, `InternalProfileChanged="ProfileChanged"`.

**`.../models`**

- `func New(env runtime.Env) *Model`
- `type Model struct { Ctx *ctx.Ctx; Player *Player; StreamingServer *StreamingServer }`
- `func (m *Model) Update(message *msg.Msg) ([]runtime.Effect, []string)`; `func (m *Model) UpdateField(message *msg.Msg, field string) ([]runtime.Effect, []string)`
- Field constants `FieldCtx="ctx"`, `FieldStreamingServer="streaming_server"`, `FieldPlayer="player"` (reported in the stable order `ctx`, `streaming_server`, `player`)
- `type Selected struct { Stream types.Stream; StreamRequest *types.ResourceRequest; MetaRequest *types.ResourceRequest }`
- `type Player struct { ...; NextVideo *types.Video; LibraryItem *types.LibraryItem; Watched *types.WatchedBitField; ... }`

**`.../models/common` (package `common`)**

- `type Loadable[T any] struct { ... }`; `func Loading[T any]() Loadable[T]`, `func Ready[T any](value T) Loadable[T]`, `func Err[T any](err error) Loadable[T]`; methods `IsReady() bool`, `IsErr() bool`, `IsLoading() bool`
- `type ResourceLoadable[T any] struct { Request types.ResourceRequest; Content *Loadable[T] }`
- `type ResourceResult struct { Response *types.ResourceResponse; Err *runtime.EnvError }`
- `type ResourceAction struct { Request *types.ResourceRequest; Result *ResourceResult }`; `func ResourceRequested(request *types.ResourceRequest) ResourceAction`, `func ResourceRequestResult(request *types.ResourceRequest, result ResourceResult) ResourceAction`
- `func ResourceUpdate[T any](env runtime.Env, resource *ResourceLoadable[T], action ResourceAction, convert func(types.ResourceResponse) (T, error)) runtime.Effects`
- `func EqUpdate[T any](value *T, next T) runtime.Effects`

**`.../models/ctx` (package `ctx`)**

- `type Ctx struct { Profile types.Profile; Library types.LibraryBucket; Notifications types.NotificationsBucket; Streams types.StreamsBucket; StreamingServerURLs types.ServerUrlsBucket; SearchHistory types.SearchHistoryBucket; DismissedEvents types.DismissedEventsBucket; ... }`

**`.../types` (package `types`)**

- `type ExtraValue struct { Name string; Value string }`; `type ResourcePath struct { Resource string; Type string; ID string; Extra []ExtraValue }`; `type ResourceRequest struct { Base string; Path ResourcePath }`
- `type ResourceResponse struct { Raw []byte }`; `type Manifest struct { ... }`
- `type QueryPair struct { Key string; Value string }`; `func QueryParamsEncode(pairs []QueryPair) string`
- `type StreamSource struct { Kind string; URL string; ... }`; constant `StreamSourceURL = "url"`; `type Stream struct { Source StreamSource; ... }`; `type Video struct { ID string; Title string; ... }`
- `type WatchedBitField struct { ... }`; `func WatchedBitFieldConstructAndResize(serialized string, videoIDs []string) (WatchedBitField, error)`; methods `GetVideo(videoID string) bool`, `SetVideo(videoID string, v bool)`, `String() string`
- `type StreamsItemKey struct { MetaID string; VideoID string }`; `type StreamsItem struct { Stream Stream; Type string; MetaID string; VideoID string; ... }`; `type StreamsBucket struct { UID UID; Items map[StreamsItemKey]StreamsItem }`; `func NewStreamsBucket(uid UID) StreamsBucket` (wire `{"uid":...,"items":[...]}`)
- Persistence constructors and types: `NewProfile() Profile`, `NewLibraryBucket(uid UID, items []LibraryItem) LibraryBucket`, `NewNotificationsBucket(now time.Time, uid UID, items []NotificationItem) NotificationsBucket`, `NewSearchHistoryBucket(uid UID) SearchHistoryBucket`, `NewServerUrlsBucket(now time.Time, uid UID) ServerUrlsBucket`, `NewDismissedEventsBucket(uid UID) DismissedEventsBucket`
- `type Profile struct { ..., Settings Settings, ... }`; `type LibraryItem struct { ID string; Name string; Type string; Mtime time.Time; State LibraryItemState; ... }`; `type LibraryItemState struct { ..., TimeOffset uint64, Watched *string, ... }`; `type NotificationItem struct { MetaID string; VideoID string; VideoReleased time.Time }`; `type LibraryBucket`, `type NotificationsBucket`, `type SearchHistoryBucket`, `type ServerUrlsBucket`, `type DismissedEventsBucket`

**`.../addon_transport` (package `addontransport`)**

- `func BuildLegacyReq(transportURL string, path types.ResourcePath) (string, *runtime.EnvError)`
- `type LegacyErr struct { Kind LegacyErrKind; Code int; Message string }`; constant `LegacyJSONRPC`; `func (e *LegacyErr) ToEnvError() *runtime.EnvError`
- `type JSONRPCResp struct { ... }`; `func (r JSONRPCResp) Parse() (json.RawMessage, *LegacyErr)`
- `func NewAddonHTTPTransport(transportURL string, env runtime.Env) *AddonHTTPTransport` (implements `Resource`/`Manifest`)
- `func NewUnsupportedTransport(transportURL string) *UnsupportedTransport` (implements `Resource`/`Manifest`)
- `func NewAddonLegacyTransport(transportURL string, env runtime.Env) *AddonLegacyTransport`

**`.../deep_links` (package `deeplinks`)**

- `func NewSearchHistoryItemDeepLinks(query string) SearchHistoryItemDeepLinks` (field `Search string`)
- `func NewDiscoverDeepLinks(request types.ResourceRequest) DiscoverDeepLinks` (field `Discover string`)
- `func NewMetaItemDeepLinks(typ, id string) MetaItemDeepLinks` (fields `MetaDetailsVideos *string`, `MetaDetailsStreams *string`, `Player *string`)
- `func NewMetaItemDeepLinksWithVideo(typ, id, defaultVideoID string) MetaItemDeepLinks`
- `func NewCalendarDeepLinks(year, month int) CalendarDeepLinks` (field `Calendar string`)
- `func NewLibraryDeepLinks(root string, typ *string, sort string) LibraryDeepLinks` (field `Library string`)

**`.../analytics` (package `analytics`)**

- `func New(env runtime.Env, sendFn func(Batch) error) *Analytics`
- `type Batch struct { AuthKey string; Events []Event }`; `type Event struct { ..., Name string, ... }`
- `func (a *Analytics) Emit(name string, data map[string]any, authKey string, context any)`; `func (a *Analytics) Flush()`; `func (a *Analytics) QueueLen() int`; `func (a *Analytics) SendNextBatch()`

**`.../internal/testenv` (package `testenv`)**

- `func New() *Env`; `type Env struct { Storage map[string]json.RawMessage; FetchFn func(method, url string, headers map[string]string, body []byte) ([]byte, *runtime.EnvError); Transport runtime.AddonTransport }`; `Env` implements `runtime.Env`.

## Public contract to preserve

- `RuntimeEvent` MUST serialize as `{"name": "NewState"|"CoreEvent", "args": ...}`: `NewState` carries the changed model field list, `CoreEvent` carries an `Event`.
- `Action` values MUST keep their wire representation with `action`/`args` tags, `Event` values with `event`/`args`, and model-load actions with `model`/`args`.
- `EnvError` MUST serialize as `{"code": <integer>, "message": <string>}` with codes `Fetch=1`, `AddonTransport=2`, `Serde=3`, `StorageUnavailable=4`, `StorageSchemaVersionDowngrade=5`, `StorageSchemaVersionUpgrade=6`, `StorageReadError=7`, `StorageWriteError=8`, `Other=1001`.
- Persisted values MUST keep their storage key names and their serde-JSON string encoding. The protected keys are `schema_version`, `profile`, `library`, `library_recent`, `streams`, `search_history`, `streaming_server_urls`, `notifications`, `calendar`, and `dismissed_events`; the latest schema version is `25`.
- The engine MUST keep the equivalent public surface of the `addon_transport`, `analytics`, `deep_links`, `models`, `runtime`, and `types` modules, including the model construction/derivation entry point.
- The runtime MUST be constructible from a model, an initial effect list, and a buffer size, returning the runtime together with a receiver of `RuntimeEvent`.
- The runtime MUST expose a read accessor for the current model and a dispatch operation taking an optional model field plus an action: a present field routes to field-only update, an absent field updates the whole model.
- The model abstraction MUST keep `update` (whole model) and `update_field` (single field) operations with the same effect and changed-field outputs.
- The `Env` abstraction MUST keep the equivalent of `fetch`, `get_storage`, `set_storage`, `exec_concurrent`, `exec_sequential`, `now`, `local_now`, and analytics operations.
- The legacy addon transport MUST keep its JSON-RPC wire protocol and error mapping: a JSON-RPC error becomes an addon-transport error formatted `rpc error {code}: {message}`, and unsupported resources/requests keep their existing messages.

## Behavior requirements

- After dispatch, the runtime MUST emit `NewState` only when a model field changed, carrying the changed field set in a stable order; two identical `Player` loads MUST each emit one `NewState`.
- An internal message MUST update the model and continue processing the effects it returns; an event message MUST emit only a `CoreEvent`; an action returned by an effect MUST panic.
- The legacy transport MUST build a request by base64-encoding the JSON-RPC body into `{transport_url}/q.json?b=<param>`, and MUST deserialize `{"result": ...}` and `{"error": {"message": ..., "code": ...}}` responses.
- `Loadable` and `ResourceLoadable` models MUST keep their loading states and changed-field notifications when `Env::fetch` succeeds or fails.
- Values persisted through storage MUST round-trip at the serde-JSON string level for profile, library, notifications, search history, streaming server urls, streams, and dismissed events.
- The storage-schema migration MUST upgrade the persisted user state instead of discarding it. After a profile persisted by an earlier schema version is migrated to the latest version, the profile MUST still carry the equivalent settings under the names used by the current schema (a setting persisted under a legacy name MUST NOT be dropped and a non-empty profile MUST NOT be emptied); every setting introduced by the schema versions that were traversed MUST be present afterwards with its schema-defined value; and each persisted bucket whose corresponding schema version removed it MUST be cleared. A downgrade of the storage schema version MUST still be reported as a downgrade error.
- `deep_links` MUST generate the same per-platform URL shapes (`stremio:///detail/...`, `/player/...`, `/discover/...`, `/addons/...`, `/library...`, `/calendar/...`, `/search?...`) and the same query-parameter encoding.

## Constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production code within the migration scope MUST NOT import or call the Rust implementation for the migrated responsibilities.
3. The migrated responsibilities MUST NOT be delegated to a Rust runtime through cgo, FFI, subprocesses, RPC, or dynamic loading.
4. Changes MUST be limited to the in-scope modules and the Go build configuration; `stremio-core-web`, browser/Web Worker code, and the `wasm-bindgen`/`web-sys`/`js-sys` bindings MUST NOT be migrated or modified.
5. Source-framework dependencies that support only out-of-scope responsibilities MAY remain, provided they are not used for the migrated responsibilities.
6. The protected storage keys and encoded values, the `RuntimeEvent`/`Action`/`Event` wire formats, and the legacy JSON-RPC wire protocol and error text MUST NOT be changed.
7. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
8. The model's working directory is `/workspace/repo`.
9. The evaluation collects the resulting repository change set from `/workspace/repo`; keep all migrated sources and Go build configuration in that directory.

## Validation

From `/workspace/repo`, the repository's public checks are:

```
cargo fmt --all -- --check
cargo clippy --all --no-deps -- -D warnings
cargo test
cargo build
```

These are the repository's existing public formatting, lint, unit-test, and build checks. They exercise the current implementation and do not by themselves prove that every requirement above is satisfied.
