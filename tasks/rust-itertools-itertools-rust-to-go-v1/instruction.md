# Migrate the itertools iterator-extension library from Rust to Go

## Background

The repository contains `itertools` (crate version 0.15.0, Rust edition 2018, minimum supported Rust 1.63.0), a library of extra iterator adaptors, iterator methods, free functions, and macros built on the standard `Iterator` protocol. It exposes an `Itertools` extension trait that adds adaptor and query methods to every `Iterator`, free functions in `src/free.rs` that accept any `IntoIterator`, concrete adaptor structs, error types, and the `iproduct!`, `izip!`, and `chain!` macros. It supports `use_std`, `use_alloc`, and `no_std` configurations.

This task re-implements the library's public iterator behavior in Go 1.23.0.

## Goal

- Migrate the iterator-extension library from Rust edition 2018 (MSRV Rust 1.63.0) to Go 1.23.0.
- The Go implementation MUST provide the adaptor and query methods of the `Itertools` extension trait, the free functions in `src/free.rs`, the concrete adaptor and error types, and the behaviors currently supplied by the `iproduct!`, `izip!`, and `chain!` macros.
- The migration scope covers these production modules: `src/lib.rs`, `src/adaptors/mod.rs`, `src/adaptors/map.rs`, `src/adaptors/coalesce.rs`, `src/adaptors/multi_product.rs`, `src/free.rs`, `src/size_hint.rs`, `src/format.rs`, `src/concat_impl.rs`, `src/array_impl.rs`, `src/either_or_both.rs`, `src/groupbylazy.rs`, `src/grouping_map.rs`, `src/merge_join.rs`, `src/process_results_impl.rs`, `src/put_back_n_impl.rs`, `src/peek_nth.rs`, `src/next_array.rs`, `src/intersperse.rs`, `src/with_position.rs`, `src/minmax.rs`, `src/k_smallest.rs`, `src/kmerge_impl.rs`, `src/permutations.rs`, `src/combinations.rs`, `src/unique_impl.rs`, `src/exactly_one_err.rs`, `src/all_equal_value_err.rs`, `src/lazy_buffer.rs`, `src/rciter_impl.rs`, `src/take_while_inclusive.rs`, and `src/pad_tail.rs`.
- The behavior tests `tests/quick.rs`, `tests/laziness.rs`, `tests/specializations.rs`, `tests/test_core.rs`, `tests/test_std.rs`, `tests/adaptors_no_collect.rs`, and `tests/arrays.rs` are in scope as the behavioral reference.
- The `benches/` and `examples/` trees and the repository CI configuration are out of scope and MUST NOT be migrated.
- The Go implementation MUST implement the iterator logic itself. Production code within scope MUST NOT obtain the migrated behavior from the Rust crate or the Rust runtime.
- The iterator representation is an implementation detail; the delivered Go form MUST expose the behaviors described below.

## Public contract to preserve

- The Go implementation MUST expose operations equivalent to every public operation of the Rust `Itertools` trait and the `src/free.rs` free functions. Their behavior MUST NOT change. The exported Go identifiers MUST use the PascalCase projection of the Rust public name, and the exact names and signatures fixed in the declarations below take precedence over that projection.
- The `EitherOrBoth` type MUST keep its `Left`, `Right`, and `Both` variants and their payloads.
- `concat` MUST combine the elements of an iterator into one element by extension. An empty input MUST return the element type's default value.
- The window adaptors MUST preserve the fixed window-size contract of `ArrayWindows` and `CircularArrayWindows`.
- `AllEqualValueError` and `ExactlyOneError` MUST keep their observable error semantics and MUST implement the standard error interface when the standard library is available. Their `Display` text MUST stay exactly:
  - `AllEqualValueError` with zero elements: `got zero elements when all elements were expected to be equal`.
  - `AllEqualValueError` with two unequal elements: `got different elements when all elements were expected to be equal`.
  - `ExactlyOneError` with two or more elements: `got at least 2 elements when exactly one was expected`.
  - `ExactlyOneError` with zero elements: `got zero elements when exactly one was expected`.
