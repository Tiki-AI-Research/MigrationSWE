# Migrate chess-tui core modules from Rust to Go

## Background

chess-tui is a terminal chess program currently implemented in Rust (edition 2024). Its configuration handling, PGN parsing and playback, chess board model, network opponent, UCI bot client, TCP multiplayer server, and Lichess client are provided by Rust modules. The repository is provided at the task's starting baseline revision.

## Goal

- Migrate the responsibilities implemented by `src/lib.rs`, `src/config.rs`, `src/pgn_viewer.rs`, `src/game_logic/game_board.rs`, `src/game_logic/opponent.rs`, `src/game_logic/bot.rs`, `src/server/game_server.rs`, `src/lichess/game.rs`, and `src/lichess/stream.rs` from Rust (edition 2024) to Go 1.25.0.
- The Go 1.25.0 implementation MUST provide the migrated responsibilities natively: configuration loading and persistence, PGN parsing and navigation, the chess board model, the TCP network opponent, the UCI bot client, the TCP multiplayer server, and the Lichess REST/SSE client.
- The deliverable MUST be a single Go module with module path `github.com/thomas-mauran/chess-tui`, a command entry point in `main`, and the migrated responsibilities exposed under the `internal/` packages `internal/config` (CLI parsing, configuration loading and persistence), `internal/pgn` (PGN parsing and navigation), `internal/chess` (board model and coordinates), `internal/tcp` (network opponent and multiplayer server), `internal/uci` (UCI bot client), `internal/lichess` (Lichess REST/SSE client), `internal/skin` (skin and colour model), and `internal/constants` (shared constants and config-directory resolution). The behavioural checks compile Go tests against this module path and package layout, so the migrated behaviour MUST be reachable there.

## Required Go API surface

The behavioural checks compile Go tests against the module path, package layout and the Go declarations
listed below. The checks call these identifiers by name, so a migration that implements the same behaviour
under different names or signatures is not reachable by them. The exported declarations below (types and
their exported fields, functions, methods, constants and variables) MUST exist with exactly these names and
signatures. Because the checks are compiled into the `main` and `internal/lichess` packages, the two
`main`-package entry-point helpers and the `internal/lichess` seek-error helper below MUST also exist.

