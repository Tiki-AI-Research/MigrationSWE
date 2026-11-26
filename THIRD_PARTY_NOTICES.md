# Third-party notices

The [task source table](PROVENANCE.md#task-sources) identifies the upstream revision, distributed source assets, and applicable license for each task. Files under [licenses/third-party](licenses/third-party) reproduce the notices supplied with those sources or the upstream notices identified here. Preserve the notices included in the original assets. When redistributing an individual archive, patch, wheel, or image, include the notices applicable to that artifact.

## Bundled materials

- **QLHazyCoder extension.** The bundled upstream LICENSE credits whwh1233 and QLHazyCoder and contributors. The upstream source notice records the shared history with StepFlow-Duck and the commit that added the original project's MIT license. [Bundled LICENSE](licenses/third-party/QLHazyCoder/codex-oauth-automation-extension/LICENSE) · [Bundled source notice](licenses/third-party/QLHazyCoder/codex-oauth-automation-extension/THIRD_PARTY_NOTICES.md) · [StepFlow-Duck MIT license](https://github.com/whwh1233/StepFlow-Duck/blob/72218aab151a2ff74bf1763684a6370657c7bc57/LICENSE)
- **Stepperize.** Both tasks use upstream commit `f1b5bfafa5d585de47f6c70eeeb355b76b20ff24`. The README and package manifests declare MIT at that revision. The linked LICENSE is absent from the upstream tree and the task bundles. The accompanying text and copyright notice come from the upstream LICENSE added in `0b64c5239974db080dead149a1a72120943f6cda`. [Pinned README](https://github.com/damianricobelli/stepperize/blob/f1b5bfafa5d585de47f6c70eeeb355b76b20ff24/README.md#license) · [Later upstream LICENSE](https://github.com/damianricobelli/stepperize/blob/0b64c5239974db080dead149a1a72120943f6cda/LICENSE) · [Accompanying copy](licenses/third-party/damianricobelli/stepperize/LICENSE)
- **Express File Upload offline dependencies.** Retain each package's copyright and license files under the offline dependency tree. `only@0.0.2` includes its MIT text in `Readme.md`. The pinned versions of `koa-compose@4.1.0` and `cache-content-type@1.0.1` declare MIT. Their full notices have been restored from the same upstream repositories at the later commits linked here. [Offline dependencies](tasks/richardgirg-express-fileupload/tests/deps/offline_deps) · [koa-compose upstream text](https://github.com/koajs/compose/blob/903480c36957776f8561a33a840cdd0a06ad611f/LICENSE) · [cache-content-type upstream text](https://github.com/node-modules/cache-content-type/blob/919c1f36b9873b6a3606b90ac49f4e4462b3a55c/LICENSE)
- **OpenDataCam Work Sans fonts.** The three `public/static/font/WorkSans-*.woff2` files in the OpenDataCam bundle identify version 2.006, copyright 2019 The Work Sans Project Authors, and the SIL Open Font License 1.1. [OFL-1.1 text and copyright](licenses/third-party/weiweihuanghuang/Work-Sans/OFL.txt)
- **WebVR Polyfill assets.** The packaged source includes the upstream license, the bundled notices in `licenses.txt`, and a separate MIT license for Three.js in the examples. [WebVR Polyfill license](tasks/immersive-web/tests/repo/LICENSE) · [Bundled notices](tasks/immersive-web/tests/repo/licenses.txt) · [Three.js license](tasks/immersive-web/tests/repo/examples/third_party/three.js/LICENSE)
- **Other bundled dependencies.** The Arkade bundle includes vendored Go dependencies and their notices under `vendor/`. The PartyKit bundle includes separate notices for PartySocket under `packages/partysocket/`. Release Action carries dependency notices in `dist/licenses.txt`. The Node OIDC Provider bundle includes notices for incorporated code. [Arkade bundle](tasks/alexellis-arkade/tests/assets/build-deps/repo/base-repo.bundle) · [PartySocket notices](licenses/third-party/cloudflare/partykit/packages/partysocket/LICENSE.txt) · [Release Action notices](licenses/third-party/ncipollo/release-action/dist/licenses.txt) · [OIDC Provider notices](licenses/third-party/panva/node-oidc-provider/THIRD-PARTY-NOTICES.md)

### Lovelace Mushroom

The task records the Apache-2.0 license identified in the upstream root LICENSE. At the pinned commit, `package.json` declares ISC. [Pinned LICENSE](https://github.com/piitaya/lovelace-mushroom/blob/d8a90ffff1b217fbbf9cb8210ee672be99596231/LICENSE) · [Pinned package.json](https://github.com/piitaya/lovelace-mushroom/blob/d8a90ffff1b217fbbf9cb8210ee672be99596231/package.json)

## Migration patches

The patches below record modifications made for the benchmark. The dated record for this repository snapshot is 2026-11-26. Each patch identifies the affected paths in its diff headers. Keep the upstream copyright and license notices with the changed material. If you distribute a resulting modified source tree, carry the change notices into it as required by the applicable license.

| Upstream | MigrationSWE modifications | License for upstream-derived material |
| --- | --- | --- |
| InvisiProxy | Reference migration changes `src/server.mjs` and package metadata. [Reference patch](tasks/QuiteAFancyEmerald-InvisiProxy/solution/solution.patch) | [AGPL-3.0-only](licenses/third-party/QuiteAFancyEmerald/InvisiProxy/LICENSE) |
| AiEditor | Reference migration changes `src/ai/OpenAiService.ts`, `src/ai/editor-tools.ts`, and package metadata. [Reference patch](tasks/aieditor-team-AiEditor/solution/solution.patch) | [LGPL-2.1-only](licenses/third-party/aieditor-team/AiEditor/LICENSE) |
| WebVR Polyfill | Reference migration replaces the legacy build and test configuration with Vite and Vitest and changes existing build and test files. [Reference patch](tasks/immersive-web/solution/solution.patch) | [Apache-2.0](tasks/immersive-web/tests/repo/LICENSE) |
| Lovelace Mushroom | Reference migration updates validation code in the card editors and shared configuration. [Reference patch](tasks/piitaya-lovelace-mushroom/solution/solution.patch) | [Apache-2.0](licenses/third-party/piitaya/lovelace-mushroom/LICENSE) |

This scope also applies where verifier patches, adversarial patches, or fixtures copy or adapt upstream code. The licenses of independent benchmark materials are described in the [license scope](PROVENANCE.md#license-scope).

## Prebuilt InvisiProxy and AiEditor images

These are benchmark images stored under `docker.io/tikiaiteam/migration`. Each task has a base image used by its environment Dockerfile, a task image that Harbor uses to run the coding agent, and a verifier image that Harbor uses to grade the submitted changes. [InvisiProxy task configuration](tasks/QuiteAFancyEmerald-InvisiProxy/task.toml) · [AiEditor task configuration](tasks/aieditor-team-AiEditor/task.toml)

The registry returned manifests for all six images listed here on 2026-11-26. The base and task image labels identify the upstream repository and commit. The task and verifier images expose build provenance. The inspected base and verifier source layers contain the upstream source and a byte-for-byte match for the bundled `/workspace/repo/LICENSE`. Each task image includes the inspected source layer from its base image.

The benchmark maintainer verified that the final images contain source code. A full review of bundled dependency notices and build materials remains a follow-up. [InvisiProxy license](licenses/third-party/QuiteAFancyEmerald/InvisiProxy/LICENSE) · [AiEditor license](licenses/third-party/aieditor-team/AiEditor/LICENSE)

| Image | Registry digest |
| --- | --- |
| `quiteafancyemerald-invisiproxy-base-image-20261126` | `sha256:bc9f05c46e115672a8feb56f29869fbb081a72e3f2fe3ce4aeff93e9700f5988` |
| `quiteafancyemerald-invisiproxy-task-image-20261126` | `sha256:25439125ef40f2795db0bec84e1855dc5a7f6da88ddec250db3cfc40df33c249` |
| `quiteafancyemerald-invisiproxy-verifier-image-20261126` | `sha256:5e710046d303ef6b941682a187363a754e48ac0743c88737fd5623d9369459bc` |
| `aieditor-team-aieditor-base-image-20261126` | `sha256:8ff027a383c5776dcdebb172957424aa139d0f1321cb2a91b0343c56bad71140` |
| `aieditor-team-aieditor-task-image-20261126` | `sha256:4acb10d04332d65e777e7ca231ad38000630f7964bc74ca68096872e05a90934` |
| `aieditor-team-aieditor-verifier-image-20261126` | `sha256:75a9bfdbfeafc856289e5b12588664f823f5a8a74497ed6ad62ebe529548ac16` |
