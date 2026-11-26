# Harbor task: express-fileupload to Koa 2

This directory is a complete Harbor task. Harbor builds the agent image from
`environment/Dockerfile` and the separate verifier image from `tests/Dockerfile`.
Both images use the pinned public `harbor-bundle` base and the included,
checksum-verified Git archive of the exact task base commit. No prebuilt local
image or checkout of the upstream repository is required.

## Run

Install Harbor and Docker, then make a model API key available in the host
environment. For the task's configured gateway:

```bash
export OPENAI_API_KEY='your-key'
export OPENAI_BASE_URL='https://api.openai.com/v1'
harbor run -y -p /path/to/this-directory -a codex -m gpt-6.1-sol \
  --ak reasoning_effort=xhigh
```

Use a model and credentials available to the recipient. The first run builds
both Docker images and needs access to Docker Hub, Debian packages, Node.js,
npm, and the agent installation sources. The repository itself is bundled.
Harbor switches the verification phase to no-network before running the tests.

The Dockerfiles target Linux amd64. The verifier also needs Docker support for
the capabilities and AppArmor setting in `tests/docker-compose.yaml`.