```go
// main package helpers used by the checks
func run(arguments []string, stdin io.Reader, stdout, stderr io.Writer) int
func loadPGN(path string, stderr io.Writer) ([]*pgn.Viewer, error)

// internal/lichess helper used by the checks
func seekError(status int, body string) error

// package config
func Create(args *Args, folderPath, configPath string) error
func HandleWriteError(err error, path string)
func IsReadonlyError(err error) bool
func ParseDifficulty(name string) (uint8, bool)
type Args struct {
	EnginePath   string
	Depth        *uint8
	Difficulty   *string
	LichessToken *string
	NoSound      bool
	Skin         *string
	UpdateSkins  bool
	Pgn          *string
}
func ParseArgs(arguments []string) (*Args, error)
type Config struct {
	EnginePath        *string `toml:"engine_path,omitempty"`
	DisplayMode       *string `toml:"display_mode,omitempty"`
	LogLevel          *string `toml:"log_level,omitempty"`
	BotDepth          *uint8  `toml:"bot_depth,omitempty"`
	BotDifficulty     *uint8  `toml:"bot_difficulty,omitempty"`
	SelectedSkinName  *string `toml:"selected_skin_name,omitempty"`
	LichessToken      *string `toml:"lichess_token,omitempty"`
	SoundEnabled      *bool   `toml:"sound_enabled,omitempty"`
	AnimationsEnabled *bool   `toml:"animations_enabled,omitempty"`
}
func Default() *Config
func Load(path string) *Config
func (c *Config) Persist(path string) error

// package pgn
const DefaultSpeedIdx = 1
var Speeds = []Speed{
	{0.5, "0.5x"},
	{1.0, "1x"},
	{1.5, "1.5x"},
	{2.0, "2x"},
	{2.5, "2.5x"},
	{3.0, "3x"},
	{4.0, "4x"},
}
type Speed struct {
	Rate  float32
	Label string
}
type Viewer struct {
	// Positions[i] is the position after Moves[i-1]; Positions[0] is the start.
	Positions []*chesslib.Position
	Moves     []*chesslib.Move
	SANs      []string
	// CurrentPly is the currently shown ply (0 = start).
	CurrentPly int
	// Auto-play state.
	AutoPlay      bool
	SpeedIdx      int
	AutoPlayAccum float32
	// EndBannerDismissed is set when the end-of-game banner is hidden.
	EndBannerDismissed bool
	// Metadata from the PGN headers.
	Title       string
	White       string
	Black       string
	Result      string
	Termination string
}
func FromFile(path string) ([]*Viewer, error)
func FromPGNString(pgn string) ([]*Viewer, error)
func (v *Viewer) CurrentPosition() *chesslib.Position
func (v *Viewer) GotoEnd()
func (v *Viewer) GotoStart()
func (v *Viewer) IsAtEnd() bool
func (v *Viewer) LastMove() *chesslib.Move
func (v *Viewer) Next()
func (v *Viewer) Prev()
func (v *Viewer) ResultSummary() string
func (v *Viewer) SpeedDown()
func (v *Viewer) SpeedLabel() string
func (v *Viewer) SpeedUp()
func (v *Viewer) Tick()
func (v *Viewer) TotalPlies() int

// package chess
type Coord struct {
	Row uint8
	Col uint8
}
func FromSquare(sq chesslib.Square) Coord
func NewCoord(row, col uint8) Coord
func (c *Coord) MoveTo(direction MoveDirection)
func (c Coord) Reverse() Coord
func (c Coord) Square() chesslib.Square
type GameBoard struct {
	// MoveHistory holds the moves that produced each position.
	MoveHistory []*chesslib.Move
	// PositionHistory holds the past states; the last one is the current
	// position unless navigating.
	PositionHistory []*chesslib.Position
	// ConsecutiveNonPawnOrCapture counts half-moves since the last pawn move
	// or capture.
	ConsecutiveNonPawnOrCapture int
	// TakenPieces stores captured pieces.
	TakenPieces []chesslib.Piece
	// IsFlipped tracks whether the board is displayed flipped.
	IsFlipped bool
	// HistoryPositionIndex is the current index when navigating history;
	// nil means viewing the latest position.
	HistoryPositionIndex *int
	// OriginalFlipState stores the flip state before navigating history.
	OriginalFlipState *bool
}
func NewGameBoard() *GameBoard
func (b *GameBoard) BlackTakenPieces() []chesslib.PieceType
func (b *GameBoard) CurrentPosition() *chesslib.Position
func (b *GameBoard) ExecuteMove(from, to chesslib.Square, promotion chesslib.PieceType) *chesslib.Move
func (b *GameBoard) ExecuteShakmatyMove(from, to chesslib.Square) *chesslib.Move
func (b *GameBoard) ExecuteShakmatyMoveWithPromotion(from, to chesslib.Square, promotion chesslib.PieceType) bool
func (b *GameBoard) FEN() string
func (b *GameBoard) FlipTheBoard()
func (b *GameBoard) GetAuthorizedPositions(playerTurn chesslib.Color, sq chesslib.Square) []chesslib.Square
func (b *GameBoard) GetKingCoordinates(color chesslib.Color) (Coord, bool)
func (b *GameBoard) GetPieceColorAtSquare(sq chesslib.Square) (chesslib.Color, bool)
func (b *GameBoard) GetRoleAtSquare(sq chesslib.Square) (chesslib.PieceType, bool)
func (b *GameBoard) IncrementConsecutiveNonPawnOrCapture(roleFrom chesslib.PieceType, roleTo *chesslib.PieceType)
func (b *GameBoard) IsCheckmate() bool
func (b *GameBoard) IsDraw() bool
func (b *GameBoard) IsDrawByRepetition() bool
func (b *GameBoard) IsGettingChecked(playerTurn chesslib.Color) bool
func (b *GameBoard) IsSquareOccupied(sq chesslib.Square) bool
func (b *GameBoard) MoveToSAN(moveIndex int) string
func (b *GameBoard) NavigateHistoryNext(isSoloMode bool) bool
func (b *GameBoard) NavigateHistoryPrevious(isSoloMode bool) bool
func (b *GameBoard) PositionRef() *chesslib.Position
func (b *GameBoard) ReconstructHistory(movesStr string, expectedFEN *string)
func (b *GameBoard) Reset()
func (b *GameBoard) ResetHistoryNavigation(isSoloMode bool)
func (b *GameBoard) TruncateHistoryAt(index int)
func (b *GameBoard) WhiteTakenPieces() []chesslib.PieceType
type MoveDirection int
const (
	// Up moves towards rank 8.
	Up MoveDirection = iota
	// Down moves towards rank 1.
	Down
	// Left moves towards file a.
	Left
	// Right moves towards file h.
	Right
)

// package tcp
func GetColorFromStream(conn net.Conn) (chesslib.Color, error)
func GetHostIP() (net.IP, error)
func PromotionSuffix(promotion chesslib.PieceType) string
func SquareString(sq chesslib.Square) string
func WaitForGameStart(o *Opponent) (bool, error)
type GameServer struct {
	IsHostWhite bool

	// Has unexported fields.
}
func NewGameServer(isHostWhite bool) *GameServer
func SetupGameServer(hostColor chesslib.Color) *GameServer
func (s *GameServer) BroadcastMessage(message string, sender net.Conn)
func (s *GameServer) HandleClient(conn net.Conn)
func (s *GameServer) RemoveClient(addr string)
func (s *GameServer) Run()
func (s *GameServer) Serve(listener net.Listener)
func (s *GameServer) Stop()
type Opponent struct {
	OpponentWillMove bool
	Color            chesslib.Color
	GameStarted      bool
	InitialMoveCount int
	MovesReceived    int
	// Has unexported fields.
}
func New(addr string, color *chesslib.Color) (*Opponent, error)
func (o *Opponent) Close() error
func (o *Opponent) Conn() net.Conn
func (o *Opponent) ReadStream() (string, error)
func (o *Opponent) SendEndGameToServer()
func (o *Opponent) SendMoveToServer(from, to chesslib.Square, promotion chesslib.PieceType)

// package uci
type Bot struct {
	EnginePath    string
	BotWillMove   bool
	IsBotStarting bool
	// Depth is the base depth used when difficulty is off.
	Depth uint8
	// Difficulty preset index: nil = off (full strength), 0..3 otherwise.
	Difficulty *uint8
}
func New(enginePath string, isBotStarting bool, depth uint8, difficulty *uint8) *Bot
func (b *Bot) EffectiveDepth() uint8
func (b *Bot) Elo() (uint16, bool)
func (b *Bot) GetMove(fen string) (string, error)
func (b *Bot) MovetimeMs() (int, bool)

// package lichess
func ParseGameColor(colorStr string) chesslib.Color
func SendStatusMessage(status string, moveTx chan<- string)
type Client struct {
	Token   string
	BaseURL string
	HTTP    *http.Client
}
func NewClient(token string) *Client
func (c *Client) GetGameInfo(gameID string) (int, *string, error)
func (c *Client) GetOngoingGames() ([]OngoingGame, error)
func (c *Client) JoinGame(gameID, myID string) (string, chesslib.Color, error)
func (c *Client) MakeMove(gameID, move string) error
func (c *Client) ResignGame(gameID string) error
func (c *Client) SeekGame(timeMs, increment uint32, cancel *atomic.Bool) (string, chesslib.Color, error)
func (c *Client) SpawnEventStream(initial map[string]struct{}, cancel *atomic.Bool, gameFound chan<- GameFound)
func (c *Client) SpawnGameStream(gameID string, moveTx chan<- string, playerColor *chesslib.Color)
func (c *Client) StreamGame(gameID string, moveTx chan<- string, playerColor *chesslib.Color) error
type EventStreamEvent struct {
	Type string           `json:"type"`
	Game *EventStreamGame `json:"game"`
}
type EventStreamGame struct {
	GameID string `json:"gameId"`
	Color  string `json:"color"`
}
type GameEvent struct {
	Type   string     `json:"type"`
	ID     string     `json:"id"`
	White  Player     `json:"white"`
	Black  Player     `json:"black"`
	State  *GameState `json:"state"`
	Moves  string     `json:"moves"`
	Status string     `json:"status"`
}
type GameFound struct {
	GameID string
	Color  chesslib.Color
	Err    error
}
type GameState struct {
	Moves  string `json:"moves"`
	Status string `json:"status"`
}
type OngoingGame struct {
	GameID   string       `json:"gameId"`
	FullID   string       `json:"fullId"`
	Color    string       `json:"color"`
	FEN      string       `json:"fen"`
	Opponent OpponentInfo `json:"opponent"`
	IsMyTurn bool         `json:"isMyTurn"`
}
type OngoingGamesResponse struct {
	NowPlaying []OngoingGame `json:"nowPlaying"`
}
type OpponentInfo struct {
	ID       string  `json:"id"`
	Username string  `json:"username"`
	Rating   *uint16 `json:"rating"`
}
type Player struct {
	ID       string  `json:"id"`
	Username string  `json:"username"`
	Rating   *uint16 `json:"rating"`
}

// package skin
func CreateDefaultSkinsFile(path string) error
func DefaultSkins() []byte
func RunUpdateSkins(in *bufio.Reader, out *os.File) error
func SkinsPath() (string, error)
type Color struct {
	Name string
	Rgb  *[3]uint8
}
func NamedColor(name string) Color
func RgbColor(r, g, b uint8) Color
func (c Color) MarshalJSON() ([]byte, error)
func (c *Color) UnmarshalJSON(data []byte) error
type PieceStyle struct {
	Name     string         `json:"name"`
	Small    PieceStyleSize `json:"small"`
	Compact  PieceStyleSize `json:"compact"`
	Extended PieceStyleSize `json:"extended"`
	Large    PieceStyleSize `json:"large"`
}
func LoadAllPieceStyles(path string) ([]PieceStyle, error)
type PieceStyleCollection struct {
	PieceStyles []PieceStyle `json:"piece_styles"`
}
type PieceStyleSize struct {
	Bishop string `json:"bishop"`
	King   string `json:"king"`
	Knight string `json:"knight"`
	Pawn   string `json:"pawn"`
	Queen  string `json:"queen"`
	Rook   string `json:"rook"`
}
type Skin struct {
	Name            string `json:"name"`
	PieceStyle      string `json:"piece_style"`
	BoardWhiteColor Color  `json:"board_white_color"`
	BoardBlackColor Color  `json:"board_black_color"`
	PieceWhiteColor Color  `json:"piece_white_color"`
	PieceBlackColor Color  `json:"piece_black_color"`
	CursorColor     Color  `json:"cursor_color"`
	SelectionColor  Color  `json:"selection_color"`
	LastMoveColor   Color  `json:"last_move_color"`
}
func ASCII() Skin
func Default() Skin
func GetSkinByName(skins []Skin, name string) (Skin, bool)
func LoadAllSkins(path string) ([]Skin, error)
type SkinCollection struct {
	Skins []Skin `json:"skins"`
}

// package constants
const BotDifficultyCount = 4
const LichessAPIURL = "https://lichess.org/api"
const LichessUserAgent = "chess-tui (https://github.com/thomas-mauran/chess-tui)"
const NetworkBufferSize = 5
const NetworkPort uint16 = 2308
const SkinNameASCII = "ASCII"
const SkinNameDefault = "Default"
const SleepDurationLongMs = 100
const SleepDurationResignMs = 500
const SleepDurationShortMs = 50
var BotDifficultyDepth = [4]uint8{1, 4, 8, 20}
var BotDifficultyElo = [4]uint16{400, 900, 1500, 2700}
var BotDifficultyMovetimeMs = [4]int{25, 120, 500, 12000}
var BotDifficultyNames = [4]string{"Easy (400)", "Medium (900)", "Hard (1500)", "Magnus (2700)"}
func ConfigDir() (string, error)
```

