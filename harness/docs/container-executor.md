# Container executor setup

This guide sets up the optional built-in container executor of `backend-orchestration`:
`extensions.runtime_access: "docker"`. With it, `harness-runtime` starts every approved external
dispatch in its own container, and `qa run` runs every QA gate command in its own container. The
executor applies only the access plan that the dispatch approval pinned. It never widens that plan,
never approves anything for a human and never enables auto-approve in an agent.

The harness never builds, pulls, pushes or publishes a container image. You build the images with
the commands in this guide, and the harness only runs the image ids that `dispatch propose` pinned.

## Verified and unverified environments

A real smoke in CI covers one environment only: rootful Docker on a Linux GitHub Actions runner.
That smoke drives the public coordinator commands with a real Git worktree, a real runtime store
and real containers. It checks these results:

- A worker dispatch (`dispatch propose`, `dispatch create`, `dispatch send`) runs its adapter in a
  container, and the files the worker writes belong to the host user.
- The worker reaches an allowed host through the egress proxy over HTTPS and HTTP.
- The proxy answers `403` for a host outside the allowlist, and the worker has no direct egress.
- A container QA run (`qa run`) records the real exit code of a failing gate command in the stage
  evidence and in the `failed` report.
- No container or network with the dispatch label remains after the worker or the QA run ends.

These environments are not verified by a real smoke. Only the engine argv matrix and a scripted
engine check them, so treat them as unverified on a live engine:

- Podman, rootful and rootless.
- Rootless Docker.
- Hosts with SELinux enforcing.

These environments are not supported. `dispatch preflight` reports each one as a blocker:

- Remote Podman.
- Docker with user namespace remapping (`userns-remap`).
- An engine CLI whose `info` answer does not match the selected engine, such as a
  Docker-compatible shim.
- Any platform other than Linux, and a repository that still uses the file ledger.

## Prerequisites

- A Linux host with a local Docker or Podman engine. The `docker` or `podman` CLI must be on the
  `PATH` of `harness-runtime serve`, and the user who runs it must be allowed to use the engine.
- The runtime store epoch. Cut the ledger over once with `harness-runtime migrate --repo <repo>`,
  then keep `harness-runtime serve --repo <repo>` running while you work.
- A transport of `external` in `assignment_plans` for every role that `dispatch send` sends. An
  `in-process` role is a blocker: the executor wraps the command of an external adapter.
- The worker image, and the proxy image when `network.hosts` is not empty, present in the engine
  under the tags that the configuration names.
- A project-owned agent adapter that can run inside the worker image (see the example below).

## Configuration and overrides

Select the executor in `.harness/orchestration.json` and name the images in `access_policy`:

```json
{
  "extensions": {"runtime_access": "docker"},
  "access_policy": {
    "defaults": {
      "mode": "sandbox",
      "container": {
        "engine": "docker",
        "worker_image": "harness-worker:local",
        "proxy_image": "harness-proxy:local"
      },
      "environment": ["ANTHROPIC_API_KEY"],
      "network": {"hosts": ["api.anthropic.com", "github.com"]}
    },
    "roles": {
      "code-review": {"network": {"hosts": ["api.anthropic.com"]}}
    },
    "operations": {
      "qa": {
        "container": {
          "engine": "docker",
          "worker_image": "harness-worker:local",
          "proxy_image": "harness-proxy:local"
        },
        "environment": [],
        "network": {"hosts": ["pypi.org", "files.pythonhosted.org"]},
        "filesystem": [{"resource": "cache", "path": "/home/me/.cache/uv", "access": "write"}]
      }
    }
  },
  "execution_policy": {
    "worker_execution_timeout_seconds": 14400,
    "qa_command_timeout_seconds": 3600,
    "stop_grace_seconds": 30
  }
}
```

- `container.worker_image` is required. `engine` is `docker` (the default) or `podman`.
  `proxy_image` is required when `network.hosts` is not empty.
- `access_policy.defaults.container` is required when the executor is selected; `harness health`
  and the schema reject a configuration without it.
- `mode` must be `sandbox` or `inherit`. The executor refuses `unsandboxed`.
- A `roles.<role>` or `operations.qa` override replaces each component that it names as a whole,
  for example the complete `container` object or the complete `environment` list. The components
  it does not name come from `defaults`.
- QA runs in containers only when `access_policy.operations.qa.container` is set. Without it, QA
  stays on the host, even when `defaults.container` is set.
- `worker_execution_timeout_seconds` is the deadline of a worker container, and
  `qa_command_timeout_seconds` is the deadline of one QA command. At a deadline the runtime stops
  the container with `stop_grace_seconds` of grace, then kills it.
- The approval pins the resolved plan. A configuration change after `dispatch propose` does not
  change that dispatch: cancel it and propose again to apply the change.

## Worker image compatibility

The executor accepts any image of the project that meets these conditions:

- The image contains `/bin/sh`, because each QA command runs as `/bin/sh -c '<command>'`.
- The image contains the interpreter of the adapter's shebang line and every tool the agent uses,
  for example `git` and the agent CLI.