- `Format` and `FormatWith` MUST render their elements lazily with the given separator. They MUST be formattable only once; a second formatting attempt MUST fail.
- Adaptors MUST implement the standard iterator protocol: they produce the same items, in the same order and count, and their size hints MUST report exact lower and upper bounds. The double-ended, exact-size, and fused capabilities exposed by an adaptor MUST be preserved.
- The API availability boundaries of the `no_std`, `use_alloc`, and `use_std` configurations MUST be preserved: operations that require heap allocation or hashing MUST remain segregated from those that do not.
- The migrated project MUST be a single Go module whose import path is `itertools`, so its public surface is imported as `itertools`, `itertools/core`, and `itertools/free`. The module MUST build and test offline using only the Go 1.23.0 standard library.
- The `no_std`/`use_alloc`/`use_std` availability boundaries MUST be projected onto Go build constraints behind build tags named exactly `nostd` and `noalloc`, and the default build MUST include the full surface. Both `go build -tags nostd ./...` and `go build -tags noalloc ./...` MUST succeed using only the Go 1.23.0 standard library.
  - Under `-tags nostd` the standard-library hashing operations MUST be excluded while the remaining operations stay available; the excluded group includes `Unique`, `Duplicates`, `Counts`, and `IntoGroupingMap`.
  - Under `-tags noalloc` the heap-allocation operations MUST be excluded while the remaining operations stay available; the excluded group includes `Iproduct`, `Combinations`, `Permutations`, `Powerset`, and `Sorted`.
- The public surface MUST be split by responsibility across those three packages: `itertools/core` owns the iterator protocol, the source constructors, the size-hint value and its arithmetic, the shared element and outcome value types, and the concrete adaptor and error types; the module-root `itertools` package owns the extension operations and the behaviors of the `iproduct!`, `izip!`, and `chain!` macros; `itertools/free` owns the `src/free.rs` free functions. Every operation is a package-level generic function.
- The iterator protocol MUST use Go's two-result form: a value satisfies `core.Iter[T]` by exposing `Next() (T, bool)` and `SizeHint() core.SizeHint`. `core.SizeHint` carries `Lower` and `Upper` bounds, is constructed with `core.Hint(lower, upper int)`, reports exactness with `IsExact()` and boundedness with `Bounded()`, and supports the size-hint arithmetic `core.Add`, `core.AddScalar`, `core.SubScalar`, `core.Mul`, `core.MulScalar`, `core.Min`, and `core.Max`. An infinite lower bound is the maximum `int` and an absent upper bound is `-1`. `core.DoubleEnded[T]` adds `NextBack() (T, bool)` and `core.Fused[T]` marks a fused iterator; each adaptor MUST carry the capabilities inherited from the corresponding Rust type.
- The `itertools/core` package MUST expose the iterator protocol and shared building blocks with exactly these names and signatures (declarations are shown as they appear inside `core`, so `Iter` and `SizeHint` are unqualified; a type parameter is constrained by `any` unless its constraint is written out):
  - `type Iter[T any] interface { Next() (T, bool); SizeHint() SizeHint }`, `type DoubleEnded[T any] interface { Iter[T]; NextBack() (T, bool) }`, and `type Fused[T any] interface { Iter[T] }`.
  - `type SizeHint struct { Lower int; Upper int }`, built by `func Hint(lo, hi int) SizeHint`, with `func (s SizeHint) IsExact() bool`, `func (s SizeHint) Bounded() bool`, `func Add(a, b SizeHint) SizeHint`, `func AddScalar(sh SizeHint, x int) SizeHint`, `func SubScalar(sh SizeHint, x int) SizeHint`, `func Mul(a, b SizeHint) SizeHint`, `func MulScalar(sh SizeHint, x int) SizeHint`, `func Min(a, b SizeHint) SizeHint`, and `func Max(a, b SizeHint) SizeHint`.
  - `type Slice[T any]` with `func FromSlice[T any](s []T) *Slice[T]`; `type Repeat[T any]` with `func RepeatIter[T any](v T) *Repeat[T]`; `type RepeatN[T any]` with `func NewRepeatN[T any](n int, v T) *RepeatN[T]`; `type Iterate[T any]` with `func NewIterate[T any](seed T, next func(T) T) *Iterate[T]`.
  - `type Pair[A, B any] struct { First A; Second B }`.
  - `type EitherOrBoth[A, B any]` with `func Both[A, B any](a A, b B) EitherOrBoth[A, B]`, `func Left[A, B any](a A) EitherOrBoth[A, B]`, and `func Right[A, B any](b B) EitherOrBoth[A, B]`, and the methods `IsLeft() bool`, `IsRight() bool`, `IsBoth() bool`, `HasLeft() bool`, `HasRight() bool`, `JustLeft() (A, bool)`, `JustRight() (B, bool)`, `Left() (A, bool)`, and `Right() (B, bool)`.
  - `type MinMaxResult[T any] struct { Kind MinMaxResultKind; Min T; Max T }`, `type MinMaxResultKind int`, and the constants `MinMaxNoElements`, `MinMaxOneElement`, and `MinMaxPair`.
  - `type ArrayWindows[T any]` with `func NewArrayWindows[T any](it Iter[T], n int) *ArrayWindows[T]`; `type CircularArrayWindows[T any]` with `func NewCircularArrayWindows[T any](it Iter[T], n int) *CircularArrayWindows[T]`.
  - `type Format[T any]` with `func NewFormat[T any](it Iter[T], sep string, render func(T) string) *Format[T]` and `func (f *Format[T]) String() string`; `type FormatWith[T any]` with `func NewFormatWith[T any](it Iter[T], sep string, fn FormatWithFunc[T]) *FormatWith[T]` and `func (f *FormatWith[T]) String() string`; and `type FormatWithFunc[T any] func(v T, write func(any) string) string`.
  - `type AllEqualValueError[T any]` with `func NewAllEqualValueErrorZero[T any]() *AllEqualValueError[T]`, `func NewAllEqualValueErrorUnequal[T any](a, b T) *AllEqualValueError[T]`, and `func (e *AllEqualValueError[T]) Error() string`; `type ExactlyOneError[T any]` with `func (e *ExactlyOneError[T]) Error() string`.
  - `type Map[T, U any]` with `func NewMap[T, U any](it Iter[T], f func(T) U) *Map[T, U]`.
  - Every other concrete adaptor and error type reached by the operations below MUST also be exported from `core` under its PascalCase name and implement `Iter[T]`, together with the remaining sources `EmptyIter`, `OnceIter`, `Unfold`, and `NewUnfold`.