- The TUI presentation and input layer is out of scope: `ratatui`, `crossterm`, `tachyonfx`, `throbber-widgets-tui`, and the rendering logic in `src/ui/**`, `src/pieces/**`, `src/color.rs`, and `src/animations.rs` MUST NOT be ported. The absence of a terminal UI in the Go implementation is not a missing responsibility.
- The external UCI chess engine process (for example Stockfish or GNU Chess) and the Lichess HTTP API are allowed external dependencies and are not part of the source framework.

## Public contract to preserve

- CLI arguments: `-e`/`--engine-path`, `-d`/`--depth`, `--difficulty`, `-l`/`--lichess-token`, `--no-sound`, `--skin`, `--update-skins`, `-p`/`--pgn`.
- CLI precedence: command-line values override configuration-file values for engine path, depth, difficulty, Lichess token, and sound.
- Configuration file `CONFIG_DIR/chess-tui/config.toml` with fields `engine_path`, `display_mode`, `log_level`, `bot_depth`, `bot_difficulty`, `selected_skin_name`, `lichess_token`, `sound_enabled`, and `animations_enabled`, including the read-only-filesystem tolerance described below. The external file formats (`config.toml` and `skins.json`) MUST NOT change.
- UCI engine contract: handshake, `UCI_LimitStrength`/`UCI_Elo` options, `position` with FEN, `go depth`/`go movetime`, and `bestmove` parsing.
- TCP multiplayer contract: color token on join, start token `s`, move-string forwarding, `ended` broadcast, and the full-response message.
- PGN contract: multi-game splitting, header extraction (`Event`, `White`, `Black`, `Result`, `Termination`), SAN move application, and stop-on-illegal-move.
- Lichess REST/SSE contract: User-Agent and bearer authentication, event and board stream parsing, and 400/401/403/429 error mapping and cancellation.