- The image works under an arbitrary user id. A rootful engine runs the container as the host
  `uid:gid`, which usually has no `/etc/passwd` entry in the image.
- The image does not depend on its own home directory. `HOME` is a tmpfs at `/harness-home`, so
  files that the image puts in a home directory are not visible to the worker.
- The image needs no `ENTRYPOINT`, `CMD` or `USER`: the runtime sets the entrypoint, the user and
  the working directory. Do not declare `VOLUME`: its anonymous volumes stay after the container
  is removed.
- The image contains no secret. Pass credentials by name through `environment` (see below).

The runtime starts every container with `--pull never`, `--cap-drop ALL` and
`no-new-privileges`. It sets `HOME`, a `safe.directory=*` Git setting through `GIT_CONFIG_COUNT`,
`GIT_CONFIG_KEY_0` and `GIT_CONFIG_VALUE_0`, and the marker `HARNESS_EXECUTOR=docker` for every
engine. Each plan root is mounted at the same absolute path, read-only for `read` access and
read-write for `write` access, and there are no other mounts. The host `~/.gitconfig` is not
mounted: set the Git identity in the repository configuration or pass it by name through
`environment`.

## Build the images

Run these commands in the target project. The optional base image
`.harness/container/Dockerfile.sandbox` is a pinned Debian slim image with `git`, `curl`, `jq`,
`ca-certificates`, Python 3 with `uv`, and Node.js with `npm`:

```bash
docker build -f .harness/container/Dockerfile.sandbox -t harness-sandbox:local .harness/container
podman build -f .harness/container/Dockerfile.sandbox -t harness-sandbox:local .harness/container
```

Use it as the base of your own worker image and add your toolchain and agent CLI, each pinned to a
version:

```dockerfile
FROM harness-sandbox:local
RUN npm install --global @anthropic-ai/claude-code@<version>
```

```bash
docker build -f Dockerfile.worker -t harness-worker:local .
podman build -f Dockerfile.worker -t harness-worker:local .
```

Build the egress proxy image from the installed payload:

```bash
docker build -f .harness/container/Dockerfile.proxy -t harness-proxy:local .harness/container
podman build -f .harness/container/Dockerfile.proxy -t harness-proxy:local .harness/container
```

`dispatch propose` pins the image id of each tag in the brief field `runtime_images`, and
`dispatch create` observes the images again. A send or QA run whose image changed after the
approval is refused with `approval_mismatch`. After a rebuild, cancel the dispatch and repeat
`dispatch propose`: an approval is never reused for another image.

## Caches

The worker keeps nothing between containers. Only the `cache` resources of the plan give it
persistent storage: each one is mounted at the same path with its own access. A relative `path`
resolves against the dispatch worktree. The executor creates no named volume and no long-lived
container. To use a cache from a tool, point the tool at that path, for example through a
variable passed by name in `environment`.

## Environment by name

`environment` lists variable names, never values. The container receives each name as
`--env NAME` without a value, and the engine takes the value from the environment of
`harness-runtime serve`. Restart `harness-runtime serve` after you change a value. The value never
reaches the configuration, the brief, a digest, an argv, evidence or a log.

- The names are part of the plan digest. A name that you add after `dispatch propose` does not
  reach that dispatch.
- A name that is missing from the runtime environment is a blocker of `dispatch preflight` and of
  the send.
- The executor sets some names itself, and a plan must not list them: `HOME`, `GIT_CONFIG_COUNT`,
  `GIT_CONFIG_KEY_0`, `GIT_CONFIG_VALUE_0` and `HARNESS_EXECUTOR`. With a non-empty
  `network.hosts`, the executor also sets `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`,
  `https_proxy`, `NO_PROXY` and `no_proxy`.
- Agent authentication files are not mounted. An agent must authenticate through a variable, for
  example an API key.

## Network allowlist

- With an empty `network.hosts`, the container gets `--network none` and no proxy.
- With hosts, every dispatch (and every QA run) gets its own `--internal` network and an egress
  proxy sidecar from the pinned proxy image. The proxy is the only route out. The worker receives
  the proxy URL in `HTTP_PROXY` and `HTTPS_PROXY` (also in lower case), and
  `NO_PROXY=localhost,127.0.0.1,::1`.
- The proxy forwards plain HTTP and `CONNECT` tunnels only. Ports 80 and 443 are allowed.
- The target host must equal one listed host. The match ignores case and a trailing dot, and it
  allows no wildcard and no subdomain. An IP literal is refused even when it is listed.
- The proxy resolves the host once and refuses it when any answer is a loopback, link-local,
  unspecified or multicast address. Everything else gets `403`.
- List the host of the model API explicitly. Git remotes over ssh and other non-HTTP protocols do
  not work. A tool that ignores the proxy variables has no network.

## Blockers

`dispatch preflight` lists every blocker with its remedy, and none of them creates a container.

