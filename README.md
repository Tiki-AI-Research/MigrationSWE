# MigrationSWE

40 repository migration tasks, testing both the migration and compatibility with the original implementation.

[Run tasks](#quick-start) · [Leaderboard](#2-leaderboard) · [Task design](#3-task-design) · [Evaluation method](#4-evaluation-method) · [Results by task](#6-results-by-task) · [Error analysis](#7-error-analysis) · [Full report](https://tikiai.co/benchmarks/migration-swe)

## Quick start

Use a Linux x86_64 host with Docker Engine, Docker Compose v2, and `uv`. Docker needs nftables `fib` support (`CONFIG_NFT_FIB_INET=y` or `m`), the `SYS_ADMIN` / `NET_ADMIN` container capabilities, and access to the task image registries.

1. Install Harbor:

   ```bash
   uv tool install --python 3.12 'harbor==0.21.0'
   ```

2. Choose a task directory and set your API key and endpoint:

   ```bash
   cd /path/to/Migration-SWE
   TASK_DIR="tasks/<task-directory>"
   export OPENAI_API_KEY="YOUR_API_KEY"
   export OPENAI_BASE_URL="https://api.openai.com/v1"
   ```

   The API hostname must also appear in the existing `[agent]` section of the task's `task.toml`:

   ```toml
   [agent]
   network_mode = "allowlist"
   allowed_hosts = ["api.openai.com"]
   ```

   For a custom OpenAI-compatible API, change both settings: `https://api.example.com/v1` goes in `OPENAI_BASE_URL`, and `api.example.com` goes in `allowed_hosts`. Keep the task's other fields and hosts. Allowlist entries contain only hostnames, without a scheme, port, or path. If setup or verification calls the same service, add its hostname to the relevant allowlists in `[environment]`, `[verifier]`, or `[verifier.environment]` too.

3. Run the task with a model ID from your API service:

   ```bash
   harbor run -p "$TASK_DIR" -a mini-swe-agent \
     -m "openai/<model-id>" -e docker -k 1 -n 1 -o jobs
   ```

Results go to `jobs/<job>/<trial>/`. Check `result.json` for run status, `verifier/reward.json` for the score (1 = full pass), and `trial.log` for logs.

To run all 40 tasks four times each, replace `-p "$TASK_DIR" -k 1` with `-p tasks -k 4`. Set the API allowlist in each task you run.

## 1. Overview

MigrationSWE contains 40 tasks from 38 open-source repositories: framework replacements, dependency upgrades, and language migrations. The agent starts from a fixed source revision, makes the migration, and submits a patch. It can inspect the repository and run tests along the way.

Tests check the migration against the original behavior. An Express-to-Fastify task checks request bodies, response headers, and errors; a Rust-to-Go task checks the interfaces and return types used by callers.

| Item | Count |
| --- | ---: |
| Migration tasks | 40 |
| Unique repositories | 38 |
| Model and harness configurations | 13 |
| Runs per task per configuration | 4 |
| Total runs | 2,080 |
| Fully successful runs | 222 (10.7%) |
| Evaluator test patches | 1,571 |

## 2. Leaderboard

Each row is a model, reasoning effort, and harness combination, run four times on each of the 40 tasks (160 runs). Rows are sorted by pass@1, with ties broken by model name. Average cost is in USD and includes failed runs.

Scores and cost estimates use the same data, prices, and exchange rates as the [HTML report](../migration-report.html).

| Model · effort | Harness | pass@1 | pass@4 | pass^4 | Avg. steps | Avg. tokens | Avg. cost / rollout | Tasks passed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gpt-6.1-sol` · xhigh | codex | 21.9% | 37.5% | 10.0% | 65.3 | 5.16M | $1.475 | 15/40 |
| `claude-opus-5-5` · xhigh | claude-code | 17.5% | 22.5% | 10.0% | 141.4 | 37.18M | $18.488 | 9/40 |
| `kimi-k3.1-pro` · max | mini-swe-agent | 12.5% | 17.5% | 10.0% | 70.7 | 8.03M | $3.004 | 7/40 |
| `hy4` · high | mini-swe-agent | 11.9% | 17.5% | 7.5% | 99.8 | 14.54M | $2.177 | 7/40 |
| `deepseek-v4-1-flash-261017` · max | mini-swe-agent | 11.3% | 15.0% | 7.5% | 128.4 | 20.19M | $0.184 | 6/40 |
| `mimo-v2.6-pro` · high | mini-swe-agent | 11.3% | 17.5% | 7.5% | 109.2 | 19.26M | $0.525 | 7/40 |
| `qwen4-max` · xhigh | mini-swe-agent | 10.0% | 15.0% | 5.0% | 187.3 | 32.64M | $5.033 | 6/40 |
| `muse-wonder` · xhigh | mini-swe-agent | 8.8% | 12.5% | 2.5% | 109.4 | 14.69M | $8.268 | 5/40 |
| `gemini-4-argon` · xhigh | mini-swe-agent | 8.1% | 12.5% | 5.0% | 92.9 | 13.30M | $9.246 | 5/40 |
| `glm-5.3` · max | mini-swe-agent | 8.1% | 15.0% | 2.5% | 185.9 | 32.72M | $10.618 | 6/40 |
| `grok-4.7` · xhigh | mini-swe-agent | 8.1% | 15.0% | 2.5% | 145.1 | 15.99M | $13.292 | 6/40 |
| `MiniMax-M3.1-Flash` · max | mini-swe-agent | 6.9% | 10.0% | 5.0% | 192.3 | 37.64M | $3.071 | 4/40 |
| `doubao-seed-2-1-pro` · high | mini-swe-agent | 2.5% | 5.0% | 0.0% | 153.7 | 14.17M | $3.211 | 2/40 |

A run counts as a pass only if its final reward is 1. pass@1 is the pass rate across 160 runs. pass@4 is the share of tasks passed at least once in four attempts; pass^4 is the share passed in all four. “Tasks passed” is the task count used for pass@4. All tasks have equal weight.

GPT passed 15 tasks at least once, but passed only four in every attempt. Claude passed nine at least once and four in every attempt.

## 3. Task design

Each task defines a source version, migration target, permitted changes, and behavior to preserve. Tests cover API return values, CLI arguments, configuration, serialization, and errors. Tasks with persistent data also check existing records, defaults, and null values.

| Type | Tasks | Main requirement | Examples |
| --- | ---: | --- | --- |
| Framework migration | 18 | Replace the framework while preserving interfaces and runtime behavior | Express → Fastify, PEG.js → Peggy |
| Version upgrade | 7 | Adapt to dependency and build changes while maintaining compatibility | ESLint 9 → 10, Django 3.2 → 5.2 |
| Language migration | 15 | Rewrite the specified modules in the target language while preserving semantics | Rust → Go, TypeScript → Rust |

By primary language (target language for rewrites), the tasks break down as follows: Go 13, JavaScript 10, TypeScript 10, Python 4, and Rust 3. The set has 27 Complex and 13 Medium tasks, rated by the size of the migration, compatibility requirements, and scope of changes.

## 4. Evaluation method

Evaluation has two stages:

1. The patch must apply, pass the required build or import checks, and preserve existing behavior. P2P (pass-to-pass) tests check behavior that already works in the source implementation. The evaluator also checks migration completeness and whether tests or the evaluation environment were tampered with, including changes to protected files, covert calls to the source implementation, and weakened tests.
2. After those checks pass, the evaluator scores migration completion. F2P (fail-to-pass) tests check requirements that fail before migration and should pass afterward. The score averages migration-scope coverage, the F2P pass rate, and any required state-preservation checks. P2P is not counted a second time.

Confirmed cheating scores −1; other failures in the first stage score 0. A full pass requires every applicable condition to pass. Checks that do not apply are left out of the average: 90% scope coverage and 80% F2P, with no state-preservation requirement, give a reward of 0.85.

## 5. Quality assurance

The evaluator is checked against the source baseline, a reference solution, and test patches. The baseline and reference solution establish the expected P2P and F2P results. Incomplete migrations, covert source calls, and weakened tests should be rejected; valid alternative solutions should pass. The archive contains 1,571 evaluator test patches, or 1,493 after deduplication.

Scores were reviewed for all 520 task/configuration pairs, with four runs per pair. Thirty passed all four runs. Of the pairs with failures, 94 had concrete behavioral differences in the saved evidence, two involved environment or policy restrictions, and three did not finish. The cause remains unresolved for the other 391 pairs.

## 6. Results by task

F2P / P2P counts come from this batch's evaluation configuration; “Adversarial patches” counts archived evaluator test samples. “Best model pass@1” is the highest pass rate across the 13 configurations for that task. A score of 75% means three passes in four attempts; 0% means no configuration passed the task.

### Framework migrations (18 tasks)

| Repository / task | Migration | Difficulty | F2P / P2P | Adversarial patches | Best model pass@1 |
| --- | --- | --- | --- | --- | --- |
| [1365436742/element-ai-vue](https://github.com/1365436742/element-ai-vue) | unified 11.0.5 → markdown-it 14.1.0 | Medium | 70 / 42 | 42 | 25.0% |
| [afar1/fieldtheory-cli](https://github.com/afar1/fieldtheory-cli) (to oclif 4.0.0) | Commander.js 14.0.3 → oclif 4.0.0 | Complex | 15 / 26 | 27 | 0.0% |
| [aieditor-team/AiEditor](https://github.com/aieditor-team/AiEditor) | OpenAI Node SDK 7.8.0 → OpenAI Agents SDK 0.1.0 | Medium | 72 / 46 | 41 | 25.0% |
| [alexellis/arkade](https://github.com/alexellis/arkade) | Cobra 1.10.2 → Kong 1.8.1 | Complex | 70 / 40 | 49 | 25.0% |
| [chrysb/alphaclaw](https://github.com/chrysb/alphaclaw) | Express 5.2.1 → Hono 4.13.7 | Complex | 72 / 40 | 41 | 25.0% |
| [cloudflare/partykit](https://github.com/cloudflare/partykit) | Hono 4.12.15 → Elysia 1.3.0 | Medium | 72 / 37 | 31 | 100.0% |
| [immersive-web/webvr-polyfill](https://github.com/immersive-web/webvr-polyfill) | Rollup 0.55 / Babel 6 / Mocha 5 → Vite 5.4 / Vitest 2.1 | Complex | 27 / 23 | 47 | 25.0% |
| [openai/openai-cua-sample-app](https://github.com/openai/openai-cua-sample-app) | Fastify 5.12.1 → Express 5.1.0 | Medium | 48 / 37 | 43 | 100.0% |
| [opendatacam/opendatacam](https://github.com/opendatacam/opendatacam) | Express 4.17.1 → Fastify 4.28.1 | Complex | 72 / 43 | 71 | 0.0% |
| [panva/node-oidc-provider](https://github.com/panva/node-oidc-provider) | Koa 3.2.1 → Fastify 5.12.0 | Complex | 72 / 40 | 41 | 0.0% |
| [parvardegr/sharing](https://github.com/parvardegr/sharing) | Express 4 → Fastify 4 | Medium | 10 / 56 | 55 | 75.0% |
| [piitaya/lovelace-mushroom](https://github.com/piitaya/lovelace-mushroom) | Superstruct 2.0.2 → Zod 3.23.8 | Complex | 75 / 19 | 38 | 25.0% |
| [ProtonMail/gopenpgp](https://github.com/ProtonMail/gopenpgp) | go-mime v0.0.0-20230322103455-7d82a3887f2f → go-message v0.18.0 | Medium | 72 / 19 | 40 | 25.0% |
| [QLHazyCoder/codex-oauth-automation-extension](https://github.com/QLHazyCoder/codex-oauth-automation-extension) | Python http.server 3.10 → Flask 3.1.2 | Medium | 130 / 23 | 38 | 0.0% |
| [QuiteAFancyEmerald/InvisiProxy](https://github.com/QuiteAFancyEmerald/InvisiProxy) | Fastify 5.12.3 → Express 5.1.0 | Medium | 106 / 25 | 30 | 0.0% |
| [richardgirges/express-fileupload](https://github.com/richardgirges/express-fileupload) | express-fileupload@1.5.2 (Express 4 + busboy) → Koa 2 async middleware (dual Express 4 compatible) | Medium | 21 / 201 | 87 | 25.0% |
| [usewaypoint/email-builder-js](https://github.com/usewaypoint/email-builder-js) | marked 12.0.2 → markdown-it 14.1.0 | Medium | 72 / 26 | 29 | 75.0% |
| [versatica/JsSIP](https://github.com/versatica/JsSIP) | PEG.js 0.7.0 → Peggy 4.2.0 | Medium | 46 / 32 | 55 | 100.0% |

### Version upgrades (7 tasks)

| Repository / task | Migration | Difficulty | F2P / P2P | Adversarial patches | Best model pass@1 |
| --- | --- | --- | --- | --- | --- |
| [asteasolutions/zod-to-openapi](https://github.com/asteasolutions/zod-to-openapi) | Zod 4.0.5 → Zod 4.1.0 | Complex | 72 / 40 | 36 | 0.0% |
| [feincms/feincms](https://github.com/feincms/feincms) | Django 3.2 → Django 5.2 | Complex | 58 / 31 | 33 | 50.0% |
| [gre/react-native-view-shot](https://github.com/gre/react-native-view-shot) | React Native 0.84.1 → React Native 0.85.0 | Complex | 71 / 33 | 32 | 50.0% |
| [jest-community/eslint-plugin-jest](https://github.com/jest-community/eslint-plugin-jest) | ESLint 9.39.5 → ESLint 10.5.0 | Medium | 72 / 40 | 36 | 100.0% |
| [js-joda/js-joda](https://github.com/js-joda/js-joda) | js-joda 6.1.0 → js-joda 6.3.2 | Medium | 67 / 40 | 23 | 100.0% |
| [linkedin/shiv](https://github.com/linkedin/shiv) | click 6.7 → click 8.1.7 | Complex | 11 / 48 | 28 | 0.0% |
| [sveltejs/devalue](https://github.com/sveltejs/devalue) | devalue 4.3.3 → devalue 6.0.0 | Complex | 61 / 22 | 33 | 0.0% |

### Language migrations (15 tasks)

| Repository / task | Migration | Difficulty | F2P / P2P | Adversarial patches | Best model pass@1 |
| --- | --- | --- | --- | --- | --- |
| [actions/labeler](https://github.com/actions/labeler) | TypeScript 6.0.3 → Go 1.24.0 | Complex | 31 / 38 | 36 | 0.0% |
| [afar1/fieldtheory-cli](https://github.com/afar1/fieldtheory-cli) (to Go 1.23.0) | TypeScript 6.0.2 → Go 1.23.0 | Complex | 35 / 16 | 30 | 0.0% |
| [dalibo/pev2](https://github.com/dalibo/pev2) | TypeScript 5.9.3 → Rust 1.83.0 | Complex | 185 / 39 | 42 | 0.0% |
| [damianricobelli/stepperize](https://github.com/damianricobelli/stepperize) (to Rust 1.85.0) | TypeScript 6.0.3 → Rust 1.85.0 | Complex | 72 / 40 | 36 | 75.0% |
| [damianricobelli/stepperize](https://github.com/damianricobelli/stepperize) (to Rust 1.83.0) | TypeScript 6.0.3 → Rust 1.83.0 | Complex | 72 / 38 | 30 | 100.0% |
| [fables-tales/rubyfmt](https://github.com/fables-tales/rubyfmt) | Rust 2024 → Go 1.24.0 | Complex | 64 / 32 | 25 | 25.0% |
| [huseyinbabal/taws](https://github.com/huseyinbabal/taws) | Rust 2021 → Go 1.23.0 | Complex | 31 / 21 | 46 | 0.0% |
| [lusingander/serie](https://github.com/lusingander/serie) | Rust 2021 → Go 1.24.0 | Complex | 72 / 30 | 33 | 50.0% |
| [ncipollo/release-action](https://github.com/ncipollo/release-action) | TypeScript 6.0.3 → Go 1.23.0 | Complex | 52 / 40 | 32 | 0.0% |
| [ramosbugs/oauth2-rs](https://github.com/ramosbugs/oauth2-rs) | Rust 2021 → Go 1.22 | Complex | 72 / 22 | 43 | 50.0% |
| [rust-itertools/itertools](https://github.com/rust-itertools/itertools) (to Go 1.23.0, task v1) | Rust 2018 → Go 1.23.0 | Complex | 48 / 27 | 33 | 0.0% |
| [servo/rust-url](https://github.com/servo/rust-url) | Rust 2018 → Go 1.23.0 | Complex | 72 / 16 | 44 | 0.0% |
| [Stremio/stremio-core](https://github.com/Stremio/stremio-core) | Rust 2021 → Go 1.23.0 | Complex | 72 / 17 | 44 | 100.0% |
| [thomas-mauran/chess-tui](https://github.com/thomas-mauran/chess-tui) | Rust 2024 → Go 1.25.0 | Complex | 72 / 33 | 35 | 0.0% |
| [zereight/gitlab-mcp](https://github.com/zereight/gitlab-mcp) (OMG MCP server) | TypeScript 5.9.3 → Python 3.13.0 | Complex | 23 / 40 | 36 | 0.0% |

## 7. Error analysis

Of 2,080 runs, 222 passed, 912 earned partial credit, and 916 scored zero. The evaluator flagged another 30 as cheating and assigned −1.

| Task type | Runs | Fully successful runs | Tasks passed by at least one configuration |
| --- | ---: | ---: | ---: |
| Framework migration | 936 | 81 | 13/18 |
| Version upgrade | 364 | 96 | 4/7 |
| Language migration | 780 | 45 | 6/15 |

All successful version-upgrade runs came from four tasks. The failures below show where compatibility broke: default values, error messages, request handling, and public types. Examples are listed in leaderboard order.

| Model | Tasks and runs | Observed differences |
| --- | --- | --- |
| `gpt-6.1-sol` | fieldtheory-cli (framework migration), run 3; release-action, run 1 | Four invalid CLI calls returned exit code 2 instead of 1, and all help-text checks failed. In release-action, standard artifact checks passed, but a POSIX digit character class matched the wrong file. |
| `claude-opus-5-5` | zod-to-openapi, run 1; alphaclaw, run 1 | The main schema references were preserved, but the CollisionDU mapping keys were reversed, giving a score of about 0.954. GET-to-POST conversion worked in the webhook migration, but the forwarded bytes were wrong in that branch; standard raw webhook checks passed. |
| `kimi-k3.1-pro` | arkade, run 1; lovelace-mushroom, run 2 | Help calls for some command aliases returned exit code 1 instead of 0. In the validation task, both implementations rejected the input, but the error entries changed from an empty list to `["wrong_type:"]`. |
| `hy4` | fieldtheory-cli (framework migration), run 2; rust-url, run 1 | Four invalid CLI calls still reported errors, but exit codes changed from 1 to 2, and the help-text check for `recent --limit` failed. In rust-url, an incompatible `AppendKeyOnly` method signature prevented the external test program from compiling. |
| `deepseek-v4-1-flash-261017` | stepperize (Rust 1.85.0), run 1; opendatacam, run 1 | The stepper migration dropped the object's own `__proto__` property. In opendatacam, the home and catch-all routes returned 200 and query-parameter checks passed, but body content was not preserved. |
| `mimo-v2.6-pro` | element-ai-vue, run 1; arkade, run 2 | Supplied middleware did not execute after the base rules as required. In arkade, an input that should have produced an argument error instead entered a path requiring `kubectl`, then stopped because the program was missing. |
| `qwen4-max` | element-ai-vue, run 1; arkade, run 1 | The public type `MiddlewarePluginItem` was missing, while export checks for the other three types and the utility module passed. In arkade, `bundle` changed from a list to a boolean, its default changed from `[]` to `false`, and the `tag` argument was missing. |
| `muse-wonder` | arkade, run 2; itertools, run 2 | Plain `version` output had a different text hash, while exit codes and JSON results matched. In itertools, passing `ExactlyOneError` to an interface requiring an iterator prevented the test program from compiling, blocking multiple cases. |
| `gemini-4-argon` | arkade, run 1; release-action, run 1 | The `image` default changed from an empty array to an empty string. In release-action, standard artifact checks passed, but the pattern `literal[.zip`, which contains a literal opening bracket, did not match the required file. |
| `glm-5.3` | fieldtheory-cli (framework migration), run 1; itertools, run 4 | `recent --limit` and `seeds create` still exited with errors, but their help-text checks failed. In itertools, incompatible generic parameter counts produced `got 2 type arguments but want 1`, preventing subsequent differential cases from running. |
| `grok-4.7` | opendatacam, run 1; itertools, run 4 | A call to `res.setHeader` failed, changing a 200 HTML page into a 500 JSON error. In itertools, the `itertools.ChainConcatenates` entry point required by the external tests was missing. |
| `MiniMax-M3.1-Flash` | arkade, run 1; itertools, run 1 | Invalid arguments still received exit code 1, but error messages gained an `arkade: error:` prefix. In itertools, a missing `Map.NextBack` method prevented the external test program from compiling. |
| `doubao-seed-2-1-pro` | devalue, run 2; fieldtheory-cli (framework migration), run 1 | Invalid `Uint8Array` data did not throw the exception raised by the original implementation. All four invalid CLI calls exited with code 1, but their error messages and help text failed the checks. |

The examples alone do not show how common each failure cause is. More cases appear in the [full report](https://tikiai.co/benchmarks/migration-swe); the archived runs include the original evaluator output.

## 8. References

- [HumanEval](https://arxiv.org/abs/2107.03374): the pass@k estimator.
- [τ-bench](https://arxiv.org/abs/2406.12045): repeated-run reliability and pass^k.