- The module-root `itertools` package MUST expose these operations with exactly these names and signatures (shown as seen from the module root, so `core.` qualifies the `itertools/core` package):
  - `func AllEqualValue[T any](it core.Iter[T], eq func(a, b T) bool) (T, error)`
  - `func ArrayWindows[T any](it core.Iter[T], n int) *core.ArrayWindows[T]`
  - `func ChainConcatenates[T any](iters ...core.Iter[T]) *core.ChainMany[T]`
  - `func Chunks[T any](it core.Iter[T], size int) *core.Chunks[T]`
  - `func CircularArrayWindows[T any](it core.Iter[T], n int) *core.CircularArrayWindows[T]`
  - `func CollectVec[T any](it core.Iter[T]) []T`
  - `func Concat[T any](it core.Iter[[]T]) []T`
  - `func Counts[T any, K comparable](it core.Iter[T], key func(T) K) map[K]int`
  - `func Dedup[T any](it core.Iter[T], eq func(a, b T) bool) *core.Dedup[T]`
  - `func ExactlyOne[T any](it core.Iter[T]) (T, *core.ExactlyOneError[T])`
  - `func Filter[T any](it core.Iter[T], pred func(T) bool) *core.Filter[T]`
  - `func Fold[T, A any](it core.Iter[T], init A, f func(A, T) A) A`
  - `func Format[T any](it core.Iter[T], sep string) *core.Format[T]`
  - `func FormatWith[T any](it core.Iter[T], sep string, fn core.FormatWithFunc[T]) *core.FormatWith[T]`
  - `func Interleave[T any](a, b core.Iter[T]) *core.Interleave[T]`
  - `func InterleaveShortest[T any](a, b core.Iter[T]) *core.InterleaveShortest[T]`
  - `func Intersperse[T any](it core.Iter[T], elt T) *core.Intersperse[T]`
  - `func Iproduct[T any](iters ...core.Iter[T]) *core.MultiProduct[T]`
  - `func Izip[T any](iters ...core.Iter[T]) *MultiZip[T]`
  - `func KMerge[T cmp.Ordered](iters []core.Iter[T]) *core.KMergeBy[T]`
  - `func KSmallest[T cmp.Ordered](it core.Iter[T], k int) []T`
  - `func Map[T, U any](it core.Iter[T], f func(T) U) *core.Map[T, U]`
  - `func Merge[T cmp.Ordered](a, b core.Iter[T]) *core.MergeBy[T]`
  - `func MinmaxByKey[T any, K cmp.Ordered](it core.Iter[T], key func(T) K) core.MinMaxResult[T]`
  - `func PositionMax[T interface{ ~int | ~int8 | ~int16 | ~int32 | ~int64 | ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~float32 | ~float64 | ~string }](it core.Iter[T]) (core.Pair[int, T], bool)`
  - `func PositionMin[T interface{ ~int | ~int8 | ~int16 | ~int32 | ~int64 | ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~float32 | ~float64 | ~string }](it core.Iter[T]) (core.Pair[int, T], bool)`
  - `func PositionMinMax[T cmp.Ordered](it core.Iter[T]) core.MinMaxResult[int]`
  - `func Product1[T Number](it core.Iter[T]) (T, bool)`
  - `func Sorted[T cmp.Ordered](it core.Iter[T]) []T`
  - `func Sum1[T Number](it core.Iter[T]) (T, bool)`
  - `func ZipEq[A, B any](a core.Iter[A], b core.Iter[B]) *core.ZipEq[A, B]`
  - `type MultiZip[T any]` with `func (z *MultiZip[T]) Next() ([]T, bool)` and `func (z *MultiZip[T]) SizeHint() core.SizeHint`, and `type Number interface { ~int | ~int8 | ~int16 | ~int32 | ~int64 | ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~float32 | ~float64 }`.
  - The package MUST also expose every remaining public operation of the Rust `Itertools` trait and the `iproduct!`, `izip!`, and `chain!` macros as package-level generic functions: `Zip`, `ZipLongest`, `ChunkBy`, `GroupBy`, `FilterMap`, `IntersperseWith`, `MergeBy`, `MergeJoinBy`, `KMergeBy`, `Coalesce`, `WithPosition`, `PadUsing`, `PutBackN`, `PeekNth`, `MultiPeek`, `Tee`, `Dropping`, `DroppingBack`, `Tail`, `TakeWhileInclusive`, `WhileSome`, `Batching`, `Enumerate`, `Rev`, `All`, `AllEqual`, `AllUnique`, `Any`, `AtMostOne`, `Contains`, `Fold1`, `FoldOK`, `FoldWhile`, `DiffWith`, `Positions`, `Minmax`, `MinmaxBy`, `Unique`, `Duplicates`, `IntoGroupingMap`, `PartitionMap`, `PartitionResult`, `FlattenOK`, `TryCollect`, `SortedBy`, `KSorted`, `KLargest`, `Combinations`, `CombinationsWithReplacement`, `Permutations`, `Powerset`, `CartesianProduct`, `MultiCartesianProduct`, and `Chain`.