| Blocker | Remedy |
| --- | --- |
| The role transport is not `external` | Set the role's `assignment_plans` transport to `external`. |
| The plan has no `container` component | Set `access_policy.defaults.container.worker_image`. |
| `network.hosts` is not empty and no `proxy_image` is set | Build the proxy image and set `container.proxy_image`, or set `network.hosts` to `[]`. |
| `mode` is `unsandboxed` | Set the mode to `sandbox` or `inherit`. |
| `environment` lists a name that the executor sets | Remove the reserved name. |
| The host is not Linux, or the repository uses the file ledger | Run on Linux; run `harness-runtime migrate`, then `harness-runtime serve`. |
| The engine is unavailable | Start the engine and put its CLI on the `PATH` of `harness-runtime serve`. |
| The engine mode cannot be determined from `info` | Use the real engine CLI that the configuration names. |
| Remote Podman or Docker `userns-remap` | Use a local engine without user namespace remapping. |
| An image is missing | Build or pull the image yourself, then repeat `dispatch preflight` and `dispatch propose`. |
| A variable of `environment` is missing in the runtime environment | Set it for `harness-runtime serve` and restart the runtime. |
| A plan root exposes an engine socket or a runtime directory, contains `,`, `"` or a line break, or overlaps `/harness-home` | Move the resource; mounts never include the engine socket or the runtime's own files. |
| An image changed after `dispatch propose` (`approval_mismatch`) | Restore the pinned image, or cancel the dispatch and repeat `dispatch propose`. |

## Cleanup

- Every object gets the labels `harness.dispatch`, `harness.repository`, `harness.plan_digest`,
  `harness.operation` and `harness.execution`. A worker container is named
  `harness-<dispatch_id>`, its proxy `harness-<dispatch_id>-proxy` and its network
  `harness-<dispatch_id>-net`.
- When a worker exits, the runtime saves its streams (`executions/<id>/stdout.log` and
  `stderr.log`), records the exit code, then removes the worker, the proxy and the network in that
  order. Reconcile repeats an incomplete removal.
- A failed launch removes the objects that it created, newest first. The execution becomes `lost`,
  the dispatch becomes `blocked`, and the runtime never launches it again.
- Each QA command runs with `--rm --init`. The proxy and the network of a QA run live for the
  whole run and are removed at its end.
- The harness never stops a running worker without an explicit command. To stop one, run
  `docker stop harness-<dispatch_id>` (or `podman stop`).
- `harness cleanup <repo>` lists the objects of this repository that have no live owner and removes
  them with `--apply`. It skips a worker that is still running and the objects of a live execution.
- The executor creates no named volume, and the runtime never removes a volume.

## Reports from a container

A worker in a container cannot change the ledger. Worker roles mount `shared_storage` read-only,
and the runtime socket is not mounted. So `dispatch self-report`, `dispatch heartbeat` and
`report submit` do not run inside the container. The worker writes its completion report to the
brief's `report_staging_path`, which is inside the writable artifacts root
`.harness/.sandboxes/scratch`. After the worker exits, the coordinator or a human records that
report on the host with `report submit --file <path>`.

## Example project-owned agent adapter

The harness ships no adapter for a specific agent. The script below is an example that a project
adapts and keeps as its own file. It runs Claude Code under the container executor and keeps the
agent's native approval: it passes the tool allowlist of the brief and sets no auto-approve or
permission-skipping option. The agent's own sandbox stays as the agent configures it. Whether to
turn that sandbox off inside the container is the project's own decision, outside this example.

`dispatch send --adapter <path>` runs the adapter as the container entrypoint with the arguments
`dispatch --repo <repo> --brief <brief>`. The adapter must be an executable file inside a plan
root at the same absolute path, for example a tracked file in the dispatch worktree or a file
under the main checkout's `.harness/` (the read-only `shared_storage` root). A `.py` adapter does
not work, because the coordinator passes the host's Python interpreter as its entrypoint.

```sh
#!/bin/sh
# Project-owned runtime adapter (example). The harness does not ship this file.
set -eu
[ "${1:-}" = dispatch ] || { echo "usage: $0 dispatch --repo <repo> --brief <brief>" >&2; exit 2; }
shift
brief=""
while [ $# -gt 0 ]; do
    case "$1" in
        --repo) shift 2 ;;
        --brief) brief=$2; shift 2 ;;
        *) echo "unsupported argument: $1" >&2; exit 2 ;;
    esac
done
if [ "${HARNESS_EXECUTOR:-}" != docker ]; then
    echo "this adapter runs only under the container executor" >&2
    exit 2
fi
# The container's working directory is the dispatch checkout; HOME is a tmpfs.
tools=$(jq -r '.allowed_tools | join(",")' "$brief")
model=$(jq -r '.resolved_model' "$brief")
staging=$(jq -r '.report_staging_path' "$brief")
exec claude --print --model "$model" --allowedTools "$tools" \
    "Work on the brief $brief. You cannot run coordinator commands in this container. \
Write the completion report JSON to $staging and stop."
```

The configuration of this example lists `ANTHROPIC_API_KEY` in `environment` and
`api.anthropic.com` in `network.hosts`, and the worker image contains `jq` and the agent CLI.
An action that needs approval and is outside the brief's `allowed_tools` is denied, because a
non-interactive session cannot ask a human.