## Behavior requirements

- If `config.toml` is missing, empty, or not valid TOML, the application MUST continue with default values and MUST NOT crash: `display_mode` = `"DEFAULT"`, `log_level` = `"OFF"`, `bot_depth` = 10, `selected_skin_name` = `"Default"`, `sound_enabled` = true, `animations_enabled` = false, and no engine path, difficulty, or token.
- If writing `config.toml` fails because the filesystem is read-only (EROFS, errno 30) or permission is denied, the failure MUST only be logged as a warning and MUST NOT propagate to the caller.
- When persisting runtime settings, the existing file MUST be re-read first so that fields not tracked by the running state, such as `engine_path`, are preserved.
- `--difficulty` MUST map `easy` → 0, `medium` → 1, `hard` → 2, and `magnus` → 3, case-insensitively; an unrecognized value MUST leave the difficulty unchanged.
- Bot depth MUST be handled as an 8-bit unsigned value (0-255).
- When a difficulty preset is selected, the bot MUST use the preset depth `[1, 4, 8, 20]`, ELO `[400, 900, 1500, 2700]`, and movetime `[25, 120, 500, 12000]` ms. With no difficulty (full strength), the bot MUST use the base depth with no movetime and no ELO limit.
- The engine path MAY carry extra arguments, for example `"/usr/bin/gnuchess --uci"`; the first whitespace-separated token is the executable and the remainder are its arguments.
- The bot MUST complete the UCI handshake before sending options or the position, MUST set `UCI_LimitStrength` and `UCI_Elo` when a difficulty ELO applies, MUST send the position as FEN, MUST send `go depth <n>` and/or `go movetime <ms>`, and MUST return the engine `bestmove`. Spawn, handshake, parse, and computation failures MUST be reported as errors rather than a move.
- PGN file loading MUST read the file and, on failure, return the error `Cannot read '<path>': <err>`. A PGN string with multiple games MUST be split so that each game is parsed, only valid games are collected, and an input with no valid games MUST return `No valid games found in PGN`.
- PGN headers MUST map `Event` to the title, plus `White`, `Black`, `Result` (default `"*"`), and `Termination`; the title, white, and black values default to `"?"`.
- PGN movetext MUST remove `{...}` comments and (nested) `(...)` variations, skip move numbers, `$n` NAG tokens, and result tokens, strip trailing `!`/`?`, tolerate `+`/`#` suffixes, and stop applying moves at the first unparsable or illegal move. A game with no parsed moves MUST be rejected with `No moves parsed from game '<title>'`.
- PGN navigation: advancing one ply MUST clamp at the final position; stepping back MUST clamp at the start and clear the end-banner-dismissed flag; jump-to-start MUST go to ply 0 and clear that flag; jump-to-end MUST go to the final ply; at-end MUST be true only when the game has at least one move and the current ply is the final ply.
- `result_summary` MUST return `<white> (White) won` for `"1-0"`, `<black> (Black) won` for `"0-1"`, `Draw` for `"1/2-1/2"`, and the raw result otherwise.
- Auto-play speeds are the fixed multipliers 0.5x, 1x, 1.5x, 2x, 2.5x, 3x, and 4x; increasing or decreasing the speed MUST clamp at the ends, and a tick MUST advance one ply when the accumulator reaches one.
- The TCP multiplayer server MUST listen on `0.0.0.0` port `2308`.
- On join, the joining player MUST receive its color token (`w` or `b`), and the other connected player MUST receive the start token `s`. When two players are already connected, any additional connection MUST receive `Game is already full` and MUST NOT be admitted.
- Moves MUST be exchanged as `<from><to>` strings plus an optional promotion suffix (`q`, `r`, `b`, or `n`); the TCP read buffer is 5 bytes.
- A message from one player MUST be forwarded to the other player and MUST NOT be echoed back to the sender. When a client disconnects or sends `ended`, the server MUST broadcast `ended` to the remaining player and stop.
- Reading `ended` from a peer MUST surface as the error `Game ended by the other opponent`. A color token other than `w` or `b` MUST surface as `Failed to get color from stream`. Waiting for the start signal MUST accept only `s` and MUST report "not started" when the non-blocking socket has no data.
- All Lichess requests MUST target `https://lichess.org/api`, MUST send the `User-Agent` value `chess-tui (https://github.com/thomas-mauran/chess-tui)`, and MUST authenticate with the bearer token.
- Seeking MUST open the event stream `/stream/event` before creating the seek, then detect a `gameStart` whose game id was not present before the seek. A seek is a correspondence request (`days` = 3) when time and increment are both 0, and a real-time request with `time`/`increment` otherwise. Errors MUST map as: 403 → `Token missing permissions. Please generate a new token with 'board:play' scope enabled.`; 429 → `Rate limit exceeded. Please wait a minute before trying again.`; 401 → `Invalid token. Please check your token or generate a new one.`; 400 → `Invalid seek parameters: <body>`.
- Joining MUST prefer the color from the player's ongoing games; otherwise it MUST attempt `POST /challenge/{id}/accept` and poll for up to 30 seconds. Errors MUST map as: 403 → `Cannot join this game. You may not be a participant.`; 401 → `Invalid token. Please check your token or generate a new one.`.
- `make_move` MUST use `POST /board/game/{id}/move/{move}` and, on a non-success response, return `Failed to make move: <status>`. Resigning MUST use `POST /board/game/{id}/resign` and, on failure, return `Failed to resign game: <status> - <body>`.
- The board stream (`/board/game/stream/{id}`) MUST parse `gameFull` and `gameState`, track the move count, and emit only newly arrived moves. Status changes MUST emit `GAME_STATUS:checkmate` for `mate`/`checkmate`, `GAME_STATUS:draw` for `draw`/`stalemate`/`repetition`/`insufficient`/`fifty`, `GAME_STATUS:resign` for `resign`, and `GAME_STATUS:aborted` for `aborted`. `chatLine` events MUST be ignored, empty lines are keep-alive, a 404 MUST stop the stream, and other endings MUST reconnect after 5 seconds.
- The event stream (`/stream/event`) MUST handle `gameStart`, `gameFinish`, `challenge`, `challengeCanceled`, and `challengeDeclined`, treat empty lines as keep-alive, and reconnect after 5 seconds.
- Fetching game info MUST use `/stream/game/{id}`, read the first line, and return the move count (`turns`, default 0) and the last move (`lastMove`), erroring on an empty or unparsable response.
- When `--pgn` points to a directory, every `.pgn` file MUST be loaded in sorted path order; if none yields a valid game, an error MUST be written to stderr and the process MUST exit with status 1. A `--pgn` file that fails to load MUST likewise write an error to stderr and exit with status 1.