- The `itertools/free` package MUST expose these free functions with exactly these names and signatures:
  - `func Iterate[T any](seed T, next func(T) T) *core.Iterate[T]`
  - `func ProcessResults[T any, R any](it core.Iter[core.Pair[T, error]], processor func(core.Iter[T]) R) (R, error)`
  - The package MUST also expose every remaining free function from `src/free.rs` under its PascalCase name: `All`, `Any`, `Chain`, `Cloned`, `Concat`, `ConsTuples`, `DiffWith`, `Enumerate`, `Fold`, `Intersperse`, `IntersperseWith`, `Join`, `KMergeBy`, `Max`, `Min`, `Multiunzip`, `Multizip`, `RepeatN`, `Rev`, `Sorted`, `SortedUnstable`, `Unfold`, and `Zip`.
- For each package above, importing it and calling the named identifiers MUST return the migrated behavior described in this instruction.

## Behavior that must stay unchanged

- Interleaving `[1, 2]` with `[-1, -2]` MUST yield `[1, -1, 2, -2]`. `interleave` MUST alternate until both inputs are exhausted; `interleave_shortest` MUST alternate until at least one input has run out, keeping the trailing element already selected from the longer input (interleaving `[1, 2, 3]` with `[-1, -2]` MUST yield `[1, -1, 2, -2, 3]`).
- `concat([[1], [2, 3], [4, 5, 6]])` MUST return `[1, 2, 3, 4, 5, 6]`; an empty input MUST return the default element value.
- `all_equal_value` MUST return `AllEqualValueError` when the input has zero elements or unequal elements, and MUST return the single element as success when all elements are equal.
- `array_windows` with window size `N` MUST return exactly `k - N + 1` windows for `k` input items when `N >= 1`. When `N == 0` it MUST return `k + 1` empty windows and MUST NOT write into the ring buffer. When `k < N` it MUST return no windows.
- Merging the sorted inputs `[0, 3, 6, 9]` and `[0, 5, 10]` MUST yield `[0, 0, 3, 5, 6, 9, 10]`. `merge`, `merge_by`, `merge_join_by`, `kmerge`, and `kmerge_by` MUST merge already-sorted inputs in order and MUST NOT drop duplicate elements.
- An adaptor MUST be lazy: until it is consumed, it MUST NOT produce effects. `FormatWith` MUST fail when formatted a second time.
- Specialized bulk operations such as `fold` and `collect` MUST produce the same output as element-by-element processing.
- `zip_eq` MUST panic when the two inputs have unequal lengths. `chunks` MUST panic when the chunk size is `0`.
- `dedup` MUST remove only runs of consecutive equal elements.
- `exactly_one` MUST return the single element only when the input has exactly one element; otherwise it MUST return an error that restores the elements already consumed from the input.

## constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production Go code within scope MUST NOT import, link, call, or otherwise use the Rust `itertools` crate for the migrated responsibilities.
3. Production code within scope MUST NOT delegate the migrated responsibilities to the Rust implementation through subprocesses, RPC, FFI, cgo, or dynamic loading.
4. Changes MUST be limited to the migrated library sources, a Go module and build files, and supporting Go tests or fixtures.
5. The `benches/` and `examples/` trees and the repository CI configuration are out of scope and MUST NOT be changed to carry the migration.
6. The semantics expressed by the public operation and type names MUST NOT change. In particular: the standard iterator protocol observable results (`next`, `size_hint`, `fold`, `collect`); the empty-`concat` default and the `all_equal_value` zero/unequal error semantics; adaptor laziness and the one-shot `FormatWith` contract; and the `no_std`/`use_alloc`/`use_std` API boundaries.
7. Preserving the Rust source text, the macro internals, or the literal form of generic monomorphization is NOT required.
8. The existing Rust tests listed in the migration scope are the behavioral reference. Adapting them to Go MAY be done, but their behavioral assertions MUST be preserved.
9. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
10. Work in `/workspace/repo`. The task starts from the provided base commit; do not replace it with `main` or another branch. The evaluation collects the changes left in the repository working tree relative to that starting commit as a patch, so keep every source, module, and supporting file needed by the migration in that working tree.
11. From `/workspace/repo`, the existing public Rust baseline is `cargo test --all-features`, `cargo miri nextest run --all-features`, and `cargo miri test --doc`. These commands exercise the Rust reference behavior, including the property tests, the laziness assertions, and the specialization behavior; they do not verify the Go implementation.
12. The starting repository has no public Go test command. Add focused Go tests for the migrated behavior and run `go build ./...` and `go test ./...` from the directory containing your Go module. These checks cover compilation and the scenarios they exercise only; they do not by themselves prove every requirement above.

IMPORTANT: Create a new branch from the task's base commit fd619332f002979c3d7958fe0fc45509f84fdb8d and commit all task changes, including new files, when you are done.