## Constraints

1. The migration MUST NOT remove required public capabilities or change the contracts in this document outside the stated migration scope.
2. The Go implementation of the migrated responsibilities MUST NOT call, embed, or wrap the Rust implementation (no cgo, FFI, dynamic loading, subprocess, or reuse of the original Rust binary or crate).
3. Production code for the migrated responsibilities MUST NOT delegate them to a Rust runtime.
4. Changes MUST be limited to the Go source, Go module and dependency configuration, and the supporting assets required for these responsibilities.
5. The external UCI engine process and the Lichess HTTP API are allowed external dependencies and are not the source framework.
6. The external formats and contracts MUST be preserved: the CLI option names and precedence, the `CONFIG_DIR/chess-tui/config.toml` field set and semantics, the `skins.json` format, the TCP wire protocol, the PGN behavior, and the Lichess REST/SSE contract.
7. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
8. You MUST NOT modify or bypass protected public assets: evaluation assets, public test fixtures, and the public behavior contracts listed above. If a public test must be adapted to the Go implementation, its behavioral assertions MUST remain intact.
9. Work in `/workspace/repo`. The repository is provided at the task's starting baseline revision; do not reset the checkout to another branch. All changes required for the deliverable MUST be present in this working tree, which is collected as a patch relative to that baseline.
